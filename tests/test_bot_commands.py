import os
import zipfile
from unittest.mock import AsyncMock, patch
import pytest
from src.main import backup

@pytest.mark.asyncio
@patch('src.main.config')
async def test_backup_command(mock_config):
    """Test the /backup command."""
    # Setup mock config for authorization
    mock_config.whitelisted_users = [12345]

    update = AsyncMock()
    update.effective_user.id = 12345
    context = AsyncMock()

    # Create dummy files to be backed up
    with open("config.json", "w") as f:
        f.write("{}")
    with open("database.db", "w") as f:
        f.write("dummy data")

    await backup(update, context)

    # Verify that the bot tried to send a document
    update.effective_message.reply_document.assert_called_once()

    # Clean up dummy files
    os.remove("config.json")
    os.remove("database.db")

@pytest.mark.asyncio
@patch('src.main.config')
@patch('src.main.os.execv')
@patch('src.main.sys')
@patch('src.main.zipfile.ZipFile')
@patch('src.main.os.path.exists', return_value=True)
@patch('src.main.os.remove')
async def test_restore_command(mock_remove, mock_exists, mock_zipfile, mock_sys, mock_execv, mock_config):
    """Test the /restore command."""
    mock_config.whitelisted_users = [12345]
    update = AsyncMock()
    update.effective_user.id = 12345
    context = AsyncMock()

    # Mock the document object
    update.message.document.file_name = "test_backup.zip"
    update.message.document.get_file.return_value.download_to_drive = AsyncMock()

    # Configure the mock ZipFile
    mock_zipfile.return_value.__enter__.return_value.namelist.return_value = ["config.json", "database.db"]

    from src.main import restore_file
    await restore_file(update, context)

    # Verify that the restart was called
    mock_execv.assert_called_once()

    # Verify the backup file was removed
    mock_remove.assert_called_with("test_backup.zip")

    # Clean up dummy files
    if os.path.exists("config.json"):
        os.remove("config.json")
    if os.path.exists("database.db"):
        os.remove("database.db")
