"""Purpose: Static info, resource usage and live monitoring loops.

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

import asyncio

from . import state

from .support import _edit_message_safely, _extract_user_id

logger = logging.getLogger(__name__)


async def server_status_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays the server status menu."""
    query = update.callback_query
    await query.answer()
    alias = query.data.split('_', 3)[3]
    user_id = _extract_user_id(update)

    keyboard = [
        [InlineKeyboardButton("ℹ️ System Info", callback_data=f"static_info_{alias}")],
        [InlineKeyboardButton("📈 Resource Usage", callback_data=f"resource_usage_{alias}")],
        [InlineKeyboardButton("🔴 Live Monitoring", callback_data=f"live_monitoring_{alias}")],
        [InlineKeyboardButton("🔙 Back to Server Menu", callback_data=f"connect_{alias}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await _edit_message_safely(
        query.message,
        f"**📊 Server Status for {alias}**\n\nSelect an option:",
        user_id,
        reply_markup=reply_markup,
        preformatted=True
    )

async def get_static_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Gets static system information from the server."""
    query = update.callback_query
    await query.answer()
    alias = query.data.split('_', 2)[2]
    user_id = update.effective_user.id

    commands = {
        "Kernel": "uname -a",
        "Distro": "lsb_release -a",
        "Uptime": "uptime"
    }

    info_message = f"**ℹ️ System Information for {alias}**\n\n"

    for key, command in commands.items():
        output = ""
        try:
            output = ""
            async for item, stream in state.ssh_manager.run_command(user_id, alias, command):
                if stream in ('stdout', 'stderr'):
                    output += item
            info_message += f"**{key}:**\n```{output.strip()}```\n\n"
        except Exception as e:
            info_message += f"**{key}:**\n`Error fetching info: {str(e)}`\n\n"

    keyboard = [[InlineKeyboardButton("🔙 Back to Status Menu", callback_data=f"server_status_menu_{alias}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await _edit_message_safely(query.message, info_message, user_id, reply_markup=reply_markup, preformatted=True)

async def get_resource_usage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Gets a snapshot of the server's resource usage."""
    query = update.callback_query
    await query.answer()
    alias = query.data.split('_', 2)[2]
    user_id = update.effective_user.id

    commands = {
        "Memory Usage": "free -m",
        "CPU Usage": "top -bn1 | head -n 5"
    }

    usage_message = f"**📈 Resource Usage for {alias}**\n\n"

    for key, command in commands.items():
        output = ""
        try:
            output = ""
            async for item, stream in state.ssh_manager.run_command(user_id, alias, command):
                if stream in ('stdout', 'stderr'):
                    output += item
            usage_message += f"**{key}:**\n```{output.strip()}```\n\n"
        except Exception as e:
            usage_message += f"**{key}:**\n`Error fetching info: {str(e)}`\n\n"

    keyboard = [[InlineKeyboardButton("🔙 Back to Status Menu", callback_data=f"server_status_menu_{alias}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await _edit_message_safely(query.message, usage_message, user_id, reply_markup=reply_markup, preformatted=True)

async def live_monitoring(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Starts live monitoring of the server's resource usage."""
    query = update.callback_query
    await query.answer()
    alias = query.data.split('_', 2)[2]
    user_id = update.effective_user.id

    if user_id in state.MONITORING_TASKS:
        state.MONITORING_TASKS[user_id].cancel()

    keyboard = [[InlineKeyboardButton("⏹️ Stop Monitoring", callback_data=f"stop_live_monitoring_{alias}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    message = await _edit_message_safely(
        query.message,
        f"**🔴 Live Monitoring for {alias}**\n\nStarting...",
        user_id,
        reply_markup=reply_markup,
        preformatted=True
    )

    async def _update_stats():
        while True:
            command = "top -bn1 | head -n 5"
            output = ""
            try:
                output = ""
                async for item, stream in state.ssh_manager.run_command(user_id, alias, command):
                    if stream in ('stdout', 'stderr'):
                        output += item
                await _edit_message_safely(
                    message,
                    f"**🔴 Live Monitoring for {alias}**\n\n```{output.strip()}```",
                    user_id,
                    reply_markup=reply_markup,
                    preformatted=True
                )
            except Exception as e:
                await _edit_message_safely(
                    message,
                    f"**🔴 Live Monitoring for {alias}**\n\n`Error fetching info: {str(e)}`",
                    user_id,
                    reply_markup=reply_markup,
                    preformatted=True
                )
            await asyncio.sleep(5)

    state.MONITORING_TASKS[user_id] = asyncio.create_task(_update_stats())

async def stop_live_monitoring(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stops the live monitoring task."""
    query = update.callback_query
    await query.answer()
    alias = query.data.split('_', 3)[3]
    user_id = update.effective_user.id

    if user_id in state.MONITORING_TASKS:
        state.MONITORING_TASKS[user_id].cancel()
        del state.MONITORING_TASKS[user_id]

    keyboard = [[InlineKeyboardButton("🔙 Back to Status Menu", callback_data=f"server_status_menu_{alias}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await _edit_message_safely(
        query.message,
        f"**🔴 Live Monitoring for {alias}**\n\nMonitoring stopped.",
        user_id,
        reply_markup=reply_markup,
        preformatted=True
    )

async def get_disk_usage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Gets disk usage information from the server."""
    query = update.callback_query
    await query.answer()
    alias = query.data.split('_', 2)[2]
    user_id = update.effective_user.id

    command = "df -h"
    output = ""
    try:
        output = ""
        async for item, stream in state.ssh_manager.run_command(user_id, alias, command):
            if stream in ('stdout', 'stderr'):
                output += item
        keyboard = [[InlineKeyboardButton("🔙 Back to System Commands", callback_data=f"system_commands_menu_{alias}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await _edit_message_safely(query.message, f"**💾 Disk Usage for `{alias}`**\n\n```{output.strip()}```", user_id, reply_markup=reply_markup, preformatted=True)
    except Exception as e:
        await _edit_message_safely(query.message, f"❌ **Error:**\n`{str(e)}`", user_id, preformatted=True)

async def get_network_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Gets network information from the server."""
    query = update.callback_query
    await query.answer()
    alias = query.data.split('_', 2)[2]
    user_id = update.effective_user.id

    command = "ip a"
    output = ""
    try:
        output = ""
        async for item, stream in state.ssh_manager.run_command(user_id, alias, command):
            if stream in ('stdout', 'stderr'):
                output += item
        keyboard = [[InlineKeyboardButton("🔙 Back to System Commands", callback_data=f"system_commands_menu_{alias}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await _edit_message_safely(query.message, f"**🌐 Network Info for `{alias}`**\n\n```{output.strip()}```", user_id, reply_markup=reply_markup, preformatted=True)
    except Exception as e:
        await _edit_message_safely(query.message, f"❌ **Error:**\n`{str(e)}`", user_id, preformatted=True)

async def get_open_ports(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Gets open ports from the server."""
    query = update.callback_query
    await query.answer()
    alias = query.data.split('_', 2)[2]
    user_id = update.effective_user.id

    command = "ss -tuln"
    output = ""
    try:
        output = ""
        async for item, stream in state.ssh_manager.run_command(user_id, alias, command):
            if stream in ('stdout', 'stderr'):
                output += item
        keyboard = [[InlineKeyboardButton("🔙 Back to System Commands", callback_data=f"system_commands_menu_{alias}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await _edit_message_safely(query.message, f"**🔌 Open Ports for `{alias}`**\n\n```{output.strip()}```", user_id, reply_markup=reply_markup, preformatted=True)
    except Exception as e:
        await _edit_message_safely(query.message, f"❌ **Error:**\n`{str(e)}`", user_id, preformatted=True)
