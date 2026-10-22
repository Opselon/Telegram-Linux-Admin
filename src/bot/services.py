"""Purpose: systemd service management.

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
from telegram.ext import ConversationHandler

from . import state
from .state import (
    AWAIT_SERVICE_NAME, ssh_manager,
)

from .support import _edit_message_safely, _extract_user_id, _send_message_safely

logger = logging.getLogger(__name__)


async def service_management_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays the service management menu."""
    query = update.callback_query
    await query.answer()
    alias = query.data.split('_', 3)[3]
    user_id = _extract_user_id(update)

    keyboard = [
        [InlineKeyboardButton("🔍 Check Service Status", callback_data=f"check_service_{alias}")],
        [InlineKeyboardButton("▶️ Start a Service", callback_data=f"start_service_{alias}")],
        [InlineKeyboardButton("⏹️ Stop a Service", callback_data=f"stop_service_{alias}")],
        [InlineKeyboardButton("🔄 Restart a Service", callback_data=f"restart_service_{alias}")],
        [InlineKeyboardButton("🔙 Back to Server Menu", callback_data=f"connect_{alias}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await _edit_message_safely(
        query.message,
        f"**🔧 Service Management for {alias}**\n\nSelect an action:",
        user_id,
        reply_markup=reply_markup,
        preformatted=True
    )

async def service_action_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the service management conversation."""
    query = update.callback_query
    await query.answer()

    action = query.data.split('_')[0]
    alias = query.data.split('_', 2)[2]
    user_id = _extract_user_id(update)

    context.user_data['service_action'] = action
    context.user_data['alias'] = alias

    await _edit_message_safely(query.message, f"Please enter the name of the service to `{action}`.", user_id, preformatted=True)
    return AWAIT_SERVICE_NAME

async def execute_service_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Executes the selected service action."""
    service_name = update.message.text
    action = context.user_data['service_action']
    alias = context.user_data['alias']
    user_id = update.effective_user.id

    command = f"systemctl {action} {service_name}"

    result_message = await _send_message_safely(update.message.chat, f"Running `{command}` on `{alias}`...", user_id, preformatted=True)

    output = ""
    try:
        output = ""
        async for item, stream in state.ssh_manager.run_command(user_id, alias, command):
            if stream in ('stdout', 'stderr'):
                output += item
        await _edit_message_safely(result_message, f"✅ **Command completed on `{alias}`**\n\n```{output.strip()}```", user_id, preformatted=True)
    except Exception as e:
        await _edit_message_safely(result_message, f"❌ **Error:**\n`{str(e)}`", user_id, preformatted=True)

    context.user_data.clear()
    return ConversationHandler.END

async def cancel_service_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the service management conversation."""
    user_id = _extract_user_id(update)
    await _send_message_safely(update.message.chat, "Service action cancelled.", user_id)
    context.user_data.clear()
    return ConversationHandler.END
