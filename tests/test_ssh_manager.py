import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from src.ssh_manager import SSHManager

# Reusable server configurations for tests
TEST_SERVERS = [
    {"alias": "server1", "hostname": "host1", "user": "user1", "key_path": "/path/key1"},
    {"alias": "server2", "hostname": "host2", "user": "user2", "password": "password"},
]

@pytest.fixture
def manager():
    """Fixture to create an SSHManager with a mocked database call."""
    with patch('src.ssh_manager.get_all_servers', return_value=TEST_SERVERS):
        yield SSHManager()

@pytest.fixture
def mock_ssh_connection():
    """
    Fixture to create a mock asyncssh.SSHClientConnection.
    The close method is a regular MagicMock returning a completed future
    to avoid a "coroutine was never awaited" warning from the underlying
    asyncio event loop that pytest uses.
    """
    mock_conn = AsyncMock()
    mock_conn.is_closing = MagicMock(return_value=False)

    # This is the key to fixing the final warning.
    # We replace the AsyncMock's `close` with a regular MagicMock
    # that returns a future, which satisfies the `await` call.
    f = asyncio.Future()
    f.set_result(None)
    mock_conn.close = MagicMock(return_value=f)
    return mock_conn

@pytest.mark.asyncio
async def test_run_command_streams_output(manager, mock_ssh_connection):
    """
    Test that run_command connects, executes, streams output, and closes the connection.
    """
    # Setup mock process for streaming
    mock_process = AsyncMock()
    mock_process.__aenter__.return_value = mock_process
    mock_process.__aexit__.return_value = None
    mock_process.stdout.__aiter__.return_value = ["stdout line 1\n"]
    mock_process.stderr.__aiter__.return_value = ["stderr line 1\n"]
    mock_ssh_connection.create_process = AsyncMock(return_value=mock_process)

    with patch('src.ssh_manager.asyncssh.connect', new_callable=AsyncMock, return_value=mock_ssh_connection) as mock_connect:
        # Execute
        command_output = []
        async for line, stream in manager.run_command("server1", "ls"):
            command_output.append((line.strip(), stream))

        # Assertions
        mock_connect.assert_awaited_once_with(
            "host1",
            username="user1",
            client_keys=["/path/key1"],
            password=None,
            known_hosts=None
        )
        mock_ssh_connection.create_process.assert_awaited_once_with("ls")
        assert ("stdout line 1", "stdout") in command_output
        assert ("stderr line 1", "stderr") in command_output
        mock_ssh_connection.close.assert_called_once()

@pytest.mark.asyncio
async def test_run_command_server_not_found(manager):
    """Test that run_command raises ValueError for an unknown alias."""
    with pytest.raises(ValueError, match="Server alias 'unknown' not found."):
        # This part of the test ensures the async generator is consumed to trigger the error.
        async for _ in manager.run_command("unknown", "ls"):
            pass

@pytest.mark.asyncio
async def test_shell_session_lifecycle(manager, mock_ssh_connection):
    """
    Test the full lifecycle of a shell session: start, run command, and disconnect.
    """
    with patch('src.ssh_manager.asyncssh.connect', new_callable=AsyncMock, return_value=mock_ssh_connection) as mock_connect:
        # 1. Start shell session
        await manager.start_shell_session("server2")
        mock_connect.assert_awaited_once_with(
            "host2",
            username="user2",
            password="password",
            client_keys=None,
            known_hosts=None
        )
        assert "server2" in manager.active_shells
        assert manager.active_shells["server2"] == mock_ssh_connection

        # 2. Run command in shell
        mock_result = MagicMock()
        mock_result.stdout = "command output"
        mock_ssh_connection.run.return_value = mock_result

        output = await manager.run_command_in_shell("server2", "echo 'hello'")
        mock_ssh_connection.run.assert_awaited_once_with("echo 'hello'", check=True, timeout=60.0)
        assert output == "command output"

        # 3. Disconnect shell
        await manager.disconnect("server2")
        mock_ssh_connection.close.assert_called_once()
        assert "server2" not in manager.active_shells

@pytest.mark.asyncio
async def test_run_command_in_shell_no_session(manager):
    """
    Test that running a command in a non-existent shell raises a ConnectionError.
    """
    assert "server1" not in manager.active_shells
    with pytest.raises(ConnectionError, match="No active shell session for server1."):
        await manager.run_command_in_shell("server1", "ls")
