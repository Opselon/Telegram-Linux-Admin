"""Authentication service: web logins, Argon2id hashing, brute-force lockout.

Session issuance/revocation lives in :mod:`.store`; this module adds the
credential layer (hashing, verification, lockout accounting, TOTP) and the
bootstrap flow that creates the first OWNER account.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError

from .. import database
from ..audit import audit as _audit


def audit_record(event: str, **fields: Any) -> None:
    """Audit one security-relevant event (shared helper for all services)."""
    _audit.record(event, **fields)
from . import store
from .permissions import VALID_ROLES, ROLE_OWNER

_hasher = PasswordHasher()  # argon2id, OWASP-tuned defaults

LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 15 * 60.0


class AuthError(Exception):
    """Authentication failed; the message is safe to show to the caller."""

    def __init__(self, key: str, message: str) -> None:
        self.key = key
        super().__init__(message)


@dataclass
class _Failures:
    count: int = 0
    locked_until: float = 0.0


@dataclass
class AuthService:
    """Credential verification + lockout accounting (per username+client key)."""

    lockout_seconds: float = LOGIN_LOCKOUT_SECONDS
    max_attempts: int = LOGIN_MAX_ATTEMPTS
    _failures: dict[tuple[str, str], _Failures] = field(default_factory=dict, repr=False)

    # -- hashing -----------------------------------------------------------
    @staticmethod
    def hash_password(password: str) -> str:
        return _hasher.hash(password)

    @staticmethod
    def verify_password(password_hash: str, password: str) -> bool:
        try:
            return _hasher.verify(password_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    @staticmethod
    def check_password_strength(password: str) -> str | None:
        """Returns a rejection reason, or None when acceptable."""
        if len(password) < 10:
            return "password_too_short"
        if len(password) > 256:
            return "password_too_long"
        if password.lower() in {"password", "passw0rd", "1234567890", "qwertyuiop"}:
            return "password_too_common"
        return None

    # -- lockout -----------------------------------------------------------
    def _locked(self, key: tuple[str, str], now: float) -> float:
        entry = self._failures.get(key)
        if entry is None:
            return 0.0
        remaining = entry.locked_until - now
        if remaining <= 0 and entry.locked_until:
            self._failures.pop(key, None)
            return 0.0
        return max(0.0, remaining)

    def register_failure(self, username: str, client_key: str) -> float:
        """Records a failed login; returns remaining lockout seconds (0 = not locked)."""
        key = (username.lower(), client_key)
        now = time.monotonic()
        if self._locked(key, now):
            return self.lockout_seconds
        entry = self._failures.setdefault(key, _Failures())
        entry.count += 1
        if entry.count >= self.max_attempts:
            entry.locked_until = now + self.lockout_seconds
            return self.lockout_seconds
        return 0.0

    def clear_failures(self, username: str, client_key: str) -> None:
        self._failures.pop((username.lower(), client_key), None)

    # -- login ---------------------------------------------------------------
    def login(self, username: str, password: str, *, client_key: str = "") -> tuple[str, dict[str, Any]]:
        """Verifies credentials; returns ``(raw_session_token, account_info)``.

        Raises :class:`AuthError` with a stable message key on every failure
        path — the error surface never distinguishes "no such user" from
        "wrong password".
        """
        username = (username or "").strip()
        if not username or not password:
            raise AuthError("invalid_credentials", "Invalid username or password.")

        lock_remaining = self._locked((username.lower(), client_key), time.monotonic())
        if lock_remaining:
            audit_record("auth.login.locked", username=username)
            raise AuthError("login_locked_out", "Too many failed attempts; try again later.")

        user = store.get_web_user(username)
        if user is None:
            # Burn comparable time so timing does not enumerate users.
            try:
                _hasher.verify(
                    "$argon2id$v=19$m=65536,t=3,p=4$"
                    "c29tZXNhbHRzb21lc2FsdA$3q2+7wKJ1ZSFVOA1VdQAO1Bh5N6hXq0LZ0F2W4+cxVg",
                    password,
                )
            except Exception:
                pass
            self.register_failure(username, client_key)
            raise AuthError("invalid_credentials", "Invalid username or password.")

        if user.get("disabled"):
            audit_record("auth.login.disabled", username=username)
            raise AuthError("account_disabled", "This account is disabled.")

        if not self.verify_password(str(user["password_hash"]), password):
            self.register_failure(username, client_key)
            audit_record("auth.login.failed", username=username)
            raise AuthError("invalid_credentials", "Invalid username or password.")

        if user.get("totp_enabled"):
            # Two-step: the caller must complete /auth/mfa with this token.
            pending = secrets.token_urlsafe(24)
            self._pending_mfa[pending] = (int(user["account_id"]), time.time() + 120.0)
            raise MFARequired(pending)

        self.clear_failures(username, client_key)
        account_id = int(user["account_id"])
        token = store.create_session(account_id)
        audit_record("auth.login.success", username=username, account_id=account_id)
        return token, {
            "account_id": account_id,
            "username": username,
            "role": str(user["role"]),
        }

    _pending_mfa: dict[str, tuple[int, float]] = field(default_factory=dict, repr=False)

    def verify_mfa(self, pending_token: str, code: str) -> tuple[str, dict[str, Any]]:
        """Completes a two-step login (TOTP second factor)."""
        entry = self._pending_mfa.pop(pending_token, None)
        if entry is None or time.time() > entry[1]:
            raise AuthError("invalid_credentials", "Login session expired; sign in again.")
        account_id, _ = entry
        user = store.get_web_user_by_account(account_id)
        if user is None or not user.get("totp_enabled") or not user.get("totp_secret"):
            raise AuthError("invalid_credentials", "Login session expired; sign in again.")
        if not verify_totp(str(user["totp_secret"]), code):
            audit_record("auth.mfa.failed", account_id=account_id)
            raise AuthError("invalid_credentials", "Invalid verification code.")
        token = store.create_session(account_id)
        audit_record("auth.login.success", username=str(user["username"]), account_id=account_id)
        return token, {
            "account_id": account_id,
            "username": str(user["username"]),
            "role": str(user.get("role", "read_only")),
        }

    # -- bootstrap -----------------------------------------------------------
    def setup_status(self) -> dict[str, Any]:
        return {
            "required": store.web_user_count() == 0,
            "token_required": bool(os.environ.get("TLA_SETUP_TOKEN")),
        }

    def bootstrap_owner(
        self, username: str, password: str, *, setup_token: str | None = None
    ) -> tuple[str, dict[str, Any]]:
        """Creates the first OWNER web account and returns a session token."""
        if store.web_user_count() > 0:
            raise AuthError("setup_unavailable", "Setup is already complete.")
        expected = os.environ.get("TLA_SETUP_TOKEN", "")
        if expected:
            if not setup_token or not hmac.compare_digest(setup_token, expected):
                audit_record("auth.setup.denied")
                raise AuthError("setup_unauthorized", "Invalid setup token.")
        reason = self.check_password_strength(password)
        if reason:
            raise AuthError(reason, "Password does not meet the strength requirements.")
        if not username or len(username) > 64 or not username.replace("_", "").replace("-", "").replace(".", "").isalnum():
            raise AuthError("invalid_username", "Username must be 1-64 safe characters.")

        account = store.ensure_account(store.KIND_WEB, f"web:{username}", display_name=username, role=ROLE_OWNER)
        store.create_web_user(account.id, username, self.hash_password(password))
        token = store.create_session(account.id)
        audit_record("auth.setup.completed", username=username, account_id=account.id)
        return token, {"account_id": account.id, "username": username, "role": ROLE_OWNER}


class MFARequired(AuthError):
    """Raised when the first login step succeeded but TOTP is required."""

    def __init__(self, pending_token: str) -> None:
        super().__init__("mfa_required", "Verification code required.")
        self.pending_token = pending_token


# ---------------------------------------------------------------------------
# TOTP (RFC 6238, SHA-1, 6 digits, 30s step — the standard authenticator apps)
# ---------------------------------------------------------------------------

def generate_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _totp_code(secret_b32: str, counter: int) -> str:
    padding = "=" * ((8 - len(secret_b32) % 8) % 8)
    key = base64.b32decode(secret_b32 + padding)
    msg = counter.to_bytes(8, "big")
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = (
        (digest[offset] & 0x7F) << 24
        | digest[offset + 1] << 16
        | digest[offset + 2] << 8
        | digest[offset + 3]
    )
    return f"{binary % 1_000_000:06d}"


def verify_totp(secret_b32: str, code: str, *, window: int = 1) -> bool:
    """Constant-window TOTP check (±1 step of 30s)."""
    code = (code or "").strip()
    if not (code.isdigit() and len(code) == 6) or not secret_b32:
        return False
    step = int(time.time()) // 30
    return any(hmac.compare_digest(_totp_code(secret_b32, step + d), code) for d in range(-window, window + 1))


def set_totp(account_id: int, secret: str | None, enabled: bool) -> None:
    """Stores/removes the TOTP secret on the web user of *account_id*."""
    with database.transaction() as conn:
        conn.execute(
            "UPDATE web_users SET totp_secret = ?, totp_enabled = ? WHERE account_id = ?",
            (secret, 1 if enabled else 0, account_id),
        )


def change_password(account_id: int, current: str, new: str, auth: AuthService) -> None:
    user = store.get_web_user_by_account(account_id)
    if user is None:
        raise AuthError("invalid_credentials", "No web credential on this account.")
    if not auth.verify_password(str(user["password_hash"]), current):
        audit_record("auth.password.change_denied", account_id=account_id)
        raise AuthError("invalid_credentials", "Current password is incorrect.")
    reason = auth.check_password_strength(new)
    if reason:
        raise AuthError(reason, "Password does not meet the strength requirements.")
    store.set_web_user_password(account_id, auth.hash_password(new))
    store.revoke_all_sessions(account_id)
    audit_record("auth.password.changed", account_id=account_id)


def promote_role(account_id: int, role: str) -> None:
    """Sets an account role and revokes its sessions (rotation on privilege change)."""
    if role not in VALID_ROLES:
        raise AuthError("invalid_role", "Unknown role.")
    store.set_account_role(account_id, role)
    store.revoke_all_sessions(account_id)
    audit_record("auth.role.changed", account_id=account_id, role=role)
