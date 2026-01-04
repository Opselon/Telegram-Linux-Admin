import os
import zipfile
from unittest.mock import AsyncMock, patch
import pytest
from src.main import backup
from src.database import initialize_database, close_db_connection

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
    # Setup mock config for authorization
    mock_config.whitelisted_users = [12345]

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
@patch('src.main.os.path.exists', return_value=True)
@patch('src.main.os.remove')
async def test_restore_command(mock_remove, mock_exists, mock_zipfile, mock_sys, mock_execv, mock_config):
    """Test the restore command."""
    mock_config.whitelisted_users = [12345]
    update = AsyncMock()
    update.effective_user.id = 12345
    context = AsyncMock()

    # Create a dummy config file to be backed up during restore
    with open("config.json", "w") as f:
        f.write("{}")

    # Mock the document object
    update.message.document.file_name = "test_backup.zip"
    update.message.document.file_size = 1024  # Set an integer value for the size

    # Create a dummy file for the download mock
    dummy_zip_path = "/tmp/test_backup.zip"
    with open(dummy_zip_path, "w") as f:
        f.write("dummy zip content")

    async def mock_download(path):
        os.rename(dummy_zip_path, path)

    update.message.document.get_file.return_value.download_to_drive = AsyncMock(side_effect=mock_download)

    # Configure the mock ZipFile
    mock_zipfile.return_value.__enter__.return_value.namelist.return_value = ["config.json", "database.db"]
    mock_zipfile.return_value.__enter__.return_value.testzip.return_value = None

    def create_dummy_files(extract_dir):
        os.makedirs(extract_dir, exist_ok=True)
        with open(os.path.join(extract_dir, "config.json"), "w") as f:
            f.write("{}")
        with open(os.path.join(extract_dir, "database.db"), "w") as f:
            f.write("dummy data")

    mock_zipfile.return_value.__enter__.return_value.extractall.side_effect = create_dummy_files

    from src.main import restore_file
    await restore_file(update, context)

    # Verify that the restart was called
    mock_execv.assert_called_once()
