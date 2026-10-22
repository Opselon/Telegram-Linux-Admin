"""Purpose: Remote process listing and termination.

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
    AWAIT_PID, ssh_manager,
)

from .support import _edit_message_safely, _extract_user_id, _send_message_safely

logger = logging.getLogger(__name__)


async def process_management_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays the process management menu."""
    query = update.callback_query
    await query.answer()
    alias = query.data.split('_', 3)[3]
    user_id = _extract_user_id(update)

    keyboard = [
        [InlineKeyboardButton("📜 List Processes", callback_data=f"ps_aux_{alias}")],
        [InlineKeyboardButton("❌ Kill Process", callback_data=f"kill_process_{alias}")],
        [InlineKeyboardButton("🔙 Back to Server Menu", callback_data=f"connect_{alias}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await _edit_message_safely(
        query.message,
        f"**⚙️ Process Management for {alias}**\n\nSelect an action:",
        user_id,
        reply_markup=reply_markup,
        preformatted=True
    )

async def list_processes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Lists running processes on the server."""
    query = update.callback_query
    await query.answer()
    alias = query.data.split('_', 2)[2]
    user_id = update.effective_user.id

    command = "ps aux"
    result_message = await _edit_message_safely(query.message, f"Running `{command}` on `{alias}`...", user_id, preformatted=True)

    output = ""
    try:
        output = ""
        async for item, stream in state.ssh_manager.run_command(user_id, alias, command):
            if stream in ('stdout', 'stderr'):
                output += item
        final_message = f"✅ **Command completed on `{alias}`**\n\n```{output.strip()}```"
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

async def kill_process_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the conversation to kill a process."""
    query = update.callback_query
    await query.answer()

    alias = query.data.split('_', 2)[2]
    context.user_data['alias'] = alias
    user_id = _extract_user_id(update)

    await _edit_message_safely(query.message, f"Please enter the PID of the process to kill on `{alias}`.", user_id, preformatted=True)
    return AWAIT_PID

async def execute_kill_process(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Executes the kill command."""
    pid = shlex.quote(update.message.text)
    alias = context.user_data['alias']
    user_id = update.effective_user.id

    command = f"kill -9 {pid}"

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

async def cancel_kill_process(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the kill process conversation."""
    user_id = _extract_user_id(update)
    await _send_message_safely(update.message.chat, "Kill process cancelled.", user_id)
    context.user_data.clear()
    return ConversationHandler.END
