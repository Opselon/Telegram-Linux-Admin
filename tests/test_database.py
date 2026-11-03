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

    # Create tables expected by the module. This mirrors the production schema
    # in database.initialize_database() exactly — including the
    # FOREIGN KEY ... ON DELETE CASCADE on servers.owner_id, which the previous
    # fixture omitted. Without it, every cascade behaviour (seed_users, user
    # removal) is untestable here while silently diverging from production.
    conn.execute(
        """
        CREATE TABLE servers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            alias TEXT NOT NULL,
            hostname TEXT NOT NULL,
            user TEXT NOT NULL,
            password TEXT,
            key_path TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            host_key TEXT,
            FOREIGN KEY(owner_id) REFERENCES users(telegram_id) ON DELETE CASCADE,
            UNIQUE(owner_id, alias)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            telegram_id INTEGER NOT NULL UNIQUE,
            plan TEXT NOT NULL DEFAULT 'free'
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE user_preferences (
            telegram_id INTEGER PRIMARY KEY,
            language TEXT NOT NULL DEFAULT 'en'
        )
        """
    )

    # Patch the module-level connection utilities.
    monkeypatch.setattr(database, "get_db_connection", lambda: conn)
    monkeypatch.setattr(database, "_conn_lock", threading.RLock())
    monkeypatch.setenv(
        "TLA_ENCRYPTION_KEY",
        "VcrWrvOn83oXnwI75PwQBGzb62LF8A3BnQUwpsOSJyY=",
    )

    yield conn

    conn.close()


def test_add_and_get_servers():
    """Test adding and retrieving servers."""
    database.add_server(1, "test1", "host1", "user1", key_path="/path1")
    database.add_server(1, "test2", "host2", "user2", password="pw")

    servers = database.get_all_servers(1)
    assert len(servers) == 2
    assert servers[0]['key_path'] == '/path1'
    assert servers[1]['password'] == 'pw'


def test_add_duplicate_server():
    """Test that adding a duplicate server raises a ValueError."""
    database.add_server(1, "test1", "host1", "user1", key_path="/path1")
    with pytest.raises(ValueError, match="already exists for this user"):
        database.add_server(1, "test1", "host2", "user2", password="pw")


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
    database.add_server(1, "test1", "host1", "user1")
    database.add_server(1, "test2", "host2", "user2")

    assert database.remove_server(1, "test1") is True

    servers = database.get_all_servers(1)
    assert len(servers) == 1
    assert servers[0]['alias'] == 'test2'


def test_update_server():
    """Updating specific fields should persist the changes."""
    database.add_server(1, "test", "host", "user", password="pw")

    updated = database.update_server(1, "test", hostname="new-host", password=None)
    assert updated is True

    server = database.get_server(1, "test")
    assert server["hostname"] == "new-host"
    # Password explicitly set to None should persist as NULL.
    assert server["password"] is None


def test_update_server_no_fields():
    """Calling update without fields should be a no-op."""
    database.add_server(1, "test", "host", "user")
    assert database.update_server(1, "test") is False


def test_get_server_missing():
    """Fetching an unknown alias should return None."""
    assert database.get_server(1, "unknown") is None


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


def test_seed_users_preserves_servers_of_users_not_being_removed():
    """seed_users must never destroy another user's servers.

    The old implementation opened with ``DELETE FROM servers`` — an
    unconditional wipe of every owner's registrations — before rebuilding the
    whitelist. Seeding a whitelist that *dropped* one user silently deleted
    every other user's servers too. Only a removed user's own rows may
    cascade away.
    """
    for uid in (111, 222, 333):
        database.add_user(uid)
        for n in (1, 2):
            database.add_server(
                uid, f"srv{n}", f"10.0.0.{uid % 250}", "root", password=f"pw{uid}"
            )

    # Keep 111 and 222 in the whitelist, drop 333.
    database.seed_users([111, 222])

    # The retained users keep every server.
    assert database.get_whitelisted_users() == [111, 222]
    assert [s["alias"] for s in database.get_all_servers(111)] == ["srv1", "srv2"]
    assert [s["alias"] for s in database.get_all_servers(222)] == ["srv1", "srv2"]
    # Only the removed user's servers cascade away.
    assert database.get_all_servers(333) == []


def test_seed_users_is_idempotent():
    """Re-seeding the same whitelist must be a no-op for stored data."""
    database.add_user(111)
    database.add_server(111, "prod", "10.0.0.1", "root", password="pw")

    database.seed_users([111])
    database.seed_users([111])

    assert database.get_whitelisted_users() == [111]
    assert [s["alias"] for s in database.get_all_servers(111)] == ["prod"]


def test_user_language_preference_roundtrip():
    """Users can store and update a language preference."""
    assert database.get_user_language_preference(1001) is None

    database.set_user_language_preference(1001, 'de')
    assert database.get_user_language_preference(1001) == 'de'

    database.set_user_language_preference(1001, 'fr')
    assert database.get_user_language_preference(1001) == 'fr'


def test_user_language_preference_isolated_between_users():
    """Languages are stored independently for each Telegram user."""
    database.set_user_language_preference(1, 'ar')
    database.set_user_language_preference(2, 'fa')

    assert database.get_user_language_preference(1) == 'ar'
    assert database.get_user_language_preference(2) == 'fa'


def test_server_limit_enforced_for_free_plan():
    """Free plans cannot exceed three servers."""
    for idx in range(3):
        database.add_server(42, f"alias{idx}", f"host{idx}", f"user{idx}")

    with pytest.raises(ValueError, match="Server limit reached"):
        database.add_server(42, "extra", "host", "user")


def test_premium_plan_allows_ten_servers():
    """Premium users can store up to ten servers."""
    database.set_user_plan(99, 'premium')
    for idx in range(10):
        database.add_server(99, f"premium{idx}", f"host{idx}", f"user{idx}")

    assert database.get_user_server_count(99) == 10


def test_secrets_are_encrypted_at_rest(mock_db_connection):
    """Passwords and key paths are stored encrypted and decrypted on load."""
    database.add_server(7, "secure", "host", "user", password="secret", key_path="/tmp/key")
    row = mock_db_connection.execute(
        "SELECT password, key_path FROM servers WHERE owner_id = 7 AND alias = 'secure'"
    ).fetchone()
    assert row["password"] != "secret"
    assert row["key_path"] != "/tmp/key"

    server = database.get_server(7, "secure")
    assert server["password"] == "secret"
    assert server["key_path"] == "/tmp/key"
