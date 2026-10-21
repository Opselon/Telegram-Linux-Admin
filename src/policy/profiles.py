"""Authority profiles for the command pipeline.

PURPOSE
    Bound what an operator may do through Telegram.  An SSH admin bot must be
    able to run arbitrary commands, so the safety story is not "which commands
    exist" but "whose session is allowed to do what": on-call read access
    (READONLY), the normal operator (STANDARD), break-glass (FULL, opt-in for
    even the catastrophic band) and an incident kill switch (LOCKED).

OWNER
    lane policy (src/policy/**).

CONSUMES
    ``src.command_policy.MAX_COMMAND_BYTES`` for the global size ceiling.  The
    read-only allowlist here is deliberately local: it describes *what a
    profile permits*, not *what is dangerous*.

PROVIDES
    ``Profile``, ``ProfileRules``, ``rules_for``, ``parse_profile``,
    ``read_only_violation``, ``is_read_only_command``,
    ``load_profiles``, ``profile_for`` and ``COMMAND_PROFILES_KEY``.

INVARIANTS
    * ``rules_for`` is total for the four profiles and raises ``ValueError``
      for anything else (importers must not silently get STANDARD).
    * ``load_profiles`` never raises: a missing file, broken JSON, a non-mapping
      payload or a garbage entry all degrade to "use the default".
    * Read-only detection is a conservative allowlist: an unknown binary, a
      shell metacharacter or a write-capable subcommand is *not* read-only.
    * Nothing in this module executes or mutates state.

EXTEND
    Add a binary to ``READ_ONLY_COMMANDS`` only when every invocation of it is
    observational.  Binaries with write-capable subcommands or positional
    arguments belong in ``READ_ONLY_SUBCOMMANDS`` / ``READ_ONLY_DENY_FLAGS``
    instead, or nowhere at all.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping

from ..command_policy import MAX_COMMAND_BYTES

logger = logging.getLogger(__name__)

#: Key in ``config.json`` holding the alias -> profile mapping.
COMMAND_PROFILES_KEY = "command_profiles"


class Profile(str, Enum):
    """The four authority levels an operator session can run under."""

    READONLY = "readonly"
    STANDARD = "standard"
    FULL = "full"
    LOCKED = "locked"


@dataclass(frozen=True, slots=True)
class ProfileRules:
    """Immutable authority envelope of a single profile."""

    profile: Profile
    allow_confirm: bool
    allow_blocked: bool
    max_command_bytes: int
    deny_patterns: tuple[str, ...]
    allow_services: tuple[str, ...] | None  # None = all
    read_only: bool


#: Defaults per profile.  ``allow_blocked`` is False everywhere on purpose:
#: FULL means "break-glass unconfirmed", not "may wipe the root filesystem".
_PROFILE_RULES: dict[Profile, ProfileRules] = {
    Profile.READONLY: ProfileRules(
        profile=Profile.READONLY,
        allow_confirm=False,
        allow_blocked=False,
        max_command_bytes=1024,
        deny_patterns=(),
        allow_services=None,
        read_only=True,
    ),
    Profile.STANDARD: ProfileRules(
        profile=Profile.STANDARD,
        allow_confirm=True,
        allow_blocked=False,
        max_command_bytes=MAX_COMMAND_BYTES,
        deny_patterns=(),
        allow_services=None,
        read_only=False,
    ),
    Profile.FULL: ProfileRules(
        profile=Profile.FULL,
        allow_confirm=True,
        allow_blocked=False,
        max_command_bytes=MAX_COMMAND_BYTES,
        deny_patterns=(),
        allow_services=None,
        read_only=False,
    ),
    Profile.LOCKED: ProfileRules(
        profile=Profile.LOCKED,
        allow_confirm=False,
        allow_blocked=False,
        max_command_bytes=MAX_COMMAND_BYTES,
        deny_patterns=(),
        allow_services=None,
        read_only=True,
    ),
}


def parse_profile(value: object) -> Profile | None:
    """Coerces *value* to a :class:`Profile`, or ``None`` when it is not one.

    Tolerant on purpose: it is used for config entries, so it accepts ``" full "``
    and a ``Profile`` member and returns ``None`` instead of raising.
    """

    if isinstance(value, Profile):
        return value
    if isinstance(value, str):
        try:
            return Profile(value.strip().lower())
        except ValueError:
            return None
    return None


def rules_for(profile: Profile | str) -> ProfileRules:
    """Returns the rules of *profile*; raises ``ValueError`` if unknown.

    Strict on purpose: callers that pass an unknown profile must fail loudly
    instead of inheriting STANDARD authority by accident.
    """

    parsed = parse_profile(profile)
    if parsed is None:
        raise ValueError(f"Unknown policy profile: {profile!r}")
    return _PROFILE_RULES[parsed]


# ---------------------------------------------------------------------------
# Read-only command detection
# ---------------------------------------------------------------------------

#: Binaries whose every invocation is observational.
READ_ONLY_COMMANDS: frozenset[str] = frozenset(
    {
        "ls", "cat", "head", "tail", "wc", "stat", "file", "du", "df", "free",
        "uptime", "w", "who", "whoami", "id", "groups", "uname", "hostname",
        "date", "echo", "sort", "uniq", "cut", "tr", "grep", "egrep", "fgrep",
        "ps", "pgrep", "pstree", "nproc", "lsof", "ss", "netstat", "lsblk",
        "blkid", "lsmod", "modinfo", "dmesg", "journalctl", "systemctl",
        "docker",
    }
)

#: Binaries that are read-only only for an allowlisted subcommand.
READ_ONLY_SUBCOMMANDS: Mapping[str, frozenset[str]] = {
    "systemctl": frozenset(
        {
            "status", "show", "cat", "list-units", "list-unit-files",
            "list-sockets", "list-timers", "list-jobs", "list-dependencies",
            "is-active", "is-enabled", "is-failed", "is-system-running",
            "get-default",
        }
    ),
    "docker": frozenset(
        {"ps", "logs", "stats", "inspect", "version", "info", "images",
         "top", "port", "diff", "history"}
    ),
}

#: Flags that turn a nominally read-only binary into a mutating one.
READ_ONLY_DENY_FLAGS: Mapping[str, tuple[str, ...]] = {
    "journalctl": (
        "--vacuum-size", "--vacuum-time", "--vacuum-files", "--rotate",
        "--flush", "--setup-keys", "--update-catalog", "--sync",
    ),
    "date": ("-s", "--set"),
    "dmesg": ("-c", "-C", "--clear", "-n", "--console-level"),
    "hostname": ("-F", "--file", "-b", "--boot"),
}

#: Binaries that take a bare positional argument and would then write.
READ_ONLY_NO_BARE_ARGS: frozenset[str] = frozenset({"hostname", "who", "w", "uptime"})

#: Any of these means the command is a pipeline/redirect/chain, not one command.
_METACHARACTERS = re.compile(r"[;|&<>`\r\n]")
_COMMAND_SUBSTITUTION = "$("


def _first_positional(tokens: list[str]) -> str | None:
    for token in tokens:
        if not token.startswith("-"):
            return token
    return None


def read_only_violation(command: str) -> str | None:
    """Explains why *command* is not provably read-only, or returns ``None``.

    Conservative by construction: anything the allowlist cannot positively
    recognise — an unknown binary, a pipe, a redirect, a nested shell, a
    `systemctl restart` — is reported as a violation.
    """

    if not isinstance(command, str) or not command.strip():
        return "the command is empty"
    if "\x00" in command:
        return "the command contains a NUL byte"
    if _METACHARACTERS.search(command) or _COMMAND_SUBSTITUTION in command:
        return "the command uses a shell metacharacter (pipe, redirect, chain or substitution)"
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return "the command is not a parseable simple command"
    if not tokens:
        return "the command is empty"

    binary = os.path.basename(tokens[0])
    if binary not in READ_ONLY_COMMANDS:
        return f"'{binary}' is not on the read-only allowlist"

    arguments = tokens[1:]
    allowed_subcommands = READ_ONLY_SUBCOMMANDS.get(binary)
    if allowed_subcommands is not None:
        subcommand = _first_positional(arguments)
        if subcommand is None:
            return f"'{binary}' was called without a read-only subcommand"
        if subcommand not in allowed_subcommands:
            return f"'{binary} {subcommand}' is not a read-only operation"

    for flag in READ_ONLY_DENY_FLAGS.get(binary, ()):
        if any(arg == flag or arg.startswith(f"{flag}=") for arg in arguments):
            return f"'{binary}' was given {flag}, which changes state"

    if binary in READ_ONLY_NO_BARE_ARGS and _first_positional(arguments) is not None:
        return f"'{binary}' was given a positional argument, which changes state"

    return None


def is_read_only_command(command: str) -> bool:
    """True when *command* is a single, allowlisted, observational command."""

    return read_only_violation(command) is None


# ---------------------------------------------------------------------------
# config.json loader
# ---------------------------------------------------------------------------


def _coerce_mapping(payload: object) -> Mapping[str, Any] | None:
    return payload if isinstance(payload, Mapping) else None


def _read_config_file(path: str | os.PathLike[str]) -> Mapping[str, Any] | None:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("Cannot read policy profiles from %r: %s", str(path), exc)
        return None
    return _coerce_mapping(payload)


def load_profiles(
    path: str | os.PathLike[str] | None = None,
    *,
    data: Mapping[str, Any] | None = None,
) -> dict[str, Profile]:
    """Reads the ``command_profiles`` alias -> profile map; never raises.

    *data* wins over *path* so callers that already parsed ``config.json`` (or
    hold a ``Config.extra`` mapping) can reuse it.  Unknown profiles, non-string
    values, blank aliases and a missing/broken file are all skipped: the caller
    then falls back to the default profile instead of failing to boot.
    """

    payload: Mapping[str, Any] | None
    if data is not None:
        payload = _coerce_mapping(data)
        if payload is None:
            logger.warning("Ignoring non-mapping policy profile data: %r", type(data).__name__)
            return {}
    elif path is None:
        return {}
    else:
        payload = _read_config_file(path)
        if payload is None:
            return {}

    mapping = _coerce_mapping(payload.get(COMMAND_PROFILES_KEY))
    if mapping is None:
        if COMMAND_PROFILES_KEY in payload:
            logger.warning("'%s' must be a mapping; ignoring it", COMMAND_PROFILES_KEY)
        return {}

    profiles: dict[str, Profile] = {}
    for alias, raw_profile in mapping.items():
        if not isinstance(alias, str) or not alias.strip():
            logger.warning("Ignoring policy profile entry with a bad alias: %r", alias)
            continue
        profile = parse_profile(raw_profile)
        if profile is None:
            logger.warning(
                "Ignoring unknown policy profile %r for alias %r", raw_profile, alias
            )
            continue
        profiles[alias.strip()] = profile
    return profiles


def profile_for(
    alias: str | None,
    profiles: Mapping[str, Profile] | None,
    default: Profile = Profile.STANDARD,
) -> Profile:
    """Returns the profile bound to *alias*, falling back to *default*.

    Kept separate from :func:`load_profiles` so the hot path (one command) does
    a dict lookup rather than re-reading configuration.
    """

    if not isinstance(alias, str) or not profiles:
        return default
    return profiles.get(alias.strip(), default)


def normalized_services(services: Iterable[str] | None) -> tuple[str, ...] | None:
    """Normalizes a service allowlist: ``None`` stays "all", ``()`` means none."""

    if services is None:
        return None
    if isinstance(services, str):
        services = (services,)
    normalized: list[str] = []
    for item in services:
        name = str(item).strip().lower()
        if name.endswith(".service"):
            name = name[: -len(".service")]
        if name:
            normalized.append(name)
    return tuple(normalized)
