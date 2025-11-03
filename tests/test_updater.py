import pytest
from unittest.mock import patch, MagicMock
from src.updater import check_for_updates, apply_update

@patch('subprocess.run')
def test_check_for_updates_no_update(mock_run):
    """Test when no update is available."""
    # Mock git fetch, git rev-parse HEAD, git rev-parse @{u}
    mock_run.side_effect = [
        MagicMock(stdout=""),
        MagicMock(stdout="same_hash"),
        MagicMock(stdout="same_hash")
    ]
    result = check_for_updates()
    assert "You are already on the latest version." in result
    assert mock_run.call_count == 3

@patch('subprocess.run')
def test_check_for_updates_available(mock_run):
    """Test when an update is available."""
    mock_run.side_effect = [
        MagicMock(stdout=""),
        MagicMock(stdout="local_hash"),
        MagicMock(stdout="remote_hash")
    ]
    result = check_for_updates()
    assert "An update is available!" in result

@patch('subprocess.run')
def test_apply_update_success(mock_run):
    """Test successful application of an update."""
    mock_run.return_value = MagicMock(stdout="Success", stderr="")
    result = apply_update()
    assert "Update applied successfully!" in result
    # git pull, pip install, systemctl restart
    assert mock_run.call_count == 3
