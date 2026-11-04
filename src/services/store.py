"""Account store: the platform-neutral identity layer.

Every human identity — Telegram user, Bale user, web login — becomes a row in
``accounts``.  Roles live on the account; web logins in ``web_users``; Bale
bindings in ``bale_bindings``.

Legacy bridge: ``src.database`` keys ownership on ``owner_id`` (a Telegram id)
and every existing function keeps that signature.  Platform accounts (web,
bale) therefore map to **synthetic negative owner ids**: ``owner_id =
-(account_id)``.  A matching ``users`` row with ``telegram_id = owner_id`` is
created so the foreign key and the plan limits keep working.  The negative
space is reserved — Telegram ids are always positive, so no collision is
possible.

Telegram accounts bridge the other way: their ``owner_id`` is the real
Telegram id, so all existing bot paths are untouched.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from .. import database
from ..access_control import permissions as telegram_permissions
from .migrations import run_migrations
from .permissions import (
    ROLE_ADMIN,
    ROLE_READONLY,
    VALID_ROLES,
    Actor,
    permissions_for_role,
)

KIND_TELEGRAM = "telegram"
KIND_BALE = "bale"
KIND_WEB = "web"


@dataclass(frozen=True)
class Account:
    id: int
    kind: str
    external_id: str
    display_name: str | None
    role: str
    owner_id: int


class StoreError(RuntimeError):
    """Raised when the account store cannot satisfy a request."""


def _account_from_row(row: sqlite3.Row | None) -> Account | None:
    if row is None:
        return None
    account_id = int(row["id"])
    kind = str(row["kind"])
    external_id = str(row["external_id"])
    owner_id = int(external_id) if kind == KIND_TELEGRAM else -account_id
    return Account(
        id=account_id,
        kind=kind,
        external_id=external_id,
        display_name=row["display_name"],
        role=str(row["role"]),
        owner_id=owner_id,
    )


def _owner_row_for(account: Account) -> int:
    """Legacy ``owner_id`` for *account*, creating the synthetic users row."""
    if account.kind == KIND_TELEGRAM:
        database.add_user(int(account.external_id))
        return int(account.external_id)
    if account.owner_id < 0:
        database.add_user(account.owner_id)
    return account.owner_id


def ensure_account(kind: str, external_id: str, *, display_name: str | None = None,
                   role: str | None = None) -> Account:
    """Returns the account for *(kind, external_id)*, creating it if needed.

    Telegram accounts resolve their role dynamically from the live
    PermissionManager (admins → admin, whitelisted/open → operator); the
    stored role is the fallback for platform accounts.
    """
    if kind not in {KIND_TELEGRAM, KIND_BALE, KIND_WEB}:
        raise StoreError(f"unknown account kind '{kind}'")
    if not external_id:
        raise StoreError("external_id must be a non-empty string")
    if role is not None and role not in VALID_ROLES:
        raise StoreError(f"unknown role '{role}'")

    conn = database.get_db_connection()
    with database.transaction() as tx:
        tx.execute(
            """
            INSERT OR IGNORE INTO accounts (kind, external_id, display_name, role)
            VALUES (?, ?, ?, ?)
            """,
            (kind, external_id, display_name, role or ROLE_READONLY),
        )
        row = tx.execute(
            "SELECT * FROM accounts WHERE kind = ? AND external_id = ?",
            (kind, external_id),
        ).fetchone()
    account = _account_from_row(row)
    assert account is not None  # INSERT OR IGNORE + SELECT cannot race within the lock
    if display_name and row["display_name"] != display_name:
        with database.transaction() as tx:
            tx.execute(
                "UPDATE accounts SET display_name = ? WHERE id = ?",
                (display_name, account.id),
            )
    if kind == KIND_TELEGRAM:
        database.add_user(int(external_id))
    return account


def get_account(account_id: int) -> Account | None:
    conn = database.get_db_connection()
    return _account_from_row(
        conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    )


def set_account_role(account_id: int, role: str) -> None:
    if role not in VALID_ROLES:
        raise StoreError(f"unknown role '{role}'")
    with database.transaction() as conn:
        cursor = conn.execute(
            "UPDATE accounts SET role = ? WHERE id = ?", (role, account_id)
        )
        if cursor.rowcount == 0:
            raise StoreError(f"account {account_id} not found")


def list_accounts() -> list[Account]:
    conn = database.get_db_connection()
    rows = conn.execute("SELECT * FROM accounts ORDER BY id ASC").fetchall()
    accounts = [a for a in (_account_from_row(r) for r in rows) if a is not None]
    return accounts


def delete_account(account_id: int) -> bool:
    with database.transaction() as conn:
        cursor = conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    return cursor.rowcount > 0


def actor_from_account(account: Account) -> Actor:
    """Builds the enforcement :class:`Actor` for *account*.

    Telegram accounts take their effective role from the live
    PermissionManager (single source of truth for the bot): admins are
    ``admin``, users who pass ``check()`` are ``operator``, everyone else is
    ``read_only``.  Platform accounts use their stored role.
    """
    if account.kind == KIND_TELEGRAM:
        telegram_id = int(account.external_id)
        if telegram_permissions.is_admin(telegram_id):
            role = ROLE_ADMIN
        elif telegram_permissions.check(telegram_id).allowed:
            role = "operator"
        else:
            role = ROLE_READONLY
    else:
        role = account.role
    return Actor(
        account_id=account.id,
        kind=account.kind,
        display_name=account.display_name or account.external_id,
        role=role,
        owner_id=_owner_row_for(account),
        permissions=permissions_for_role(role),
    )


def actor_for_telegram(telegram_id: int, display_name: str | None = None) -> Actor | None:
    """Actor for a Telegram user, or None when access control denies them."""
    account = ensure_account(
        KIND_TELEGRAM, str(telegram_id), display_name=display_name
    )
    actor = actor_from_account(account)
    if actor.role == ROLE_READONLY and not telegram_permissions.check(telegram_id).allowed:
        return None
    return actor


# ---------------------------------------------------------------------------
# Web users (Argon2id credentials live in the auth service; this is storage)
# ---------------------------------------------------------------------------

def create_web_user(account_id: int, username: str, password_hash: str) -> None:
    with database.transaction() as conn:
        conn.execute(
            """
            INSERT INTO web_users (account_id, username, password_hash)
            VALUES (?, ?, ?)
            """,
            (account_id, username, password_hash),
        )


def get_web_user(username: str) -> dict[str, Any] | None:
    conn = database.get_db_connection()
    row = conn.execute(
        """
        SELECT w.*, a.role, a.kind, a.external_id, a.display_name
        FROM web_users w JOIN accounts a ON a.id = w.account_id
        WHERE w.username = ?
        """,
        (username,),
    ).fetchone()
    return dict(row) if row else None


def get_web_user_by_account(account_id: int) -> dict[str, Any] | None:
    conn = database.get_db_connection()
    row = conn.execute(
        "SELECT * FROM web_users WHERE account_id = ?", (account_id,)
    ).fetchone()
    return dict(row) if row else None


def set_web_user_password(account_id: int, password_hash: str) -> bool:
    with database.transaction() as conn:
        cursor = conn.execute(
            "UPDATE web_users SET password_hash = ? WHERE account_id = ?",
            (password_hash, account_id),
        )
    return cursor.rowcount > 0


def set_web_user_disabled(account_id: int, disabled: bool) -> bool:
    with database.transaction() as conn:
        cursor = conn.execute(
            "UPDATE web_users SET disabled = ? WHERE account_id = ?",
            (1 if disabled else 0, account_id),
        )
    return cursor.rowcount > 0


def web_user_count() -> int:
    conn = database.get_db_connection()
    row = conn.execute("SELECT COUNT(*) AS c FROM web_users").fetchone()
    return int(row["c"])


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

SESSION_IDLE_TTL = 12 * 3600.0
SESSION_ABSOLUTE_TTL = 7 * 24 * 3600.0

# Process-local key for session-token hashing (see _hash_token). Regenerated
# per process, so tokens from a previous run simply fail to resolve — which is
# the safe direction: a restarted service drops its sessions rather than
# honoring hashes an attacker could have computed from a stolen DB.
_TOKEN_HMAC_KEY = os.urandom(32)


def _hash_token(token: str) -> str:
    """Hashes a session token for storage.

    Plain SHA-256 is the wrong shape for credential-like data: an attacker who
    reads the DB can brute-force or rainbow-table low-entropy tokens offline.
    Keying the digest with a process-local random key (HMAC) makes the stored
    hashes useless outside this process without changing the lookup contract:
    the token is still only ever compared as a hash, and only this process can
    produce one that matches.
    """
    return hmac.new(_TOKEN_HMAC_KEY, token.encode("utf-8"), hashlib.sha256).hexdigest()


def create_session(account_id: int, *, ttl: float = SESSION_IDLE_TTL) -> str:
    """Creates a session; returns the raw token (cookie value, shown once)."""
    token = secrets.token_urlsafe(32)
    now = time.time()
    with database.transaction() as conn:
        conn.execute(
            """
            INSERT INTO web_sessions (token_hash, account_id, expires_at, last_seen)
            VALUES (?, ?, ?, ?)
            """,
            (_hash_token(token), account_id, now + SESSION_ABSOLUTE_TTL, now),
        )
    return token


def resolve_session(token: str | None) -> Account | None:
    """Returns the account for a valid, unexpired, unrevoked session token."""
    if not token:
        return None
    token_hash = _hash_token(token)
    now = time.time()
    conn = database.get_db_connection()
    row = conn.execute(
        """
        SELECT s.expires_at, s.last_seen, a.id AS a_id, a.kind, a.external_id,
               a.display_name, a.role
        FROM web_sessions s JOIN accounts a ON a.id = s.account_id
        WHERE s.token_hash = ? AND s.revoked = 0
        """,
        (token_hash,),
    ).fetchone()
    if row is None:
        return None
    if now >= float(row["expires_at"]):
        return None
    if now - float(row["last_seen"]) > SESSION_IDLE_TTL:
        # Idle expiry: the absolute deadline has not passed but the session
        # has gone stale; treat it as invalid (and let cleanup drop it).
        return None
    with database.transaction() as c:
        c.execute(
            "UPDATE web_sessions SET last_seen = ? WHERE token_hash = ?",
            (now, token_hash),
        )
    return _account_from_row(
        {
            "id": row["a_id"],
            "kind": row["kind"],
            "external_id": row["external_id"],
            "display_name": row["display_name"],
            "role": row["role"],
        }  # type: ignore[arg-type]
    )


def revoke_session(token: str) -> bool:
    with database.transaction() as conn:
        cursor = conn.execute(
            "UPDATE web_sessions SET revoked = 1 WHERE token_hash = ?",
            (_hash_token(token),),
        )
    return cursor.rowcount > 0


def revoke_all_sessions(account_id: int) -> int:
    """Revokes every session for *account_id* (privilege changes, password reset)."""
    with database.transaction() as conn:
        cursor = conn.execute(
            "UPDATE web_sessions SET revoked = 1 WHERE account_id = ?", (account_id,)
        )
    return cursor.rowcount


def cleanup_sessions(now: float | None = None) -> int:
    """Deletes expired/revoked sessions; returns rows removed."""
    now = now if now is not None else time.time()
    with database.transaction() as conn:
        cursor = conn.execute(
            """
            DELETE FROM web_sessions
            WHERE expires_at < ? OR revoked = 1 OR last_seen < ?
            """,
            (now, now - SESSION_IDLE_TTL),
        )
    return cursor.rowcount


# ---------------------------------------------------------------------------
# Account link codes (Bale enrollment and similar flows)
# ---------------------------------------------------------------------------

LINK_CODE_TTL = 5 * 60.0
LINK_CODE_MAX_ATTEMPTS = 5


def create_link_code(account_id: int, *, purpose: str = "bale_link",
                     ttl: float = LINK_CODE_TTL) -> str:
    """Creates a single-use link code; the code is returned exactly once."""
    code = "".join(secrets.choice("23456789ABCDEFGHJKMNPQRSTUVWXYZ") for _ in range(6))
    now = time.time()
    with database.transaction() as conn:
        conn.execute(
            """
            INSERT INTO link_codes (code_hash, account_id, purpose, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (hashlib.sha256(code.encode()).hexdigest(), account_id, purpose, now, now + ttl),
        )
    return code


