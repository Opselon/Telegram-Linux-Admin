import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from src.main import execute_shell_command, connect_menu, connect, backup

@pytest.fixture
def authorized_update():
    """Fixture to create a mock update object with an authorized user."""
    update = MagicMock()
    update.effective_user.id = 123
    update.message.from_user.id = 123
    update.message.reply_text = AsyncMock()

    # For callback queries
    update.callback_query = MagicMock()
    update.callback_query.from_user.id = 123
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.callback_query.message.reply_text = AsyncMock()

    return update

@pytest.mark.asyncio
async def test_execute_shell_command_real_time_output(authorized_update):
    """Test the real-time output and message editing logic."""
    context = MagicMock()
    mock_message = MagicMock()
    mock_message.edit_text = AsyncMock()
    authorized_update.message.reply_text.return_value = mock_message

    with patch('src.main.ssh_manager', new_callable=AsyncMock) as mock_ssh_manager:
        async def mock_run_command(alias, command):
            yield "line1\n", "stdout"
        mock_ssh_manager.run_command = mock_run_command

        with patch('src.main.user_connections', {123: 'test_server'}):
            with patch('src.main.whitelisted_users', [123]):
                await execute_shell_command(authorized_update, context, "ls -l")

                authorized_update.message.reply_text.assert_called_once()
                assert mock_message.edit_text.call_count > 0

@pytest.mark.asyncio
@patch('src.main.get_all_servers', MagicMock(return_value=[{"alias": "test1"}, {"alias": "test2"}]))
async def test_connect_menu_button(authorized_update):
    """Test the connect menu button."""
    context = MagicMock()
    with patch('src.main.whitelisted_users', [123]):
        await connect_menu(authorized_update, context)
        authorized_update.callback_query.message.reply_text.assert_called_once()
        reply_markup = authorized_update.callback_query.message.reply_text.call_args[1]['reply_markup']
        assert len(reply_markup.inline_keyboard) == 2

@pytest.mark.asyncio
async def test_connect_button_press(authorized_update):
    """Test pressing a connect button."""
    authorized_update.callback_query.data = "connect_test_server"
    context = MagicMock()

    with patch('src.main.ssh_manager', new_callable=AsyncMock) as mock_ssh_manager:
        mock_ssh_manager.get_connection = AsyncMock()
        with patch('src.main.user_connections', {}):
            with patch('src.main.whitelisted_users', [123]):
                await connect(authorized_update, context)
                alias = authorized_update.callback_query.data.split('_', 1)[1]
                mock_ssh_manager.get_connection.assert_called_once_with(alias)
                authorized_update.callback_query.edit_message_text.assert_called_once_with(text=f"Successfully connected to {alias}.")

@pytest.mark.asyncio
async def test_backup_command(authorized_update):
    """Test the backup command."""
    context = MagicMock()
    context.bot.send_document = AsyncMock()

    with patch('builtins.open', MagicMock()):
        with patch('src.main.whitelisted_users', [123]):
            await backup(authorized_update, context)
            authorized_update.callback_query.answer.assert_called_once()
            context.bot.send_document.assert_called_once()
