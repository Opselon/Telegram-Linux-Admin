"""Database helpers for the Telegram Linux Admin application."""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterable, Iterator

DEFAULT_DB_FILE = "database.db"

_conn: sqlite3.Connection | None = None
_conn_path: str | None = None
_conn_lock = threading.RLock()
_UNSET = object()


def _resolve_db_path() -> str:
    return os.environ.get("TLA_DB_FILE", DEFAULT_DB_FILE)


def _ensure_directory(path: str) -> None:
    directory = os.path.dirname(path)
    if directory and not os.path.exists(directory):
        os.makedirs(directory, exist_ok=True)


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(
        path,
        check_same_thread=False,
        detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
        isolation_level=None,  # autocommit mode for explicit transactions
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def get_db_connection() -> sqlite3.Connection:
    """Establishes and returns a cached database connection."""
    global _conn, _conn_path
    db_path = _resolve_db_path()
    with _conn_lock:
        if _conn is None or _conn_path != db_path:
            if _conn is not None:
                _conn.close()
            _ensure_directory(db_path)
            _conn = _connect(db_path)
            _conn_path = db_path
    return _conn  # type: ignore[return-value]


def close_db_connection() -> None:
    """Closes the database connection."""
    global _conn, _conn_path
    with _conn_lock:
        if _conn is not None:
            _conn.close()
            _conn = None
            _conn_path = None


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """Context manager that wraps operations in a transaction."""
    conn = get_db_connection()
    with _conn_lock:
        try:
            conn.execute("BEGIN")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def initialize_database() -> None:
    """Initializes the database schema."""
    with transaction() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS servers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alias TEXT NOT NULL UNIQUE,
                hostname TEXT NOT NULL,
                user TEXT NOT NULL,
                password TEXT,
                key_path TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                telegram_id INTEGER NOT NULL UNIQUE
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_servers_alias ON servers(alias)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_telegram_id ON users(telegram_id)"
        )


def add_server(alias: str, hostname: str, user: str, password: str | None = None, key_path: str | None = None) -> None:
    """Adds a new server definition to the database."""
    try:
        with transaction() as conn:
            conn.execute(
                "INSERT INTO servers (alias, hostname, user, password, key_path) VALUES (?, ?, ?, ?, ?)",
                (alias, hostname, user, password, key_path),
            )
    except sqlite3.IntegrityError as exc:
        raise ValueError(f"Server with alias '{alias}' already exists.") from exc


def update_server(
    alias: str,
    *,
    hostname: str | None = _UNSET,
    user: str | None = _UNSET,
    password: str | None = _UNSET,
    key_path: str | None = _UNSET,
) -> bool:
    """Updates an existing server entry. Returns True if a row was modified."""
    fields: list[str] = []
    params: list[Any] = []

    for name, value in (
        ("hostname", hostname),
        ("user", user),
        ("password", password),
        ("key_path", key_path),
    ):
        if value is _UNSET:
            continue
        fields.append(f"{name} = ?")
        params.append(value)

    if not fields:
        return False

    params.append(alias)
    with transaction() as conn:
        cursor = conn.execute(
            f"UPDATE servers SET {', '.join(fields)} WHERE alias = ?",
            params,
        )
    return cursor.rowcount > 0


def remove_server(alias: str) -> None:
    """Removes a server from the database."""
    with transaction() as conn:
        conn.execute("DELETE FROM servers WHERE alias = ?", (alias,))


def get_server(alias: str) -> dict[str, Any] | None:
    """Fetches a server by alias."""
    conn = get_db_connection()
    row = conn.execute("SELECT * FROM servers WHERE alias = ?", (alias,)).fetchone()
    return dict(row) if row else None


def get_all_servers() -> list[dict[str, Any]]:
    """Retrieves all servers from the database."""
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM servers ORDER BY alias ASC").fetchall()
    return [dict(row) for row in rows]


def add_user(telegram_id: int) -> None:
    """Adds a new whitelisted user."""
    try:
        with transaction() as conn:
            conn.execute("INSERT INTO users (telegram_id) VALUES (?)", (telegram_id,))
    except sqlite3.IntegrityError:
        # Duplicate entries are ignored silently to keep idempotent behaviour.
        pass


def remove_user(telegram_id: int) -> None:
    """Removes a whitelisted user."""
    with transaction() as conn:
        conn.execute("DELETE FROM users WHERE telegram_id = ?", (telegram_id,))


def get_whitelisted_users() -> list[int]:
    """Retrieves all whitelisted user IDs."""
    conn = get_db_connection()
    rows = conn.execute("SELECT telegram_id FROM users ORDER BY telegram_id ASC").fetchall()
    return [row["telegram_id"] for row in rows]


def seed_users(user_ids: Iterable[int]) -> None:
    """Replaces the whitelist with a new set of user IDs."""
    unique_ids = list(dict.fromkeys(user_ids))
    with transaction() as conn:
        conn.execute("DELETE FROM users")
        conn.executemany(
            "INSERT INTO users (telegram_id) VALUES (?)",
            ((user_id,) for user_id in unique_ids),
        )