def consume_link_code(code: str, *, purpose: str = "bale_link") -> Account | None:
    """Single-use consumption with attempt counting and constant-time compare."""
    if not code:
        return None
    code_hash = hashlib.sha256(code.strip().upper().encode()).hexdigest()
    now = time.time()
    conn = database.get_db_connection()
    row = conn.execute(
        "SELECT * FROM link_codes WHERE code_hash = ?", (code_hash,)
    ).fetchone()
    if row is None or row["purpose"] != purpose:
        return None
    if now >= float(row["expires_at"]):
        with database.transaction() as c:
            c.execute("DELETE FROM link_codes WHERE code_hash = ?", (code_hash,))
        return None
    if int(row["attempts"]) >= LINK_CODE_MAX_ATTEMPTS:
        return None
    account = get_account(int(row["account_id"]))
    with database.transaction() as c:
        c.execute("DELETE FROM link_codes WHERE code_hash = ?", (code_hash,))
    return account


# ---------------------------------------------------------------------------
# Bale bindings
# ---------------------------------------------------------------------------

def bind_bale_user(bale_user_id: str, account_id: int, display_name: str | None = None) -> None:
    with database.transaction() as conn:
        conn.execute(
            """
            INSERT INTO bale_bindings (bale_user_id, account_id, display_name)
            VALUES (?, ?, ?)
            ON CONFLICT(bale_user_id) DO UPDATE SET
                account_id = excluded.account_id,
                display_name = excluded.display_name
            """,
            (str(bale_user_id), account_id, display_name),
        )


