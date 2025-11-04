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

@patch('os.remove')
@patch('shutil.copy2')
@patch('os.path.exists')
@patch('src.updater.run_command')
def test_apply_update_success(mock_run_command, mock_exists, mock_copy, mock_remove):
    """Test successful application of an update."""
    mock_exists.return_value = True
    mock_run_command.side_effect = [
        # Pre-update checks
        {"stdout": "true", "stderr": "", "returncode": 0},  # git rev-parse --is-inside-work-tree
        {"stdout": "", "stderr": "", "returncode": 0},      # git status --porcelain
        # Backup
        {"stdout": "old_hash", "stderr": "", "returncode": 0}, # git rev-parse HEAD
        # Update process
        {"stdout": "Success", "stderr": "", "returncode": 0}, # git pull
        {"stdout": "Success", "stderr": "", "returncode": 0}, # pip install
        {"stdout": "Success", "stderr": "", "returncode": 0}, # systemctl restart
    ]

    result = apply_update()

    assert "Update process completed!" in result
    assert mock_run_command.call_count == 6
    assert mock_copy.called

@patch('os.remove')
@patch('shutil.move')
@patch('shutil.copy2')
@patch('os.path.exists')
@patch('src.updater.run_command')
def test_apply_update_failure_and_rollback(mock_run_command, mock_exists, mock_copy, mock_move, mock_remove):
    """Test a failed update and the subsequent rollback."""
    # Simulate the existence of the db for backup, and the backup for restore
    mock_exists.side_effect = lambda path: path in ['database.db', 'database.db.backup']

    mock_run_command.side_effect = [
        # Pre-update checks
        {"stdout": "true", "stderr": "", "returncode": 0},
        {"stdout": "", "stderr": "", "returncode": 0},
        # Backup
        {"stdout": "old_hash", "stderr": "", "returncode": 0},
        # Update process fails
        {"stdout": "", "stderr": "Error pulling", "returncode": 1}, # git pull fails
        # Rollback process
        {"stdout": "Success", "stderr": "", "returncode": 0}, # git reset --hard
        {"stdout": "Success", "stderr": "", "returncode": 0}, # pip install
        {"stdout": "Success", "stderr": "", "returncode": 0}, # systemctl restart
    ]

    result = apply_update()

    assert "Update Failed:" in result
    assert "Attempting to roll back" in result
    assert "Bot service restarted. The system should be back to its pre-update state." in result
    # Pre-checks (2) + Backup (1) + Failed pull (1) + Rollback (3)
    assert mock_run_command.call_count == 7
    assert mock_copy.call_count == 1 # Only for backup
    assert mock_move.call_count == 1 # For restore
