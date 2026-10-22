"""Purpose: Remote file manager: list, download, upload.

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
    AWAIT_FILE_PATH, AWAIT_UPLOAD_FILE, ssh_manager,
)

from .support import _edit_message_safely, _extract_user_id, _send_message_safely

logger = logging.getLogger(__name__)


async def file_manager_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays the file manager menu."""
    query = update.callback_query
    await query.answer()
    alias = query.data.split('_', 3)[3]
    user_id = _extract_user_id(update)

    keyboard = [
        [InlineKeyboardButton("📜 List Files", callback_data=f"fm_ls_{alias}")],
        [InlineKeyboardButton("📥 Download File", callback_data=f"fm_download_{alias}")],
        [InlineKeyboardButton("📤 Upload File", callback_data=f"fm_upload_{alias}")],
        [InlineKeyboardButton("🔙 Back to Server Menu", callback_data=f"connect_{alias}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await _edit_message_safely(
        query.message,
        f"**📁 File Manager for {alias}**\n\nSelect an action:",
        user_id,
        reply_markup=reply_markup,
        preformatted=True
    )

async def file_manager_action_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts a conversation for a file manager action."""
    query = update.callback_query
    await query.answer()

    action = query.data.split('_')[1]
    alias = query.data.split('_', 2)[2]
    user_id = _extract_user_id(update)

    context.user_data['file_manager_action'] = action
    context.user_data['alias'] = alias

    if action == "ls":
        prompt = "Please enter the directory path to list."
    elif action == "download":
        prompt = "Please enter the full path of the file to download."
    elif action == "upload":
        await _edit_message_safely(query.message, f"Please enter the full destination path on `{alias}`.", user_id, preformatted=True)
        return AWAIT_FILE_PATH

    await _edit_message_safely(query.message, prompt, user_id)
    return AWAIT_FILE_PATH

async def file_manager_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Dispatches to the correct file manager function based on the action."""
    action = context.user_data.get('file_manager_action')
    user_id = _extract_user_id(update)
    if action == "ls":
        return await list_files(update, context)
    elif action == "download":
        return await download_file(update, context)
    elif action == "upload":
        return await upload_file_path(update, context)
    else:
        await _send_message_safely(update.message.chat, "Unknown file manager action.", user_id)
        return ConversationHandler.END

async def list_files(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Lists files in a directory."""
    path = shlex.quote(update.message.text)
    alias = context.user_data['alias']
    user_id = update.effective_user.id
    command = f"ls -la {path}"

    result_message = await _send_message_safely(update.message.chat, f"Running `{command}` on `{alias}`...", user_id, preformatted=True)

    output = ""
    try:
        output = ""
        async for item, stream in state.ssh_manager.run_command(user_id, alias, command):
            if stream in ('stdout', 'stderr'):
                output += item
        final_message = f"✅ **Files in `{path}` on `{alias}`**\n\n```{output.strip()}```"
        if len(final_message) > 4096:
            with tempfile.NamedTemporaryFile(mode="w+", delete=False, suffix=".txt") as f:
                f.write(output)
                f.flush()
            await result_message.delete()
            await update.message.reply_document(document=open(f.name, "rb"), caption=f"File list for `{path}`")
            os.remove(f.name)
        else:
            await _edit_message_safely(result_message, final_message, user_id, preformatted=True)
    except Exception as e:
        await _edit_message_safely(result_message, f"❌ **Error:**\n`{str(e)}`", user_id, preformatted=True)

    context.user_data.clear()
    return ConversationHandler.END

async def download_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Downloads a file from the server."""
    remote_path = shlex.quote(update.message.text)
    alias = context.user_data['alias']
    user_id = update.effective_user.id

    await _send_message_safely(update.message.chat, f"📥 Downloading `{remote_path}` from `{alias}`...", user_id, preformatted=True)

    try:
        with tempfile.NamedTemporaryFile(delete=False) as f:
            local_path = f.name
        await state.ssh_manager.download_file(user_id, alias, remote_path, local_path)
        await update.message.reply_document(document=open(local_path, 'rb'))
        os.remove(local_path)
    except Exception as e:
        await _send_message_safely(update.message.chat, f"❌ **Error:**\n`{e}`", user_id, preformatted=True)

    context.user_data.clear()
    return ConversationHandler.END

async def upload_file_path(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Gets the destination path for the file upload."""
    context.user_data['remote_path'] = update.message.text
    user_id = _extract_user_id(update)
    await _send_message_safely(update.message.chat, "Okay, now please upload the file to be sent to the server.", user_id)
    return AWAIT_UPLOAD_FILE

async def upload_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Uploads a file to the server."""
    document = update.message.document
    alias = context.user_data['alias']
    remote_path = context.user_data['remote_path']
    user_id = update.effective_user.id

    local_path = document.file_name
    file = await document.get_file()
    await file.download_to_drive(local_path)

    await _send_message_safely(update.message.chat, f"📤 Uploading `{local_path}` to `{remote_path}` on `{alias}`...", user_id, preformatted=True)

    try:
        await state.ssh_manager.upload_file(user_id, alias, local_path, remote_path)
        await _send_message_safely(update.message.chat, "✅ **File uploaded successfully!**", user_id, preformatted=True)
    except Exception as e:
        await _send_message_safely(update.message.chat, f"❌ **Error:**\n`{e}`", user_id, preformatted=True)
    finally:
        os.remove(local_path)

    context.user_data.clear()
    return ConversationHandler.END

async def cancel_file_manager_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the file manager action conversation."""
    user_id = _extract_user_id(update)
    await _send_message_safely(update.message.chat, "File manager action cancelled.", user_id)
    context.user_data.clear()
    return ConversationHandler.END