def get_bale_binding(bale_user_id: str) -> Account | None:
    conn = database.get_db_connection()
    row = conn.execute(
        "SELECT account_id FROM bale_bindings WHERE bale_user_id = ?",
        (str(bale_user_id),),
    ).fetchone()
    if row is None:
        return None
    account = get_account(int(row["account_id"]))
    if account is None:
        return None
    _owner_row_for(account)
    return account


def unbind_bale_user(bale_user_id: str) -> bool:
    with database.transaction() as conn:
        cursor = conn.execute(
            "DELETE FROM bale_bindings WHERE bale_user_id = ?", (str(bale_user_id),)
        )
    return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# Notification preferences
# ---------------------------------------------------------------------------

def get_disabled_categories(account_id: int) -> set[str]:
    conn = database.get_db_connection()
    row = conn.execute(
        "SELECT disabled_categories FROM notification_prefs WHERE account_id = ?",
        (account_id,),
    ).fetchone()
    if row is None or not row["disabled_categories"]:
        return set()
    return {c.strip() for c in str(row["disabled_categories"]).split(",") if c.strip()}


def set_disabled_categories(account_id: int, categories: set[str]) -> None:
    with database.transaction() as conn:
        conn.execute(
            """
            INSERT INTO notification_prefs (account_id, disabled_categories, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(account_id) DO UPDATE SET
                disabled_categories = excluded.disabled_categories,
                updated_at = CURRENT_TIMESTAMP
            """,
            (account_id, ",".join(sorted(categories))),
        )
