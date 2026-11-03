import os
from pathlib import Path
import shutil
import sqlite3
import zipfile
from unittest.mock import AsyncMock, patch
import pytest
from src import main
from src.main import backup
from src.database import initialize_database, close_db_connection
from src.access_control import permissions

@pytest.fixture(autouse=True)
def setup_teardown():
    """Fixture to set up and tear down the test environment."""
    # Ensure no previous database file exists
    if os.path.exists("database.db"):
        os.remove("database.db")
    # Setup: Create a valid database
    initialize_database()
    yield
    # Teardown: Clean up database and config files
    close_db_connection()
    if os.path.exists("database.db"):
        os.remove("database.db")
    if os.path.exists("config.json"):
        os.remove("config.json")

@pytest.mark.asyncio
@patch('src.main.config')
async def test_backup_command(mock_config):
    """Test the backup command."""
    # Setup mock config for authorization (admin identity is mirrored by the
    # permission manager, which authorizes admin-only handlers).
    mock_config.whitelisted_users = [12345]
    permissions.update(whitelisted=[12345], admins=[12345])

    update = AsyncMock()
    update.effective_user.id = 12345
    update.effective_chat.id = 12345
    context = AsyncMock()

    # Create dummy config file
    with open("config.json", "w") as f:
        f.write("{}")

    await backup(update, context)

    # Verify that the bot tried to send a document
    context.bot.send_document.assert_called_once()

@pytest.mark.asyncio
@patch('src.main.config')
@patch('src.main.os.execv')
@patch('src.main.sys')
@patch('src.main.zipfile.ZipFile')
async def test_restore_command(
    mock_zipfile, mock_sys, mock_execv, mock_config, tmp_path, monkeypatch
):
    """Test the restore command."""
    mock_config.whitelisted_users = [12345]
    update = AsyncMock()
    update.effective_user.id = 12345
    context = AsyncMock()

    # Run in a throwaway working directory so the restore never touches the
    # developer's real config/database/key files. Both the database and the
    # config path are resolved from environment variables that point here.
    workdir = tmp_path / "restore-workdir"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    monkeypatch.setenv("TLA_CONFIG_FILE", str(workdir / "config.json"))
    monkeypatch.setenv("TLA_DB_FILE", str(workdir / "database.db"))
    monkeypatch.setenv("TLA_ENCRYPTION_KEY_FILE", str(workdir / "var" / "encryption.key"))
    from src import database as database_module
    database_module.close_db_connection()

    # Create the live config that the safety backup will snapshot
    with open(workdir / "config.json", "w") as f:
        f.write("{}")

    # Mock the document object
    update.message.document.file_name = "test_backup.zip"
    update.message.document.file_size = 1024  # Set an integer value for the size

    # Create a dummy file for the download mock
    dummy_zip_path = str(Path(tmp_path) / "test_backup.zip")
    with open(dummy_zip_path, "w") as f:
        f.write("dummy zip content")

    async def mock_download(path):
        os.rename(dummy_zip_path, path)

    update.message.document.get_file.return_value.download_to_drive = AsyncMock(side_effect=mock_download)

    # Configure the mock ZipFile. The database written here must be a *real*
    # SQLite database: restore now verifies the candidate with
    # PRAGMA integrity_check before it is allowed to replace the live
    # database. The previous version of this test wrote the literal string
    # "dummy data" as the database and asserted the bot restarted onto it —
    # which is exactly how a junk file could pass every check and replace
    # the live credential store.
    real_db_path = tmp_path / "source.db"
    real_conn = sqlite3.connect(real_db_path)
    real_conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            telegram_id INTEGER NOT NULL UNIQUE,
            plan TEXT NOT NULL DEFAULT 'free'
        );
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
        );
        CREATE TABLE user_preferences (
            telegram_id INTEGER PRIMARY KEY,
            language TEXT NOT NULL DEFAULT 'en'
        );
        INSERT INTO users (telegram_id) VALUES (12345);
        INSERT INTO servers (owner_id, alias, hostname, user)
            VALUES (12345, 'restored-server', '10.9.9.9', 'root');
        """
    )
    real_conn.commit()
    real_conn.close()

    mock_zipfile.return_value.__enter__.return_value.namelist.return_value = [
        "config.json", "database.db", "backup_metadata.json"
    ]
    mock_zipfile.return_value.__enter__.return_value.testzip.return_value = None

    def create_dummy_files(extract_dir):
        os.makedirs(extract_dir, exist_ok=True)
        with open(os.path.join(extract_dir, "config.json"), "w") as f:
            f.write("{}")
        shutil.copy2(real_db_path, os.path.join(extract_dir, "database.db"))

    mock_zipfile.return_value.__enter__.return_value.extractall.side_effect = create_dummy_files

    from src.main import restore_file
    await restore_file(update, context)

    # Verify that the restart was called
    mock_execv.assert_called_once()
