import sys

import pytest
from unittest.mock import patch, MagicMock, call

from src.updater import COMMAND_TIMEOUT, apply_update, rollback

# apply_update() drives systemd through the _is_systemd_available /
# _stop_bot_gracefully / _start_bot_gracefully helpers, so the tests patch those
# (not subprocess.run) to keep assertions aligned with the real call flow.


@pytest.fixture
def fake_data_files(tmp_path, monkeypatch):
    """Point REPO_ROOT at a temp dir holding real config.json/database.db and a src/ dir."""
    for name in ("config.json", "database.db"):
        (tmp_path / name).write_text("dummy-content")
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "dummy.py").write_text("# dummy")
    monkeypatch.setattr("src.updater.REPO_ROOT", tmp_path)
    return tmp_path


@patch("src.updater._is_systemd_available", return_value=True)
@patch("src.updater._stop_bot_gracefully")
@patch("src.updater._start_bot_gracefully")
@patch("src.updater.shutil.rmtree")
@patch("src.updater.download_and_extract_zip")
@patch("src.updater.subprocess.run")
@patch("src.updater.time.sleep", return_value=None)
def test_apply_update_success(
    mock_sleep,
    mock_run,
    mock_download,
    mock_rmtree,
    mock_start,
    mock_stop,
    mock_systemd,
    fake_data_files,
):
    """Test the successful application of an update."""
    result = apply_update()

    assert "Update process completed successfully!" in result
    mock_download.assert_called_once()

    # The only direct subprocess.run call is the dependency install; systemd
    # start/stop go through the patched helpers.
    expected_calls = [
        call([sys.executable, "-m", "pip", "install", "-e", ".", "--quiet"],
             check=True, timeout=COMMAND_TIMEOUT, capture_output=True, text=True),
    ]
    mock_run.assert_has_calls(expected_calls)
    mock_start.assert_called_once()
    assert mock_stop.call_count >= 1


@patch("src.updater._is_systemd_available", return_value=True)
@patch("src.updater._stop_bot_gracefully")
@patch("src.updater._start_bot_gracefully")
@patch("src.updater.download_and_extract_zip", side_effect=Exception("Download failed"))
@patch("src.updater.rollback")
@patch("src.updater.time.sleep", return_value=None)
def test_apply_update_failure_and_rollback(
    mock_sleep,
    mock_rollback,
    mock_download,
    mock_start,
    mock_stop,
    mock_systemd,
    fake_data_files,
):
    """Test a failed update and the subsequent rollback."""
    with patch("src.updater.subprocess.run"):
        result = apply_update()

    assert "Update Failed: Download failed" in result
    assert "Attempting automatic rollback..." in result
    mock_rollback.assert_called_once()
    mock_stop.assert_called()


@patch("src.updater._is_systemd_available", return_value=True)
@patch("src.updater._stop_bot_gracefully")
@patch("src.updater._start_bot_gracefully")
@patch("src.updater.shutil.copy2")
@patch("src.updater.shutil.copytree")
@patch("src.updater.subprocess.run")
def test_rollback(mock_run, mock_copytree, mock_copy2, mock_start, mock_stop, mock_systemd):
    """Test the rollback function."""
    backup_dir = MagicMock()

    rollback(backup_dir)

    expected_calls = [
        call([sys.executable, '-m', 'pip', 'install', '-e', '.', '--quiet'],
             check=True, timeout=COMMAND_TIMEOUT, capture_output=True),
    ]
    mock_run.assert_has_calls(expected_calls)
    mock_copytree.assert_called_once()
