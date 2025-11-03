import sqlite3
import os

DB_FILE = 'database.db'
_conn = None

def get_db_connection():
    """Establishes and returns a singleton database connection."""
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
    return _conn

def close_db_connection():
    """Closes the database connection."""
    global _conn
    if _conn:
        _conn.close()
        _conn = None

def initialize_database():
    """Initializes the database and creates tables if they don't exist."""
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS servers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        alias TEXT NOT NULL UNIQUE,
        hostname TEXT NOT NULL,
        user TEXT NOT NULL,
        password TEXT,
        key_path TEXT
    )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        telegram_id INTEGER NOT NULL UNIQUE
    )
    """)
    conn.commit()

def add_server(alias, hostname, user, password=None, key_path=None):
    """Adds a new server to the database."""
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO servers (alias, hostname, user, password, key_path) VALUES (?, ?, ?, ?, ?)",
            (alias, hostname, user, password, key_path)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        raise ValueError(f"Server with alias '{alias}' already exists.")

def remove_server(alias):
    """Removes a server from the database."""
    conn = get_db_connection()
    conn.execute("DELETE FROM servers WHERE alias = ?", (alias,))
    conn.commit()

def get_all_servers():
    """Retrieves all servers from the database."""
    conn = get_db_connection()
    servers = conn.execute("SELECT * FROM servers").fetchall()
    return [dict(row) for row in servers]

def add_user(telegram_id):
    """Adds a new whitelisted user."""
    conn = get_db_connection()
    try:
        conn.execute("INSERT INTO users (telegram_id) VALUES (?)", (telegram_id,))
        conn.commit()
    except sqlite3.IntegrityError:
        pass  # User already exists

def get_whitelisted_users():
    """Retrieves all whitelisted user IDs."""
    conn = get_db_connection()
    users = conn.execute("SELECT telegram_id FROM users").fetchall()
    return [row['telegram_id'] for row in users]
