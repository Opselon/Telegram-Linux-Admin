import pytest
from unittest.mock import patch
from src.updater import check_for_updates, apply_update

# Since run_command is now in the same module, we patch it directly
@patch('src.updater.run_command')
def test_check_for_updates_no_update(mock_run_command):
    """Test when no update is available."""
    mock_run_command.side_effect = [
        {"stdout": "", "stderr": "", "returncode": 0},  # git fetch
        {"stdout": "same_hash", "stderr": "", "returncode": 0},  # git rev-parse HEAD
        {"stdout": "same_hash", "stderr": "", "returncode": 0},  # git rev-parse @{u}
    ]

    result = check_for_updates()

    assert result['status'] == 'no_update'
    assert "You are already on the latest version." in result['message']
    assert mock_run_command.call_count == 3

@patch('src.updater.run_command')
def test_check_for_updates_available(mock_run_command):
    """Test when an update is available."""
    mock_run_command.side_effect = [
        {"stdout": "", "stderr": "", "returncode": 0},
        {"stdout": "local_hash", "stderr": "", "returncode": 0},
        {"stdout": "remote_hash", "stderr": "", "returncode": 0},
    ]

    result = check_for_updates()

    assert result['status'] == 'update_available'
    assert "An update is available!" in result['message']

@patch('src.updater.run_command')
def test_apply_update_success(mock_run_command):
    """Test successful application of an update."""
    mock_run_command.return_value = {"stdout": "Success", "stderr": "", "returncode": 0}

    result = apply_update()

    assert "Update process completed!" in result
    # git pull, pip install, systemctl restart
    assert mock_run_command.call_count == 3
