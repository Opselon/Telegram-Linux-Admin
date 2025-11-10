import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock

from src.ssh_manager import SSHManager

# --- Mock Connection Classes ---

class SyncCloseConn:
    """A mock connection with a synchronous `close` method."""
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True
        return None

class AwaitableCloseConn:
    """A mock connection where `close` returns an awaitable."""
    def __init__(self):
        self.closed = False

    async def _do_close(self):
        await asyncio.sleep(0)  # Simulate async operation
        self.closed = True

    def close(self):
        return self._do_close()

class AsyncSSHLikeConn:
    """A mock connection that mimics asyncssh's close/wait_closed pattern."""
    def __init__(self):
        self.closed = False
        self.waited = False

    def close(self):
        self.closed = True

    async def wait_closed(self):
        await asyncio.sleep(0) # Simulate async operation
        self.waited = True

# --- Tests ---

@pytest.mark.asyncio
async def test_close_conn_handles_sync_close():
    """Verify `_close_conn` handles connections with a simple sync `close`."""
    manager = SSHManager()
    conn = SyncCloseConn()

    await manager._close_conn(conn)

    assert conn.closed, "The connection's close() method should have been called."

@pytest.mark.asyncio
async def test_close_conn_handles_awaitable_close():
    """Verify `_close_conn` awaits a coroutine returned by `close`."""
    manager = SSHManager()
    conn = AwaitableCloseConn()

    await manager._close_conn(conn)

    assert conn.closed, "The connection's close() coroutine should have been awaited."

@pytest.mark.asyncio
async def test_close_conn_handles_asyncssh_pattern():
    """Verify `_close_conn` handles the close() + wait_closed() pattern."""
    manager = SSHManager()
    conn = AsyncSSHLikeConn()

    await manager._close_conn(conn)

    assert conn.closed, "The connection's close() method should have been called."
    assert conn.waited, "The connection's wait_closed() coroutine should have been awaited."

@pytest.mark.asyncio
async def test_close_conn_with_none():
    """Verify `_close_conn` does not raise when the connection is None."""
    manager = SSHManager()
    try:
        await manager._close_conn(None)
    except Exception as e:
        pytest.fail(f"_close_conn(None) raised an unexpected exception: {e}")

@pytest.mark.asyncio
async def test_run_command_always_closes_connection(mocker):
    """
    Verify `run_command` calls `_close_conn` on success and exception.
    """
    # Prevent database access during initialization
    mocker.patch('src.ssh_manager.get_all_servers', return_value=[])

    # 1. Test success case
    manager = SSHManager()
    mock_conn = AsyncSSHLikeConn()

    # Mock internal methods to isolate run_command's logic
    mocker.patch.object(manager, '_create_connection', return_value=mock_conn)
    mocker.patch.object(manager, '_close_conn', new_callable=AsyncMock)

    # Mock the command streaming part
    async def fake_streamer(*args, **kwargs):
        yield ("output", "stdout")

    # Mock the asyncssh process and its streams
    process_mock = AsyncMock()
    process_mock.stdout = fake_streamer()
    process_mock.stderr = fake_streamer()

    conn_mock = AsyncMock()
    conn_mock.create_process.return_value = process_mock

    # Replace the _create_connection to return our fully mocked connection
    mocker.patch.object(manager, '_create_connection', return_value=conn_mock)


    # Consume the generator
    async for _, __ in manager.run_command("alias", "cmd"):
        pass

    manager._close_conn.assert_awaited_once_with(conn_mock)

    # 2. Test exception case
    manager._close_conn.reset_mock()

    # Configure streamer to raise an error
    async def error_streamer(*args, **kwargs):
        yield ("output", "stdout")
        raise ValueError("Command failed")

    process_mock.stdout = error_streamer()

    with pytest.raises(ValueError):
        async for _, __ in manager.run_command("alias", "cmd"):
            pass

    manager._close_conn.assert_awaited_once_with(conn_mock)
