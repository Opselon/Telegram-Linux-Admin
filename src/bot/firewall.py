"""Purpose: Firewall status and rule management.

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

import shlex

from . import state
from .state import (
    AWAIT_FIREWALL_RULE, ssh_manager,
)

from .support import _edit_message_safely, _extract_user_id, _send_message_safely

logger = logging.getLogger(__name__)


async def firewall_management_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays the firewall management menu."""
    query = update.callback_query
    await query.answer()
    alias = query.data.split('_', 3)[3]
    user_id = _extract_user_id(update)

    keyboard = [
        [InlineKeyboardButton("📜 View Rules", callback_data=f"fw_status_{alias}")],
        [InlineKeyboardButton("➕ Allow Port", callback_data=f"fw_allow_{alias}")],
        [InlineKeyboardButton("➖ Deny Port", callback_data=f"fw_deny_{alias}")],
        [InlineKeyboardButton("🗑️ Delete Rule", callback_data=f"fw_delete_{alias}")],
        [InlineKeyboardButton("🔙 Back to Server Menu", callback_data=f"connect_{alias}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await _edit_message_safely(
        query.message,
        f"**🔥 Firewall Management for {alias}**\n\nSelect an action:",
        user_id,
        reply_markup=reply_markup,
        preformatted=True
    )

async def firewall_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Gets the firewall status from the server."""
    query = update.callback_query
    await query.answer()
    alias = query.data.split('_', 2)[2]
    user_id = update.effective_user.id

    command = "sudo ufw status verbose"
    output = ""
    try:
        output = ""
        async for item, stream in state.ssh_manager.run_command(user_id, alias, command):
            if stream in ('stdout', 'stderr'):
                output += item
        keyboard = [[InlineKeyboardButton("🔙 Back to Firewall Menu", callback_data=f"firewall_management_menu_{alias}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await _edit_message_safely(query.message, f"**🔥 Firewall Status for `{alias}`**\n\n```{output.strip()}```", user_id, reply_markup=reply_markup, preformatted=True)
    except Exception as e:
        await _edit_message_safely(query.message, f"❌ **Error:**\n`{str(e)}`", user_id, preformatted=True)

async def firewall_action_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the firewall management conversation."""
    query = update.callback_query
    await query.answer()

    action = query.data.split('_')[1]
    alias = query.data.split('_', 2)[2]
    user_id = _extract_user_id(update)

    context.user_data['firewall_action'] = action
    context.user_data['alias'] = alias

    if action == "delete":
        prompt = f"Please enter the rule number to `{action}`."
    else:
        prompt = f"Please enter the port number to `{action}`."

    await _edit_message_safely(query.message, prompt, user_id, preformatted=True)
    return AWAIT_FIREWALL_RULE

async def execute_firewall_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Executes the selected firewall action."""
    rule = shlex.quote(update.message.text)
    action = context.user_data['firewall_action']
    alias = context.user_data['alias']
    user_id = update.effective_user.id

    command = f"sudo ufw {action} {rule}"

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

async def cancel_firewall_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the firewall management conversation."""
    user_id = _extract_user_id(update)
    await _send_message_safely(update.message.chat, "Firewall action cancelled.", user_id)
    context.user_data.clear()
    return ConversationHandler.END
