import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from src.ssh_manager import SSHManager, SSHConnection

@pytest.fixture
def manager():
    """Fixture to create an SSHManager with a mock database."""
    servers = [
        {"alias": "test_server", "hostname": "localhost", "user": "testuser", "key_path": "/path/to/key"}
    ]
    with patch('src.ssh_manager.get_all_servers', MagicMock(return_value=servers)):
        manager = SSHManager()
    return manager

@pytest.mark.asyncio
async def test_get_connection(manager):
    """Test successfully getting a connection."""
    with patch("asyncssh.connect", new_callable=AsyncMock) as mock_connect:
        mock_connect.return_value = "mock_connection"
        conn = await manager.get_connection("test_server")
        assert isinstance(conn, SSHConnection)
        assert manager.connections["test_server"] == conn
        mock_connect.assert_called_once()

@pytest.mark.asyncio
async def test_get_connection_not_found(manager):
    """Test getting a connection to a non-existent server."""
    with pytest.raises(ValueError, match="Server with alias 'nonexistent' not found in the database."):
        await manager.get_connection("nonexistent")

@pytest.mark.asyncio
async def test_run_command(manager):
    """Test running a command with streaming output."""
    mock_process = AsyncMock()
    mock_process.stdout = AsyncMock()
    mock_process.stdout.__aiter__.return_value = ["line1\n", "line2\n"]
    mock_process.stderr = AsyncMock()
    mock_process.stderr.__aiter__.return_value = ["error1\n"]
    mock_process.__aenter__.return_value = mock_process

    mock_ssh_conn = AsyncMock()
    mock_ssh_conn.create_process = MagicMock(return_value=mock_process)

    # Patch asyncssh.connect to prevent real connection attempts
    with patch("asyncssh.connect", new_callable=AsyncMock) as mock_connect:
        mock_connect.return_value = mock_ssh_conn

        results = []
        async for line, stream in manager.run_command("test_server", "ls -l"):
            results.append((line.strip(), stream))

        assert ("line1", "stdout") in results
        assert ("line2", "stdout") in results
        assert ("error1", "stderr") in results
        mock_ssh_conn.create_process.assert_called_once_with("ls -l")
