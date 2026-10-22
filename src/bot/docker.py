"""Purpose: Docker management menus and actions.

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
    AWAIT_CONTAINER_NAME, ssh_manager,
)

from .support import _edit_message_safely, _extract_user_id, _send_message_safely

logger = logging.getLogger(__name__)


async def docker_management_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays the Docker management menu."""
    query = update.callback_query
    await query.answer()
    alias = query.data.split('_', 3)[3]
    user_id = _extract_user_id(update)

    keyboard = [
        [InlineKeyboardButton("📜 List Containers", callback_data=f"docker_ps_{alias}")],
        [InlineKeyboardButton("📜 List All Containers", callback_data=f"docker_ps_a_{alias}")],
        [InlineKeyboardButton("📄 View Logs", callback_data=f"docker_logs_{alias}")],
        [InlineKeyboardButton("▶️ Start Container", callback_data=f"docker_start_{alias}")],
        [InlineKeyboardButton("⏹️ Stop Container", callback_data=f"docker_stop_{alias}")],
        [InlineKeyboardButton("🔙 Back to Server Menu", callback_data=f"connect_{alias}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await _edit_message_safely(
        query.message,
        f"**🐳 Docker Management for {alias}**\n\nSelect an action:",
        user_id,
        reply_markup=reply_markup,
        preformatted=True
    )

async def docker_action_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts a conversation for a Docker action that requires a container name."""
    query = update.callback_query
    await query.answer()

    action = query.data.split('_')[1]
    alias = query.data.split('_', 2)[2]
    user_id = _extract_user_id(update)

    context.user_data['docker_action'] = action
    context.user_data['alias'] = alias

    await _edit_message_safely(query.message, f"Please enter the name or ID of the container to `{action}`.", user_id, preformatted=True)
    return AWAIT_CONTAINER_NAME

async def execute_docker_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Executes a Docker action on a specific container."""
    container_name = shlex.quote(update.message.text)
    action = context.user_data['docker_action']
    alias = context.user_data['alias']
    user_id = update.effective_user.id

    command = f"docker {action} {container_name}"

    result_message = await _send_message_safely(update.message.chat, f"Running `{command}` on `{alias}`...", user_id, preformatted=True)

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
            await update.message.reply_document(document=open(f.name, "rb"), caption=f"Command output for `{command}`")
            os.remove(f.name)
        else:
            await _edit_message_safely(result_message, final_message, user_id, preformatted=True)
    except Exception as e:
        await _edit_message_safely(result_message, f"❌ **Error:**\n`{str(e)}`", user_id, preformatted=True)

    context.user_data.clear()
    return ConversationHandler.END

async def docker_ps(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Lists Docker containers."""
    query = update.callback_query
    await query.answer()

    command = "docker ps"
    if query.data.startswith("docker_ps_a"):
        command += " -a"
    alias = query.data.split('_', 2)[-1]
    user_id = update.effective_user.id

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

async def cancel_docker_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the Docker action conversation."""
    user_id = _extract_user_id(update)
    await _send_message_safely(update.message.chat, "Docker action cancelled.", user_id)
    context.user_data.clear()
    return ConversationHandler.END
