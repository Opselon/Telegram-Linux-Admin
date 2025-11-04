"""Tests for the database helper module."""

import sqlite3
import threading

import pytest

from src import database


@pytest.fixture(autouse=True)
def mock_db_connection(monkeypatch):
    """Provides an isolated in-memory database for each test."""
    conn = sqlite3.connect(
        ":memory:", check_same_thread=False, isolation_level=None
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    # Create tables expected by the module.
    conn.execute(
        """
        CREATE TABLE servers (
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
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            telegram_id INTEGER NOT NULL UNIQUE
        )
        """
    )

    # Patch the module-level connection utilities.
    monkeypatch.setattr(database, "get_db_connection", lambda: conn)
    monkeypatch.setattr(database, "_conn_lock", threading.RLock())

    yield conn

    conn.close()


def test_add_and_get_servers():
    """Test adding and retrieving servers."""
    database.add_server("test1", "host1", "user1", key_path="/path1")
    database.add_server("test2", "host2", "user2", password="pw")

    servers = database.get_all_servers()
    assert len(servers) == 2
    assert servers[0]['key_path'] == '/path1'
    assert servers[1]['password'] == 'pw'


def test_add_duplicate_server():
    """Test that adding a duplicate server raises a ValueError."""
    database.add_server("test1", "host1", "user1", key_path="/path1")
    with pytest.raises(ValueError, match="Server with alias 'test1' already exists."):
        database.add_server("test1", "host2", "user2", password="pw")


def test_add_and_get_users():
    """Test adding and retrieving whitelisted users."""
    database.add_user(123)
    database.add_user(456)

    users = database.get_whitelisted_users()
    assert len(users) == 2
    assert 123 in users
    assert 456 in users


def test_add_duplicate_user():
    """Test that adding a duplicate user does not raise an error and does not create a duplicate."""
    database.add_user(123)
    database.add_user(123)

    users = database.get_whitelisted_users()
    assert len(users) == 1


def test_remove_server():
    """Test removing a server."""
    database.add_server("test1", "host1", "user1")
    database.add_server("test2", "host2", "user2")

    database.remove_server("test1")

    servers = database.get_all_servers()
    assert len(servers) == 1
    assert servers[0]['alias'] == 'test2'


def test_update_server():
    """Updating specific fields should persist the changes."""
    database.add_server("test", "host", "user", password="pw")

    updated = database.update_server("test", hostname="new-host", password=None)
    assert updated is True

    server = database.get_server("test")
    assert server["hostname"] == "new-host"
    # Password explicitly set to None should persist as NULL.
    assert server["password"] is None


def test_update_server_no_fields():
    """Calling update without fields should be a no-op."""
    database.add_server("test", "host", "user")
    assert database.update_server("test") is False


def test_get_server_missing():
    """Fetching an unknown alias should return None."""
    assert database.get_server("unknown") is None


def test_seed_users_overwrites():
    """Seeding replaces existing rows and maintains sort order."""
    database.add_user(400)
    database.add_user(100)

    database.seed_users([3, 2, 5])
    assert database.get_whitelisted_users() == [2, 3, 5]


def test_seed_users_deduplicates_input():
    """Duplicate IDs should not trigger integrity errors or persist twice."""
    database.seed_users([5, 5, 3])
    assert database.get_whitelisted_users() == [3, 5]
