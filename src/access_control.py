"""Permission and rate limiting middleware for Telegram handlers.

Two orthogonal concerns live here:

* :class:`PermissionManager` — who may use the bot at all.  Supports an
  open mode (the historical behaviour), a whitelist mode and an admin-only
  mode, all switchable at runtime through ``TLA_ACCESS_MODE`` or the admin
  ``/access`` command without a restart.
* :class:`RateLimiter` — how often a single user may invoke handlers.  A
  sliding window per scope, evaluated in O(1) with a deque of timestamps.

Both are intentionally dependency free and safe to call from async code:
all mutations happen under a lock and are microsecond scale.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from collections import deque
from dataclasses import dataclass, field
from threading import RLock
from typing import Deque, Iterable, MutableMapping

# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------

OPEN = "open"
WHITELIST = "whitelist"
ADMIN_ONLY = "admin_only"

_VALID_MODES = frozenset({OPEN, WHITELIST, ADMIN_ONLY})
#: Fail-closed: an unconfigured deployment must not trust strangers.  The
#: historical ``open`` default let anyone who discovered the bot reach the
#: host; ``open`` now has to be chosen explicitly via ``TLA_ACCESS_MODE``.
_DEFAULT_MODE = os.environ.get("TLA_ACCESS_MODE", ADMIN_ONLY)


class AccessDenied(Exception):
    """Raised when a user is not allowed to invoke a handler."""

    def __init__(self, reason: str, *, key: str = "access_denied") -> None:
        super().__init__(reason)
        self.reason = reason
        self.key = key


@dataclass
class AccessDecision:
    """Result of an access check, kept structured for audit logging."""

    allowed: bool
    reason: str = ""
    key: str = ""


class PermissionManager:
    """Resolves whether a Telegram user may interact with the bot.

    ``admins`` always retain access in every mode.  In ``open`` mode anyone
    is accepted, in ``whitelist`` mode only whitelisted users, and in
    ``admin_only`` mode only admins — useful as a security hold during an
    incident.
    """

    def __init__(
        self,
        mode: str = _DEFAULT_MODE,
        whitelisted: Iterable[int] = (),
        admins: Iterable[int] = (),
    ) -> None:
        self._lock = RLock()
        self._mode = mode if mode in _VALID_MODES else OPEN
        self._whitelist: set[int] = set(whitelisted)
        self._admins: set[int] = set(admins)

    # -- configuration ---------------------------------------------------
    @property
    def mode(self) -> str:
        with self._lock:
            return self._mode

    def set_mode(self, mode: str) -> str:
        """Switches the access mode at runtime and returns the active mode."""
        with self._lock:
            self._mode = mode if mode in _VALID_MODES else self._mode
            return self._mode

    def update(
        self,
        *,
        whitelisted: Iterable[int] | None = None,
        admins: Iterable[int] | None = None,
    ) -> None:
        """Replaces the whitelist and/or admin set from configuration."""
        with self._lock:
            if whitelisted is not None:
                self._whitelist = {int(u) for u in whitelisted}
            if admins is not None:
                self._admins = {int(u) for u in admins}

    @property
    def whitelisted(self) -> tuple[int, ...]:
        with self._lock:
            return tuple(sorted(self._whitelist))

    @property
    def admins(self) -> tuple[int, ...]:
        with self._lock:
            return tuple(sorted(self._admins))

    def is_admin(self, user_id: int | None) -> bool:
        if user_id is None:
            return False
        with self._lock:
            return user_id in self._admins

    # -- evaluation -------------------------------------------------------
    def check(self, user_id: int | None) -> AccessDecision:
        """Returns an :class:`AccessDecision` for the given user."""
        if user_id is None:
            return AccessDecision(False, "unidentified user", "access_denied_unknown")

        with self._lock:
            mode = self._mode
            is_admin = user_id in self._admins
            is_whitelisted = user_id in self._whitelist

        if is_admin:
            return AccessDecision(True, "admin")
        if mode == OPEN:
            return AccessDecision(True, "open mode")
        if mode == WHITELIST and is_whitelisted:
            return AccessDecision(True, "whitelisted")
        if mode == ADMIN_ONLY:
            return AccessDecision(False, "admin-only mode", "access_denied_admin_only")
        return AccessDecision(False, "not whitelisted", "access_denied_not_whitelisted")


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


@dataclass
class _Window:
    timestamps: Deque[float] = field(default_factory=deque)


class RateLimiter:
    """Sliding-window rate limiter with per-scope budgets.

    Scopes let the bot be strict where it matters (destructive operations)
    while staying responsive elsewhere::

        limiter.check(user_id, "connect", max_calls=5, period=60)
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._windows: MutableMapping[tuple[int, str], _Window] = {}

    def check(
        self,
        user_id: int | None,
        scope: str = "default",
        *,
        max_calls: int = 30,
        period: float = 60.0,
    ) -> tuple[bool, float]:
        """Returns ``(allowed, retry_after_seconds)``.

        ``retry_after`` is meaningful only when the call was denied.  A
        ``None`` user id is always allowed — the permission layer rejects
        those before any accounting happens.
        """
        if user_id is None:
            return True, 0.0

        now = time.monotonic()
        key = (user_id, scope)
        with self._lock:
            window = self._windows.get(key)
            if window is None:
                window = _Window()
                self._windows[key] = window

            cutoff = now - period
            timestamps = window.timestamps
            while timestamps and timestamps[0] <= cutoff:
                timestamps.popleft()

            if len(timestamps) >= max_calls:
                # timestamps[0] > now - period is guaranteed by the pruning
                # above, so this is <= period mathematically; the clamp keeps
                # float accumulation (timestamps[0] + period - now) from
                # leaking a few ULPs past ``period`` to callers.
                retry_after = min(period, max(0.0, timestamps[0] + period - now))
                return False, retry_after

            timestamps.append(now)
            return True, 0.0

    def reset(self, user_id: int | None = None, scope: str | None = None) -> None:
        """Clears accounting for a user, a scope, or everything."""
        with self._lock:
            if user_id is None and scope is None:
                self._windows.clear()
                return
            keys = [
                key
                for key in self._windows
                if (user_id is None or key[0] == user_id)
                and (scope is None or key[1] == scope)
            ]
            for key in keys:
                del self._windows[key]

    def usage(self, user_id: int, scope: str = "default") -> int:
        with self._lock:
            window = self._windows.get((user_id, scope))
            return len(window.timestamps) if window else 0


