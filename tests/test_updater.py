import pytest
from unittest.mock import patch, MagicMock, call
from src.updater import apply_update, rollback, COMMAND_TIMEOUT
import sys

@patch('src.updater.shutil.rmtree')
@patch('src.updater.shutil.copy')
@patch('src.updater.shutil.copytree')
@patch('src.updater.download_and_extract_zip')
@patch('src.updater.subprocess.run')
@patch('src.updater.time.sleep', return_value=None) # Mock time.sleep to speed up tests
def test_apply_update_success(mock_sleep, mock_run, mock_download, mock_copytree, mock_copy, mock_rmtree):
    """Test the successful application of an update."""
    with patch('src.updater.Path.exists', return_value=True):
        result = apply_update()

    assert "Update process completed successfully!" in result
    mock_download.assert_called_once()
    mock_copytree.assert_called_once()
    # Assuming at least 2 data files are backed up and restored
    assert mock_copy.call_count >= 2

    expected_calls = [
        call(["systemctl", "stop", "telegram_bot.service"], check=True, timeout=COMMAND_TIMEOUT),
        call([sys.executable, "-m", "pip", "install", "-e", "."], check=True, timeout=COMMAND_TIMEOUT),
        call(["systemctl", "start", "telegram_bot.service"], check=True, timeout=COMMAND_TIMEOUT)
    ]
    mock_run.assert_has_calls(expected_calls)
    assert mock_run.call_count == 3

@patch('src.updater.shutil.rmtree')
@patch('src.updater.shutil.copy')
@patch('src.updater.shutil.copytree')
@patch('src.updater.download_and_extract_zip', side_effect=Exception("Download failed"))
@patch('src.updater.rollback')
@patch('src.updater.time.sleep', return_value=None)
def test_apply_update_failure_and_rollback(mock_sleep, mock_rollback, mock_download, mock_copytree, mock_copy, mock_rmtree):
    """Test a failed update and the subsequent rollback."""
    # Create dummy data files for the backup step to succeed before the download fails
    from pathlib import Path
    config_file = Path("config.json")
    db_file = Path("database.db")
    config_file.touch()
    db_file.touch()

    try:
        with patch('src.updater.subprocess.run') as mock_run:
            result = apply_update()

        mock_run.assert_called_once_with(["systemctl", "stop", "telegram_bot.service"], check=True, timeout=COMMAND_TIMEOUT)
        assert "Update Failed: Download failed" in result
        assert "Attempting to roll back..." in result
        mock_rollback.assert_called_once()
    finally:
        # Clean up the dummy files to not affect other tests
        if config_file.exists():
            config_file.unlink()
        if db_file.exists():
            db_file.unlink()

@patch('src.updater.shutil.copytree')
@patch('src.updater.subprocess.run')
def test_rollback(mock_run, mock_copytree):
    """Test the rollback function."""
    backup_dir = MagicMock()

    rollback(backup_dir)

    expected_calls = [
        call(["systemctl", "stop", "telegram_bot.service"], check=True, timeout=COMMAND_TIMEOUT),
        call([sys.executable, '-m', 'pip', 'install', '-e', '.'], check=True, timeout=COMMAND_TIMEOUT),
        call(["systemctl", "start", "telegram_bot.service"], check=True, timeout=COMMAND_TIMEOUT)
    ]
    mock_run.assert_has_calls(expected_calls)
    mock_copytree.assert_called_once()
