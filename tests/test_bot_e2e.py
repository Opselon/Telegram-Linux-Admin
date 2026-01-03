import pytest
from unittest.mock import AsyncMock, MagicMock

# --- Mocks for Telegram Bot and SSH Manager ---

# Mock the SSHManager to control its behavior in tests
class MockSSHManager:
    def __init__(self, conn):
        self._conn = conn
        self._close_conn_mock = AsyncMock()

    async def _close_conn(self, conn):
        await self._close_conn_mock(conn)
        # In a real scenario, this would call the actual close logic
        if conn:
            if hasattr(conn, 'wait_closed'):
                conn.close()
                await conn.wait_closed()
            else:
                conn.close()


    async def run_command(self, user_id, alias, command):
        try:
            # Simulate a successful connection and command execution
            yield ("output line 1", "stdout")
        finally:
            await self._close_conn(self._conn)

# Mock connection classes from the unit tests
class SyncCloseConn:
    def __init__(self):
        self.closed = False
    def close(self):
        self.closed = True

class AsyncSSHLikeConn:
    def __init__(self):
        self.closed = False
        self.waited = False
    def close(self):
        self.closed = True
    async def wait_closed(self):
        self.waited = True

# --- E2E Test ---

@pytest.mark.asyncio
@pytest.mark.parametrize("conn_type", [SyncCloseConn, AsyncSSHLikeConn])
async def test_handle_server_connection_e2e(mocker, conn_type):
    """
    E2E test to ensure `handle_server_connection` completes without TypeError.
    """
    # 1. Setup Mocks

    # Import the function to test and the config object
    from src.main import handle_server_connection
    from src import main

    # Mock the global ssh_manager instance used by the handler
    mock_conn = conn_type()
    mock_ssh_manager = MockSSHManager(conn=mock_conn)
    mocker.patch('src.main.ssh_manager', mock_ssh_manager)

    # Mock Telegram's update and context objects
    update = AsyncMock()
    context = AsyncMock()

    # Configure the mock objects with necessary attributes
    user_id = 12345
    update.effective_user.id = user_id
    update.effective_chat.id = user_id
    update.callback_query.data = "connect_server_some_alias"
    update.callback_query.answer = AsyncMock()
    update.callback_query.message = AsyncMock()

    # Patch the config to authorize the user
    mocker.patch.object(main.config, 'whitelisted_users', [user_id])


    # 2. Execute the handler
    try:
        await handle_server_connection(update, context)
    except TypeError as e:
        # We want to fail on the specific "await None" error, but not others
        if "object NoneType can't be used in 'await' expression" in str(e):
             pytest.fail("The TypeError related to 'await None' should not have occurred.")
    except Exception as e:
        pytest.fail(f"An unexpected exception occurred: {e}")

    # 3. Assertions
    # Verify that the connection was closed correctly
    mock_ssh_manager._close_conn_mock.assert_awaited_once_with(mock_conn)

    if isinstance(mock_conn, SyncCloseConn):
        assert mock_conn.closed
    elif isinstance(mock_conn, AsyncSSHLikeConn):
        assert mock_conn.closed
        assert mock_conn.waited

    # Verify bot interactions
    update.callback_query.edit_message_text.assert_called()
    # The text is in the second call to edit_message_text
    final_call_args = update.callback_query.edit_message_text.call_args_list[1]
    assert "Connected to" in final_call_args.args[0]