# ---------------------------------------------------------------------------
# First-run admin enrollment (setup PIN)
# ---------------------------------------------------------------------------


class SetupEnrollment:
    """One-time admin claim via a setup PIN.

    A fresh deployment has no admin configured, so its owner cannot use the
    bot yet — and cannot know their numeric Telegram ID offhand.  When
    ``TLA_SETUP_PIN`` (or ``config.json``'s ``setup_pin``) is set and the
    whitelist is empty, the first user to send ``/start <pin>`` is enrolled
    as admin.  Attempts are throttled (5 failures → 15 minute lockout per
    user) and the PIN is compared with :func:`hmac.compare_digest`, never
    stored in plaintext after construction.
    """

    MAX_ATTEMPTS = 5
    LOCKOUT_SECONDS = 15 * 60.0

    def __init__(self, pin: str = "") -> None:
        self._lock = RLock()
        self._pin_hash: str = (
            hashlib.sha256(pin.strip().encode("utf-8")).hexdigest() if pin.strip() else ""
        )
        self._attempts: MutableMapping[int, int] = {}
        self._lockouts: MutableMapping[int, float] = {}

    @property
    def enabled(self) -> bool:
        """True when a PIN is configured and enrollment may proceed."""
        with self._lock:
            return bool(self._pin_hash)

    def _locked_for(self, user_id: int, now: float) -> float:
        """Returns remaining lockout seconds for ``user_id`` (0 if none)."""
        until = self._lockouts.get(user_id)
        if until is None:
            return 0.0
        remaining = until - now
        if remaining <= 0:
            self._lockouts.pop(user_id, None)
            self._attempts.pop(user_id, None)
            return 0.0
        return remaining

    def verify(self, user_id: int | None, pin: str) -> tuple[bool, str]:
        """Checks a claimed PIN; returns ``(ok, message_key)``.

        ``message_key`` explains the outcome for localization:
        ``setup_ok``, ``setup_disabled``, ``setup_locked_out``,
        ``setup_wrong_pin`` or ``setup_not_available``.
        """
        if user_id is None:
            return False, "setup_not_available"
        now = time.monotonic()
        with self._lock:
            if not self._pin_hash:
                return False, "setup_disabled"
            remaining = self._locked_for(user_id, now)
            if remaining > 0:
                return False, "setup_locked_out"
            candidate = hashlib.sha256(pin.strip().encode("utf-8")).hexdigest()
            if hmac.compare_digest(candidate, self._pin_hash):
                # Burn the PIN after a successful claim: single-use.
                self._pin_hash = ""
                self._attempts.pop(user_id, None)
                return True, "setup_ok"
            self._attempts[user_id] = self._attempts.get(user_id, 0) + 1
            if self._attempts[user_id] >= self.MAX_ATTEMPTS:
                self._lockouts[user_id] = now + self.LOCKOUT_SECONDS
                self._attempts.pop(user_id, None)
                return False, "setup_locked_out"
            return False, "setup_wrong_pin"

    def reset(self) -> None:
        """Clears attempts and lockouts (used by tests and re-configuration)."""
        with self._lock:
            self._attempts.clear()
            self._lockouts.clear()


# ---------------------------------------------------------------------------
# Singletons
# ---------------------------------------------------------------------------

#: Process-wide permission manager.  ``main()`` syncs it with ``config``.
permissions = PermissionManager()

#: Process-wide rate limiter used by the ``rate_limit`` decorator.
rate_limiter = RateLimiter()

#: Process-wide setup-PIN enrollment; ``lifecycle`` syncs it with ``config``.
setup_enrollment = SetupEnrollment()
