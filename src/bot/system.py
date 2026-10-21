"""Purpose: Power/system commands.

OWNER: src/bot/<this module>.
CONSUMES: src/bot/state.py (mutable singletons, conversation states,
  leaf helpers) and the sibling handler modules listed below.
PROVIDES: python-telegram-bot handlers registered by
  src/bot/application.py.
INVARIANTS: handler logic and user-visible behavior are unchanged from
  3.1; only the import graph was made explicit. Mutable runtime state
  is read/written through ``state`` so a patched singleton is seen here.
EXTEND: add a handler in this module, then register it in
  src/bot/application.py.
"""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton
from telegram import InlineKeyboardMarkup
from telegram import Update
from telegram.ext import ContextTypes

from . import state

from .support import _edit_message_safely, _extract_user_id

logger = logging.getLogger(__name__)


async def system_commands_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays the system commands menu."""
    query = update.callback_query
    await query.answer()
    alias = query.data.split('_', 3)[3]
    user_id = _extract_user_id(update)

    keyboard = [
        [InlineKeyboardButton("💾 Disk Usage", callback_data=f"disk_usage_{alias}")],
        [InlineKeyboardButton("🌐 Network Info", callback_data=f"network_info_{alias}")],
        [InlineKeyboardButton("🔌 Open Ports", callback_data=f"open_ports_{alias}")],
        [InlineKeyboardButton("🔄 Reboot", callback_data=f"reboot_{alias}")],
        [InlineKeyboardButton(" Shutdown", callback_data=f"shutdown_{alias}")],
        [InlineKeyboardButton("🔙 Back to Server Menu", callback_data=f"connect_{alias}")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await _edit_message_safely(
        query.message,
        f"**⚙️ System Commands for {alias}**\n\nSelect an action:",
        user_id,
        reply_markup=reply_markup,
        preformatted=True
    )

async def confirm_system_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Asks for confirmation before executing a system command."""
    query = update.callback_query
    await query.answer()

    action = query.data.split('_')[0]
    alias = query.data.split('_', 1)[1]
    user_id = _extract_user_id(update)

    keyboard = [
        [
            InlineKeyboardButton(f"✅ Yes, {action}", callback_data=f"execute_{action}_{alias}"),
            InlineKeyboardButton("❌ No", callback_data=f"system_commands_menu_{alias}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await _edit_message_safely(
        query.message,
        f"**⚠️ Are you sure you want to {action} the server `{alias}`?**",
        user_id,
        reply_markup=reply_markup,
        preformatted=True
    )

async def execute_system_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Executes a system command (reboot, shutdown) after confirmation."""
    query = update.callback_query
    await query.answer()

    _, action, alias = query.data.split('_', 2)
    user_id = update.effective_user.id

    if action == "reboot":
        command = "reboot"
    elif action == "shutdown":
        command = "shutdown now"
    else:
        return

    try:
        # We only need to start the command, not wait for output
        async for _, __ in state.ssh_manager.run_command(user_id, alias, f"sudo {command}"):
            pass
        await _edit_message_safely(query.message, f"✅ **Command `{command}` sent to `{alias}` successfully.**", user_id, preformatted=True)
    except Exception as e:
        await _edit_message_safely(query.message, f"❌ **Error:**\n`{e}`", user_id, preformatted=True)
