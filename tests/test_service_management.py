import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from src.main import service_action_start, execute_service_action, AWAIT_SERVICE_NAME

@pytest.mark.asyncio
@patch('src.main.config')
async def test_service_action_start(mock_config):
    """Test the start of the service action conversation."""
    mock_config.whitelisted_users = [12345]
    update = AsyncMock()
    update.effective_user.id = 12345
    update.effective_chat = MagicMock(id=12345)
    update.callback_query.data = "check_service_server1"
    context = AsyncMock()

    result = await service_action_start(update, context)

    assert result == AWAIT_SERVICE_NAME
    update.callback_query.message.edit_message_text.assert_called_once()
    args, kwargs = update.callback_query.message.edit_message_text.call_args
    assert "Please enter the name of the service to `check`." in args[0]

@pytest.mark.asyncio
@patch('src.main.config')
@patch('src.main.ssh_manager', new_callable=AsyncMock)
async def test_execute_service_action(mock_ssh_manager, mock_config):
    """Test the execution of a service action."""
    mock_config.whitelisted_users = [12345]
    update = AsyncMock()
    update.effective_user.id = 12345
    update.effective_chat = MagicMock(id=12345)
    update.message.text = "nginx"
    context = AsyncMock()
    context.user_data = {'service_action': 'status', 'alias': 'server1'}

    # This is the correct way to mock an async generator
    async def mock_run_command(alias, command):
        assert alias == 'server1'
        assert command == 'systemctl status nginx'
        yield "nginx is running", "stdout"

    mock_ssh_manager.run_command = mock_run_command

    from src.main import ConversationHandler
    result = await execute_service_action(update, context)

    assert result == ConversationHandler.END
    # The result message is sent via chat.send_message and then edited with the output
    update.message.chat.send_message.assert_called_once()
    # The assertions about the command/alias are inside mock_run_command above
