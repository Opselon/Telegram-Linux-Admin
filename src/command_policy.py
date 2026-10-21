"""Guard rails for free-form remote commands.

An SSH admin bot must execute arbitrary commands — that is its purpose.  But a
mistyped ``rm -rf /`` or a pasted block-device wipe from a Telegram phone
should not be one tap away.  This module classifies a command into three
bands:

``SAFE``
    Runs immediately.

``CONFIRM``
    Legitimate but disruptive (service restarts, package removal, firewall
    flushes).  The caller must show an explicit inline confirmation bound to
    the exact command, valid for :data:`CONFIRMATION_TTL` seconds and usable
    exactly once.

``BLOCKED``
    Catastrophic or irreversible (root filesystem deletion, block device
    wipes, fork bombs, killing every process).  Refused outright; there is no
    confirmation path, because a mistap must not be able to destroy a host.

The classifier inspects ``sh -c``/``bash -c`` payloads recursively so a
command cannot hide its intent behind a nested shell.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import threading
import time
from dataclasses import dataclass
from enum import Enum

#: How long a pending confirmation stays valid.
CONFIRMATION_TTL = 180.0
#: Maximum nested shell depth inspected.
MAX_SHELL_DEPTH = 3


class Risk(str, Enum):
    SAFE = "safe"
    CONFIRM = "confirm"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class Assessment:
    """Classification of a single command."""

    risk: Risk
    rule: str = "default"
    reason: str = ""

    @property
    def blocked(self) -> bool:
        return self.risk is Risk.BLOCKED

    @property
    def needs_confirmation(self) -> bool:
        return self.risk is Risk.CONFIRM


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

_SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
_WRAPPERS = {"sudo", "env", "nice", "ionice", "time", "nohup", "command", "exec", "doas"}

_BLOCKED_PATTERNS: tuple[tuple[str, str, str], ...] = (
    ("rm-root", r"\brm\b[^\n;|&]*?(?:--no-preserve-root|(?<![\w./-])/(?:\s*\*)?(?=\s|$|;|\|))",
     "recursive deletion of the root filesystem"),
    ("mkfs", r"\bmkfs(?:\.\w+)?\b", "formatting a filesystem"),
    ("wipefs", r"\bwipefs\b", "wiping filesystem signatures"),
    ("dd-device", r"\bdd\b[^\n;|&]*\bof=\s*/dev/(?:sd|hd|vd|nvme|mmcblk|disk|xvd)",
     "writing raw data over a block device"),
    ("redirect-device", r">\s*/dev/(?:sd|hd|vd|nvme|mmcblk|disk|xvd)\w*",
     "redirecting data onto a block device"),
    ("fork-bomb", r":\s*\(\s*\)\s*\{.*:\s*\|\s*:.*\}", "fork bomb"),
    ("chmod-root", r"\bchmod\b[^\n;|&]*\b0*00\b[^\n;|&]*\s/(?:\s|$)", "removing all permissions from /"),
    ("chown-root", r"\bchown\b[^\n;|&]*-R[^\n;|&]*\s/(?:\s|$)", "recursively changing ownership of /"),
    ("kill-all", r"\bkill\s+-9\s+-1\b|\bkillall5\b", "killing every process on the host"),
    ("rm-var", r"\brm\b[^\n;|&]*\s/(?:bin|boot|etc|lib|sbin|usr)(?:\s|$|/)",
     "deleting a critical system directory"),
    ("find-delete-root", r"\bfind\s+/\s[^\n;|&]*-delete\b", "deleting every file under /"),
    ("history-wipe", r"\btruncate\b[^\n;|&]*\s0\s*/var/log/(?:wtmp|btmp|lastlog)\b",
     "destroying authentication audit logs"),
)

_CONFIRM_PATTERNS: tuple[tuple[str, str, str], ...] = (
    ("power", r"\b(?:shutdown|reboot|halt|poweroff)\b|\binit\s+[06]\b|\bsystemctl\s+(?:reboot|poweroff|halt)\b",
     "powering off or rebooting the host"),
    ("rm-recursive", r"\brm\b[^\n;|&]*\s-[a-zA-Z]*[rR][a-zA-Z]*\b", "recursive deletion"),
    ("firewall-flush", r"\b(?:iptables|ip6tables)\b[^\n;|&]*\s-(?:F|X)\b|\bufw\s+(?:disable|reset)\b"
                       r"|\bfirewall-cmd\b[^\n;|&]*--panic-on", "flushing or disabling the firewall"),
    ("ssh-stop", r"\bsystemctl\s+(?:stop|disable|mask)\s+(?:ssh|sshd)\b", "stopping the SSH service (may lock you out)"),
    ("cron-wipe", r"\bcrontab\s+-r\b|\brm\b[^\n;|&]*/var/spool/cron\b", "deleting scheduled jobs"),
    ("user-delete", r"\b(?:userdel|groupdel|deluser)\b", "deleting a system account"),
    ("pkg-remove", r"\b(?:apt(?:-get)?|yum|dnf|zypper|apk)\s+(?:remove|purge|autoremove)\b",
     "removing installed packages"),
    ("dd-read", r"\bdd\b\s+if=", "raw disk copy"),
    ("docker-destructive", r"\bdocker\b[^\n;|&]*(?:system\s+prune|volume\s+rm|rm\s+-f|container\s+prune)",
     "pruning or force-removing containers/volumes"),
    ("process-kill", r"\b(?:pkill|killall)\b", "killing processes by name"),
    ("pipe-to-shell", r"\b(?:curl|wget)\b[^\n;|&]*\|\s*(?:sudo\s+)?(?:sh|bash)\b",
     "piping a remote script straight into a shell"),
    ("truncate", r"\btruncate\b[^\n;|&]*\s-s\s*0\b|\b>\s*/etc/", "truncating a file"),
    ("password-change", r"\bpasswd\b|\bchpasswd\b", "changing account passwords"),
    ("sql-drop", r"\bdrop\s+(?:database|table|schema)\b", "dropping a database object"),
    ("sysctl", r"\bsysctl\b[^\n;|&]*-w\b", "changing kernel parameters at runtime"),
)


def _normalized(command: str) -> str:
    return " ".join(command.strip().split())


def _shell_payloads(command: str, depth: int = 0) -> list[str]:
    """Extracts ``sh -c``/``bash -c`` payloads, recursively (bounded)."""
    if depth >= MAX_SHELL_DEPTH:
        return []
    payloads: list[str] = []
    for match in re.finditer(r"\b(?:sh|bash|zsh|dash|ksh)\b\s+(?:-\w+\s+)*-c\s+(['\"])(.+?)\1", command, re.S):
        inner = match.group(2)
        payloads.append(inner)
        payloads.extend(_shell_payloads(inner, depth + 1))
    return payloads


def _classify_one(command: str) -> Assessment:
    normalized = _normalized(command)
    if not normalized:
        return Assessment(Risk.SAFE, "default", "")

    for rule, pattern, reason in _BLOCKED_PATTERNS:
        if re.search(pattern, normalized, re.IGNORECASE):
            return Assessment(Risk.BLOCKED, rule, reason)

    for rule, pattern, reason in _CONFIRM_PATTERNS:
        if re.search(pattern, normalized, re.IGNORECASE):
            return Assessment(Risk.CONFIRM, rule, reason)

    return Assessment(Risk.SAFE, "default", "")


def classify(command: str) -> Assessment:
    """Classifies *command*, inspecting nested shell payloads as well."""
    if not isinstance(command, str):
        raise TypeError("command must be a string")
    assessment = _classify_one(command)
    if assessment.risk is Risk.BLOCKED:
        return assessment
    for payload in _shell_payloads(command):
        nested = _classify_one(payload)
        if nested.risk is Risk.BLOCKED:
            return nested
        if nested.risk is Risk.CONFIRM:
            assessment = nested
    return assessment


# ---------------------------------------------------------------------------
# Confirmation tokens
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PendingCommand:
    """A CONFIRM-band command awaiting an explicit user tap."""

    token: str
    user_id: int
    alias: str
    command: str
    assessment: Assessment
    created_at: float
    expires_at: float

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(
            f"{self.user_id}\0{self.alias}\0{self.command}".encode("utf-8")
        ).hexdigest()


class ConfirmationStore:
    """Single-use, time-boxed confirmations, one per user."""

    def __init__(self, ttl: float = CONFIRMATION_TTL, clock=time.monotonic) -> None:
        self._ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: dict[int, PendingCommand] = {}

    def create(self, user_id: int, alias: str, command: str, assessment: Assessment) -> PendingCommand:
        now = self._clock()
        pending = PendingCommand(
            token=secrets.token_urlsafe(8),
            user_id=user_id,
            alias=alias,
            command=command,
            assessment=assessment,
            created_at=now,
            expires_at=now + self._ttl,
        )
        with self._lock:
            self._pending[user_id] = pending
        return pending

    def peek(self, user_id: int) -> PendingCommand | None:
        with self._lock:
            pending = self._pending.get(user_id)
        if pending is None:
            return None
        if pending.expires_at <= self._clock():
            self.cancel(user_id)
            return None
        return pending

    def pop(self, user_id: int, token: str) -> PendingCommand | None:
        """Validates and consumes the pending command (single use)."""
        if not isinstance(token, str) or not token:
            return None
        with self._lock:
            pending = self._pending.get(user_id)
            if pending is None:
                return None
            if pending.expires_at <= self._clock():
                self._pending.pop(user_id, None)
                return None
            if not secrets.compare_digest(pending.token, token):
                return None
            return self._pending.pop(user_id)

    def cancel(self, user_id: int) -> bool:
        with self._lock:
            return self._pending.pop(user_id, None) is not None

    def sweep(self) -> int:
        """Drops expired entries; returns how many were removed."""
        now = self._clock()
        with self._lock:
            expired = [uid for uid, p in self._pending.items() if p.expires_at <= now]
            for uid in expired:
                del self._pending[uid]
        return len(expired)

    def __len__(self) -> int:  # pragma: no cover - trivial
        with self._lock:
            return len(self._pending)


#: Process-wide store used by the command execution handler.
CONFIRMATIONS = ConfirmationStore()

#: Largest command accepted from Telegram (bytes) before refusing outright.
MAX_COMMAND_BYTES = 4096


def validate_command_text(command: str) -> str:
    """Rejects empty, oversized, NUL-containing or non-text commands."""
    if not isinstance(command, str):
        raise ValueError("Command must be text.")
    if not command.strip():
        raise ValueError("Command is empty.")
    if "\x00" in command:
        raise ValueError("Command contains a NUL byte.")
    if len(command.encode("utf-8")) > MAX_COMMAND_BYTES:
        raise ValueError(f"Command exceeds {MAX_COMMAND_BYTES} bytes.")
    return command
