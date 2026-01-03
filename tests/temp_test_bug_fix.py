import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from src.main import execute_command
from telegram import Update, User
from telegram.ext import ContextTypes, ConversationHandler

@pytest.fixture
def mock_update():
    """Fixture for a mock Update object."""
    update = AsyncMock(spec=Update)
    update.effective_user = AsyncMock(spec=User)
    update.effective_user.id = 12345
    update.message = AsyncMock()
    update.message.text = "ls -la"
    return update

@pytest.fixture
def mock_context():
    """Fixture for a mock context object."""
    context = MagicMock(spec=ContextTypes.DEFAULT_type)
    context.user_data = {}
    return context

@pytest.mark.asyncio
@patch('src.main.ssh_manager')
@patch('src.main.user_connections', {12345: 'test-server'})
@patch('src.main.config')
async def test_execute_command_bug_fix(mock_config, mock_ssh_manager, mock_update, mock_context):
    """Test that the execute_command function correctly handles the ssh_manager.run_command output."""
    mock_config.whitelisted_users = [12345]

    async def mock_run_command_gen():
        yield ("-rw-r--r--", 'stdout')
        yield (" ", 'stdout')
        yield ("1", 'stdout')
        yield (" ", 'stdout')
        yield ("root", 'stdout')
        yield (" ", 'stdout')
        yield ("root", 'stdout')
        yield (" ", 'stdout')
        yield ("0", 'stdout')
        yield (" ", 'stdout')
        yield ("Jan", 'stdout')
        yield (" ", 'stdout')
        yield ("1", 'stdout')
        yield (" ", 'stdout')
        yield ("2025", 'stdout')
        yield (" ", 'stdout')
        yield (".", 'stdout')

    mock_ssh_manager.run_command.return_value = mock_run_command_gen()

    mock_result_message = AsyncMock()
    mock_update.message.reply_text.return_value = mock_result_message

    result = await execute_command(mock_update, mock_context)

    assert result == ConversationHandler.END
    mock_ssh_manager.run_command.assert_called_once_with('test-server', 'ls -la')
    mock_result_message.edit_text.assert_called_once()
    assert "Command completed" in mock_result_message.edit_text.call_args[0][0]
