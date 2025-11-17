"""Database helpers for the Telegram Linux Admin application."""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterable, Iterator
from pathlib import Path

from .security import decrypt_secret, encrypt_secret

DEFAULT_DB_FILE = "database.db"

DEFAULT_PLAN = "free"
PLAN_LIMITS = {
    "free": 3,
    "premium": 10,
}
VALID_PLANS = frozenset(PLAN_LIMITS)

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

def checkpoint() -> None:
    """Forces a WAL checkpoint to flush writes to the main DB file."""
    conn = get_db_connection()
    with _conn_lock:
        try:
            # FULL mode is aggressive and ensures all data is written.
            conn.execute("PRAGMA wal_checkpoint(FULL)").fetchall()
        except sqlite3.OperationalError:
            # This can happen if the DB is not in WAL mode, which is fine.
            pass

def get_db_path() -> Path:
    """Returns the path to the database file."""
    return Path(_resolve_db_path())

def reset_connection() -> None:
    """Close and clear the cached connection so future queries see a new DB file."""
    try:
        conn = get_db_connection()
        conn.close()
    except Exception:
        # Safe to ignore; maybe the connection was never created yet
        pass
    global _conn, _conn_path
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


def _get_table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"] for row in rows}


def _ensure_column(conn: sqlite3.Connection, table: str, column_def: str, column_name: str) -> None:
    columns = _get_table_columns(conn, table)
    if column_name not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_def}")


def _encrypt_value(value: str | None) -> str | None:
    encrypted = encrypt_secret(value)
    return encrypted.decode("utf-8") if encrypted else None


def _decrypt_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        data = value.encode("utf-8")
    else:
        data = value
    return decrypt_secret(data)


def _plan_limit(plan: str | None) -> int:
    return PLAN_LIMITS.get(plan or DEFAULT_PLAN, PLAN_LIMITS[DEFAULT_PLAN])


def _ensure_user(conn: sqlite3.Connection, telegram_id: int, plan: str | None = None) -> None:
    if plan is None:
        conn.execute(
            """
            INSERT INTO users (telegram_id)
            VALUES (?)
            ON CONFLICT(telegram_id) DO NOTHING
            """,
            (telegram_id,),
        )
    else:
        conn.execute(
            """
            INSERT INTO users (telegram_id, plan)
            VALUES (?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET plan = excluded.plan
            """,
            (telegram_id, plan),
        )


def _get_user_plan(conn: sqlite3.Connection, telegram_id: int) -> str:
    row = conn.execute(
        "SELECT plan FROM users WHERE telegram_id = ?",
        (telegram_id,),
    ).fetchone()
    return row["plan"] if row else DEFAULT_PLAN

def initialize_database() -> None:
    """Initializes the database schema."""
    with transaction() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                telegram_id INTEGER NOT NULL UNIQUE,
                plan TEXT NOT NULL DEFAULT 'free'
            )
            """
        )
        _ensure_column(conn, "users", "plan TEXT NOT NULL DEFAULT 'free'", "plan")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_telegram_id ON users(telegram_id)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS servers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL,
                alias TEXT NOT NULL,
                hostname TEXT NOT NULL,
                user TEXT NOT NULL,
                password TEXT,
                key_path TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(owner_id) REFERENCES users(telegram_id) ON DELETE CASCADE,
                UNIQUE(owner_id, alias)
            )
            """
        )
        _ensure_column(conn, "servers", "owner_id INTEGER NOT NULL DEFAULT 0", "owner_id")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_servers_owner_alias ON servers(owner_id, alias)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_preferences (
                telegram_id INTEGER PRIMARY KEY,
                language TEXT NOT NULL DEFAULT 'en'
            )
            """
        )


def add_server(
    owner_id: int,
    alias: str,
    hostname: str,
    user: str,
    password: str | None = None,
    key_path: str | None = None,
) -> None:
    """Adds a new server definition scoped to a Telegram user."""
    if not isinstance(owner_id, int):
        raise TypeError("owner_id must be an integer Telegram user id.")

    encrypted_password = _encrypt_value(password)
    encrypted_key = _encrypt_value(key_path)

    try:
        with transaction() as conn:
            _ensure_user(conn, owner_id)
            plan = _get_user_plan(conn, owner_id)
            current = conn.execute(
                "SELECT COUNT(*) AS count FROM servers WHERE owner_id = ?",
                (owner_id,),
            ).fetchone()
            limit = _plan_limit(plan)
            if current and current["count"] >= limit:
                raise ValueError(
                    f"Server limit reached for plan '{plan}'. Maximum allowed is {limit}."
                )
            conn.execute(
                """
                INSERT INTO servers (owner_id, alias, hostname, user, password, key_path)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (owner_id, alias, hostname, user, encrypted_password, encrypted_key),
            )
    except sqlite3.IntegrityError as exc:
        raise ValueError(
            f"Server with alias '{alias}' already exists for this user."
        ) from exc


