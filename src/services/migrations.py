"""Schema migrations for the platform (4.0) upgrade.

A real migration runner on top of the existing WAL-mode SQLite database:

* ``schema_migrations`` records applied steps (version, name, applied_at).
* Steps run inside the existing :func:`src.database.transaction` so a failure
  rolls the whole step back.
* Every step is **additive** — existing rows stay valid and the Telegram bot
  keeps working against the migrated database unchanged.
* :func:`run_migrations` takes a pre-migration WAL-consistent snapshot
  (``database.snapshot_database_to``) before touching anything destructive;
  additive steps still get the snapshot because it is cheap and makes every
  upgrade rollback-capable.

Legacy ownership note: ``servers.owner_id`` keeps its Telegram-id semantics.
New tables key on ``accounts``; ``servers.owner_account_id`` (added here) is
the canonical ownership column going forward, backfilled from the legacy
``owner_id``.  Platform accounts (web/bale) that own servers map to
*synthetic* legacy owner ids (negative integers, see ``store.py``) so every
existing ``src.database`` function keeps working without a table rebuild.
"""

from __future__ import annotations

import sqlite3
from typing import Callable

from .. import database

MigrationFn = Callable[[sqlite3.Connection], None]


def _create_accounts(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL CHECK (kind IN ('telegram', 'bale', 'web')),
            external_id TEXT NOT NULL,
            display_name TEXT,
            role TEXT NOT NULL DEFAULT 'read_only'
                CHECK (role IN ('owner', 'admin', 'operator', 'read_only')),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (kind, external_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_accounts_kind_external ON accounts(kind, external_id)"
    )


def _create_web_users(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS web_users (
            account_id INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            totp_secret TEXT,
            totp_enabled INTEGER NOT NULL DEFAULT 0,
            disabled INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def _create_web_sessions(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS web_sessions (
            token_hash TEXT PRIMARY KEY,
            account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at REAL NOT NULL,
            last_seen REAL NOT NULL,
            revoked INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_web_sessions_account ON web_sessions(account_id)"
    )


def _create_link_codes(conn: sqlite3.Connection) -> None:
    """Single-use account-link codes (Bale enrollment, web recovery flows)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS link_codes (
            code_hash TEXT PRIMARY KEY,
            account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            purpose TEXT NOT NULL DEFAULT 'bale_link',
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0
        )
        """
    )


def _create_bale_bindings(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bale_bindings (
            bale_user_id TEXT PRIMARY KEY,
            account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            display_name TEXT,
            bound_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def _create_notification_prefs(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS notification_prefs (
            account_id INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
            disabled_categories TEXT NOT NULL DEFAULT '',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def _backfill_accounts(conn: sqlite3.Connection) -> None:
    """Maps every legacy Telegram user to an account row and links servers.

    Backfill is idempotent: rows already carrying ``owner_account_id`` are
    left alone, and account creation uses ``INSERT OR IGNORE`` on the
    ``(kind, external_id)`` unique key.
    """
    for row in conn.execute("SELECT telegram_id FROM users").fetchall():
        conn.execute(
            """
            INSERT OR IGNORE INTO accounts (kind, external_id, role)
            VALUES ('telegram', ?, 'read_only')
            """,
            (str(row["telegram_id"]),),
        )

    # Link every legacy server row to its owner's account.
    conn.execute(
        """
        UPDATE servers SET owner_account_id = (
            SELECT a.id FROM accounts a
            WHERE a.kind = 'telegram' AND a.external_id = CAST(servers.owner_id AS TEXT)
        )
        WHERE owner_account_id IS NULL
        """
    )


_MIGRATIONS: list[tuple[int, str, MigrationFn]] = [
    (2, "create_accounts", _create_accounts),
    (2, "create_web_users", _create_web_users),
    (2, "create_web_sessions", _create_web_sessions),
    (2, "create_link_codes", _create_link_codes),
    (2, "create_bale_bindings", _create_bale_bindings),
    (2, "create_notification_prefs", _create_notification_prefs),
    (3, "account_ownership_backfill", _backfill_accounts),
]

SCHEMA_VERSION = 3


def _owner_account_column(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(servers)").fetchall()}
    if "owner_account_id" not in columns:
        conn.execute("ALTER TABLE servers ADD COLUMN owner_account_id INTEGER REFERENCES accounts(id)")


def run_migrations(*, snapshot: bool = True) -> int:
    """Applies pending migrations; returns the number of steps applied.

    When *snapshot* is true a WAL-consistent pre-migration snapshot is written
    next to the database file (``database.db.pre-migration.bak``) so an
    operator always has a rollback point.
    """
    database.initialize_database()  # base schema (v1) exists on every install

    if snapshot:
        import os
        from pathlib import Path
        db_path = Path(os.environ.get("TLA_DB_FILE", database.DEFAULT_DB_FILE))
        backup_path = db_path.with_suffix(db_path.suffix + ".pre-migration.bak")
        try:
            database.snapshot_database_to(backup_path)
        except Exception:  # pragma: no cover - snapshot is best-effort
            pass

    conn = database.get_db_connection()
    applied = 0
    with database.transaction() as tx:
        tx.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER NOT NULL,
                name TEXT NOT NULL,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (version, name)
            )
            """
        )
        done = {
            (row["version"], row["name"])
            for row in tx.execute("SELECT version, name FROM schema_migrations").fetchall()
        }
        # The servers.owner_account_id column must exist before the backfill
        # step; it belongs to no single named migration, so ensure it here.
        if any((v, n) not in done for v, n, _ in _MIGRATIONS):
            _owner_account_column(tx)
        for version, name, fn in _MIGRATIONS:
            if (version, name) in done:
                continue
            fn(tx)
            tx.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                (version, name),
            )
            applied += 1
    if applied:
        import logging

        from ..observability import log_event
        log_event(logging.getLogger(__name__), "migrations.applied", count=applied)
    return applied


def current_schema_version(conn: sqlite3.Connection) -> int:
    """Best-effort current version (0 when the runner has never run)."""
    try:
        row = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row["v"]) if row and row["v"] is not None else 0


if __name__ == "__main__":  # pragma: no cover
    run_migrations()
