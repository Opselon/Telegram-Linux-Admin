import pytest
import sqlite3
from src.database import add_server, get_all_servers, add_user, get_whitelisted_users

@pytest.fixture
def mock_db_connection():
    """Fixture to create and manage an in-memory SQLite database for testing."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    # Create tables
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE servers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        alias TEXT NOT NULL UNIQUE,
        hostname TEXT NOT NULL,
        user TEXT NOT NULL,
        key_path TEXT NOT NULL
    )
    """)
    cursor.execute("""
    CREATE TABLE users (
        id INTEGER PRIMARY KEY,
        telegram_id INTEGER NOT NULL UNIQUE
    )
    """)
    conn.commit()

    # Patch get_db_connection to return this in-memory connection
    with pytest.MonkeyPatch.context() as m:
        m.setattr('src.database.get_db_connection', lambda: conn)
        yield conn

    conn.close()


def test_add_and_get_servers(mock_db_connection):
    """Test adding and retrieving servers."""
    add_server("test1", "host1", "user1", "/path1")
    add_server("test2", "host2", "user2", "/path2")

    servers = get_all_servers()
    assert len(servers) == 2
    assert servers[0]['alias'] == 'test1'
    assert servers[1]['hostname'] == 'host2'

def test_add_duplicate_server(mock_db_connection):
    """Test that adding a duplicate server raises a ValueError."""
    add_server("test1", "host1", "user1", "/path1")
    with pytest.raises(ValueError, match="Server with alias 'test1' already exists."):
        add_server("test1", "host2", "user2", "/path2")

def test_add_and_get_users(mock_db_connection):
    """Test adding and retrieving whitelisted users."""
    add_user(123)
    add_user(456)

    users = get_whitelisted_users()
    assert len(users) == 2
    assert 123 in users
    assert 456 in users

def test_add_duplicate_user(mock_db_connection):
    """Test that adding a duplicate user does not raise an error and does not create a duplicate."""
    add_user(123)
    add_user(123)

    users = get_whitelisted_users()
    assert len(users) == 1
