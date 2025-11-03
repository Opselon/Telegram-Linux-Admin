import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from src.main import execute_shell_command

@pytest.mark.asyncio
async def test_execute_shell_command_real_time_output():
    """Test the real-time output and message editing logic."""
    # Mock update and context objects
    update = MagicMock()
    update.message.from_user.id = 123
    update.message.reply_text = AsyncMock()

    context = MagicMock()

    # Mock the initial message sent by the bot
    mock_message = MagicMock()
    mock_message.edit_text = AsyncMock()
    update.message.reply_text.return_value = mock_message

    # Mock the SSH manager and its run_command method
    with patch('src.main.ssh_manager', new_callable=AsyncMock) as mock_ssh_manager:
        async def mock_run_command(alias, command):
            yield "line1\n", "stdout"
            yield "line2\n", "stdout"
            yield "error1\n", "stderr"

        mock_ssh_manager.run_command = mock_run_command

        # Mock user_connections
        with patch('src.main.user_connections', {123: 'test_server'}):
            await execute_shell_command(update, context, "ls -l")

            # Assert that the initial message was sent
            update.message.reply_text.assert_called_once()

            # Assert that edit_text was called to update the message
            assert mock_message.edit_text.call_count > 0

            # Check the final message content
            final_call_args = mock_message.edit_text.call_args[0][0]
            assert "--- command finished ---" in final_call_args
            assert "line1" in final_call_args
            assert "line2" in final_call_args
            assert "error1" in final_call_args
