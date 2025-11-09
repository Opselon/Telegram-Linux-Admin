import pytest
from unittest.mock import patch, MagicMock
from src.updater import apply_update

@patch('src.updater.shutil.rmtree')
@patch('src.updater.shutil.copy')
@patch('src.updater.shutil.copytree')
@patch('src.updater.download_and_extract_zip')
@patch('src.updater.subprocess.run')
def test_apply_update_success(mock_run, mock_download, mock_copytree, mock_copy, mock_rmtree):
    """Test the successful application of an update."""
    with patch('src.updater.Path.exists', return_value=True):
        result = apply_update()

    assert "Update process completed!" in result
    mock_download.assert_called_once()
    mock_copytree.assert_called_once()
    assert mock_copy.call_count == 4 # 2 for backup, 2 for restore
    assert mock_run.call_count == 2 # pip install and systemctl restart

@patch('src.updater.shutil.rmtree')
@patch('src.updater.shutil.copytree')
@patch('src.updater.download_and_extract_zip', side_effect=Exception("Download failed"))
@patch('src.updater.rollback')
def test_apply_update_failure_and_rollback(mock_rollback, mock_download, mock_copytree, mock_rmtree):
    """Test a failed update and the subsequent rollback."""
    result = apply_update()

    assert "Update Failed: Download failed" in result
    assert "Attempting to roll back..." in result
    mock_rollback.assert_called_once()
