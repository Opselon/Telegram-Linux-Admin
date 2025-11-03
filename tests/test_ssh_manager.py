import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from src.ssh_manager import SSHManager

@pytest.fixture
def manager():
    # Create a mock config file
    config_data = {
        "servers": [
            {
                "alias": "test_server",
                "hostname": "localhost",
                "user": "testuser",
                "key_path": "/path/to/key"
            }
        ]
    }
    with patch("builtins.open", MagicMock()) as mock_open:
        with patch("json.load", MagicMock(return_value=config_data)) as mock_json:
            manager = SSHManager('dummy_config.json')
    return manager

@pytest.mark.asyncio
async def test_connect(manager):
    """Test successful connection."""
    with patch("asyncssh.connect", new_callable=AsyncMock) as mock_connect:
        mock_connect.return_value = "mock_connection"
        conn = await manager.connect("test_server")
        assert conn == "mock_connection"
        assert manager.connections["test_server"] == "mock_connection"
        mock_connect.assert_called_once_with("localhost", username="testuser", client_keys=["/path/to/key"])

@pytest.mark.asyncio
async def test_connect_not_found(manager):
    """Test connection to a non-existent server."""
    with pytest.raises(ValueError, match="Server with alias 'nonexistent' not found in config."):
        await manager.connect("nonexistent")

@pytest.mark.asyncio
async def test_run_command(manager):
    """Test running a command with streaming output."""
    # The process object must be an AsyncMock to support async iteration and context management
    mock_process = AsyncMock()

    # Mock the stdout and stderr streams to be async iterators
    mock_process.stdout = AsyncMock()
    mock_process.stdout.__aiter__.return_value = ["line1\n", "line2\n"]

    mock_process.stderr = AsyncMock()
    mock_process.stderr.__aiter__.return_value = ["error1\n"]

    # When used as a context manager, __aenter__ should return the mock itself
    mock_process.__aenter__.return_value = mock_process

    # The connection mock
    mock_conn = MagicMock()
    # The create_process method is a regular method that returns an async context manager
    mock_conn.create_process = MagicMock(return_value=mock_process)

    manager.connections["test_server"] = mock_conn

    results = []
    async for line, stream in manager.run_command("test_server", "ls -l"):
        results.append((line.strip(), stream))

    assert ("line1", "stdout") in results
    assert ("line2", "stdout") in results
    assert ("error1", "stderr") in results
    mock_conn.create_process.assert_called_once_with("ls -l")
