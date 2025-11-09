import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from telegram import Update, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from src import main

@pytest.fixture
def authorized_update():
    """Creates a mock update object that will pass the @authorized decorator."""
    update = AsyncMock(spec=Update)
    update.effective_user = MagicMock(id=12345)
    update.callback_query = AsyncMock()
    update.callback_query.from_user = MagicMock(id=12345)
    return update

@pytest.mark.asyncio
@patch('src.main.config')
async def test_system_commands_menu(mock_config, authorized_update):
    """Test that the system commands menu is displayed correctly."""
    mock_config.whitelisted_users = [12345]
    authorized_update.callback_query.data = "system_commands_menu_test_alias"

    await main.system_commands_menu(authorized_update, MagicMock(spec=ContextTypes.DEFAULT_TYPE))

    authorized_update.callback_query.edit_message_text.assert_called_once()
    call_args = authorized_update.callback_query.edit_message_text.call_args
    assert "**⚙️ System Commands for test_alias**" in call_args[0][0]

    reply_markup = call_args[1]['reply_markup']
    assert isinstance(reply_markup, InlineKeyboardMarkup)
    buttons = reply_markup.inline_keyboard
    assert len(buttons) == 5
    assert buttons[0][0].text == "💾 Disk Usage"
    assert buttons[1][0].text == "🌐 Network Info"
    assert buttons[2][0].text == "🔄 Reboot"
    assert buttons[3][0].text == " Shutdown"

@pytest.mark.asyncio
@patch('src.main.config')
async def test_confirm_system_command(mock_config, authorized_update):
    """Test that the confirmation message is shown for reboot."""
    mock_config.whitelisted_users = [12345]
    authorized_update.callback_query.data = "reboot_test_alias"

    await main.confirm_system_command(authorized_update, MagicMock(spec=ContextTypes.DEFAULT_TYPE))

    authorized_update.callback_query.edit_message_text.assert_called_once()
    call_args = authorized_update.callback_query.edit_message_text.call_args
    assert "**⚠️ Are you sure you want to reboot the server `test_alias`?**" in call_args[0][0]

    reply_markup = call_args[1]['reply_markup']
    buttons = reply_markup.inline_keyboard
    assert buttons[0][0].text == "✅ Yes, reboot"
    assert buttons[0][1].text == "❌ No"

@pytest.mark.asyncio
@patch('src.main.ssh_manager', new_callable=AsyncMock)
@patch('src.main.config')
async def test_execute_system_command_reboot(mock_config, mock_ssh_manager, authorized_update):
    """Test executing the reboot command after confirmation."""
    mock_config.whitelisted_users = [12345]
    authorized_update.callback_query.data = "execute_reboot_test_alias"

    async def mock_run_command_gen(*args, **kwargs):
        yield "output", "stdout"

    mock_ssh_manager.run_command = mock_run_command_gen

    await main.execute_system_command(authorized_update, MagicMock(spec=ContextTypes.DEFAULT_TYPE))

    authorized_update.callback_query.edit_message_text.assert_called_once_with(
        "✅ **Command `reboot` sent to `test_alias` successfully.**",
        parse_mode='Markdown'
    )

@pytest.mark.asyncio
@patch('src.main.ssh_manager', new_callable=AsyncMock)
@patch('src.main.config')
async def test_get_disk_usage(mock_config, mock_ssh_manager, authorized_update):
    """Test getting disk usage."""
    mock_config.whitelisted_users = [12345]
    authorized_update.callback_query.data = "disk_usage_test_alias"

    async def mock_run_command_gen(alias, command):
        assert alias == "test_alias"
        assert command == "df -h"
        yield "Filesystem      Size  Used Avail Use% Mounted on\n/dev/sda1        20G   10G   10G  50% /", "stdout"

    mock_ssh_manager.run_command = mock_run_command_gen

    await main.get_disk_usage(authorized_update, MagicMock(spec=ContextTypes.DEFAULT_TYPE))

    authorized_update.callback_query.edit_message_text.assert_called_once()
    call_args = authorized_update.callback_query.edit_message_text.call_args
    assert "**💾 Disk Usage for `test_alias`**" in call_args[0][0]
    assert "Filesystem" in call_args[0][0]

@pytest.mark.asyncio
@patch('src.main.ssh_manager', new_callable=AsyncMock)
@patch('src.main.config')
async def test_get_network_info(mock_config, mock_ssh_manager, authorized_update):
    """Test getting network info."""
    mock_config.whitelisted_users = [12345]
    authorized_update.callback_query.data = "network_info_test_alias"

    async def mock_run_command_gen(alias, command):
        assert alias == "test_alias"
        assert command == "ip a"
        yield "1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue state UNKNOWN group default qlen 1000", "stdout"

    mock_ssh_manager.run_command = mock_run_command_gen

    await main.get_network_info(authorized_update, MagicMock(spec=ContextTypes.DEFAULT_TYPE))

    authorized_update.callback_query.edit_message_text.assert_called_once()
    call_args = authorized_update.callback_query.edit_message_text.call_args
    assert "**🌐 Network Info for `test_alias`**" in call_args[0][0]
    assert "LOOPBACK" in call_args[0][0]
