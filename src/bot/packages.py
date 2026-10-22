"""Purpose: OS package management menus and actions.

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

import os
import shlex
import tempfile

from . import state
from .state import (
    AWAIT_PACKAGE_NAME, ssh_manager,
)

from .support import _edit_message_safely, _extract_user_id, _send_message_safely

logger = logging.getLogger(__name__)


async def package_management_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays the package management menu."""
    query = update.callback_query
    await query.answer()
    alias = query.data.split('_', 3)[3]
    user_id = _extract_user_id(update)

    keyboard = [
        [InlineKeyboardButton("🔄 Update Package Lists", callback_data=f"pkg_update_{alias}")],
        [InlineKeyboardButton("⬆️ Upgrade All Packages", callback_data=f"pkg_upgrade_{alias}")],
        [InlineKeyboardButton("➕ Install a Package", callback_data=f"pkg_install_{alias}")],
        [InlineKeyboardButton("🔙 Back to Server Menu", callback_data=f"connect_{alias}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await _edit_message_safely(
        query.message,
        f"**📦 Package Management for {alias}**\n\nSelect an action:",
        user_id,
        reply_markup=reply_markup,
        preformatted=True
    )

async def package_manager_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles package management actions like update and upgrade."""
    query = update.callback_query
    await query.answer()

    _, action, alias = query.data.split('_', 2)
    user_id = update.effective_user.id

    if action == "update":
        command = "sudo apt-get update"
    elif action == "upgrade":
        command = "sudo apt-get upgrade -y"
    else:
        return

    result_message = await _edit_message_safely(query.message, f"Running `{command}` on `{alias}`...", user_id, preformatted=True)

    output = ""
    try:
        output = ""
        async for item, stream in state.ssh_manager.run_command(user_id, alias, command):
            if stream in ('stdout', 'stderr'):
                output += item
        final_message = f"✅ **Command completed on `{alias}`**\n\n```\n{output.strip()}\n```"
        if len(final_message) > 4096:
            with tempfile.NamedTemporaryFile(mode="w+", delete=False, suffix=".txt") as f:
                f.write(output)
                f.flush()
            await result_message.delete()
            await query.message.reply_document(document=open(f.name, "rb"), caption=f"Command output for `{command}`")
            os.remove(f.name)
        else:
            await _edit_message_safely(result_message, final_message, user_id, preformatted=True)

    except Exception as e:
        await _edit_message_safely(result_message, f"❌ **Error:**\n`{str(e)}`", user_id, preformatted=True)

async def install_package_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the conversation to install a package."""
    query = update.callback_query
    await query.answer()

    alias = query.data.split('_', 2)[2]
    context.user_data['alias'] = alias
    user_id = _extract_user_id(update)

    await _edit_message_safely(query.message, f"Please enter the name of the package to install on `{alias}`.", user_id, preformatted=True)
    return AWAIT_PACKAGE_NAME

async def execute_install_package(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Executes the package installation."""
    package_name = shlex.quote(update.message.text)
    alias = context.user_data['alias']
    user_id = update.effective_user.id

    command = f"sudo apt-get install -y {package_name}"

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

async def cancel_install_package(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the package installation conversation."""
    user_id = _extract_user_id(update)
    await _send_message_safely(update.message.chat, "Package installation cancelled.", user_id)
    context.user_data.clear()
    return ConversationHandler.END