def update_server(
    owner_id: int,
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
        if name in {"password", "key_path"}:
            value = _encrypt_value(value)
        fields.append(f"{name} = ?")
        params.append(value)

    if not fields:
        return False

    params.extend([owner_id, alias])
    with transaction() as conn:
        cursor = conn.execute(
            f"UPDATE servers SET {', '.join(fields)} WHERE owner_id = ? AND alias = ?",
            params,
        )
    return cursor.rowcount > 0


def remove_server(owner_id: int, alias: str) -> bool:
    """Removes a server owned by the specified user."""
    with transaction() as conn:
        cursor = conn.execute(
            "DELETE FROM servers WHERE owner_id = ? AND alias = ?",
            (owner_id, alias),
        )
    return cursor.rowcount > 0


def get_server(owner_id: int, alias: str) -> dict[str, Any] | None:
    """Fetches a server owned by a specific user."""
    conn = get_db_connection()
    row = conn.execute(
        "SELECT * FROM servers WHERE owner_id = ? AND alias = ?",
        (owner_id, alias),
    ).fetchone()
    if not row:
        return None
    data = dict(row)
    data["password"] = _decrypt_value(data.get("password"))
    data["key_path"] = _decrypt_value(data.get("key_path"))
    data["owner_id"] = owner_id
    return data


def get_all_servers(owner_id: int | None = None) -> list[dict[str, Any]]:
    """Retrieves servers, optionally filtered by Telegram user id."""
    conn = get_db_connection()
    if owner_id is None:
        rows = conn.execute(
            "SELECT * FROM servers ORDER BY owner_id ASC, alias ASC"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM servers WHERE owner_id = ? ORDER BY alias ASC",
            (owner_id,),
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        data = dict(row)
        data["password"] = _decrypt_value(data.get("password"))
        data["key_path"] = _decrypt_value(data.get("key_path"))
        result.append(data)
    return result


def add_user(telegram_id: int, plan: str | None = None) -> None:
    """Adds or updates a whitelisted user with an optional subscription plan."""
    if plan and plan not in VALID_PLANS:
        raise ValueError(f"Unknown plan '{plan}'.")
    with transaction() as conn:
        _ensure_user(conn, telegram_id, plan)


def remove_user(telegram_id: int) -> None:
    """Removes a whitelisted user."""
    with transaction() as conn:
        conn.execute("DELETE FROM servers WHERE owner_id = ?", (telegram_id,))
        conn.execute("DELETE FROM users WHERE telegram_id = ?", (telegram_id,))


def get_whitelisted_users() -> list[int]:
    """Retrieves all whitelisted user IDs."""
    conn = get_db_connection()
    rows = conn.execute("SELECT telegram_id FROM users ORDER BY telegram_id ASC").fetchall()
    return [row["telegram_id"] for row in rows]


def set_user_plan(telegram_id: int, plan: str) -> None:
    """Assigns a subscription plan to a user."""
    if plan not in VALID_PLANS:
        raise ValueError(f"Unknown plan '{plan}'.")
    with transaction() as conn:
        _ensure_user(conn, telegram_id, plan)


def get_user_plan(telegram_id: int) -> str:
    """Returns the stored subscription plan for a user."""
    conn = get_db_connection()
    return _get_user_plan(conn, telegram_id)


def get_user_server_limit(telegram_id: int) -> int:
    """Resolves the server limit for the user's current plan."""
    return _plan_limit(get_user_plan(telegram_id))


def get_user_server_count(telegram_id: int) -> int:
    """Counts how many servers a user has registered."""
    conn = get_db_connection()
    row = conn.execute(
        "SELECT COUNT(*) AS count FROM servers WHERE owner_id = ?",
        (telegram_id,),
    ).fetchone()
    return row["count"] if row else 0


def set_user_language_preference(telegram_id: int, language: str) -> None:
    """Stores the preferred language for a Telegram user."""
    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO user_preferences (telegram_id, language)
            VALUES (?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET language = excluded.language
            """,
            (telegram_id, language),
        )


def get_user_language_preference(telegram_id: int) -> str | None:
    """Fetches the preferred language for a Telegram user."""
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT language FROM user_preferences WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        # Gracefully recover in environments where the schema was not initialized yet
        # (e.g., unit tests that import handlers directly).
        if "no such table" not in str(exc):
            raise
        initialize_database()
        row = conn.execute(
            "SELECT language FROM user_preferences WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
    return row["language"] if row else None


def seed_users(user_ids: Iterable[int]) -> None:
    """Replaces the whitelist with a new set of user IDs."""
    unique_ids = list(dict.fromkeys(user_ids))
    with transaction() as conn:
        conn.execute("DELETE FROM servers")
        conn.execute("DELETE FROM users")
        for user_id in unique_ids:
            _ensure_user(conn, user_id, DEFAULT_PLAN)
