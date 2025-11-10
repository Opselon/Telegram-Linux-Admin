import logging
import json
import asyncio
import os
import sys
import zipfile
import tempfile
import shlex
import subprocess
import socket
import traceback
from datetime import datetime
from telegram import BotCommand, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, ContextTypes,
    CallbackQueryHandler, ConversationHandler
)
from telegram.error import BadRequest, InvalidToken
import asyncssh
from .ssh_manager import SSHManager
from .database import (
    get_all_servers, initialize_database,
    close_db_connection, add_server, remove_server
)
from .config import config
from functools import wraps
from typing import Optional

# --- Globals & Logging ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

ssh_manager = None
user_connections = {}
RESTORING = False
SHELL_MODE_USERS = set()
DEBUG_MODE = False
LOCK_FILE = "bot.lock"
MONITORING_TASKS = {}

# --- Conversation States ---
(
    AWAIT_COMMAND, ALIAS, HOSTNAME, USER, AUTH_METHOD, PASSWORD, KEY_PATH,
    AWAIT_RESTORE_CONFIRMATION, AWAIT_RESTORE_FILE, AWAIT_SERVICE_NAME, AWAIT_PACKAGE_NAME, AWAIT_CONTAINER_NAME,
    AWAIT_FILE_PATH, AWAIT_UPLOAD_FILE, AWAIT_PID, AWAIT_FIREWALL_RULE
) = range(16)

# --- Authorization ---
def _extract_user_id(update: Update) -> Optional[int]:
    """Safely extract a user id from any kind of update."""
    if update is None:
        return None
    if update.effective_user:
        return update.effective_user.id
    if update.callback_query and update.callback_query.from_user:
        return update.callback_query.from_user.id
    if update.message and update.message.from_user:
        return update.message.from_user.id
    return None

def authorized(func):
    """Decorator to check if the user is authorized."""
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = _extract_user_id(update)
        if user_id is None:
            logger.error("Unable to extract user id from update; denying access for safety.")
            if update and update.callback_query:
                await update.callback_query.answer("🚫 Unable to identify user.", show_alert=True)
            elif update and update.effective_chat:
                await update.effective_chat.send_message("🚫 **Access Denied**\nWe could not verify your identity.", parse_mode='Markdown')
            return

        await send_debug_message(update, f"Checking authorization for user_id: {user_id}...")
        if user_id not in config.whitelisted_users:
            logger.warning(f"Unauthorized access denied for user_id: {user_id}")
            await send_debug_message(update, f"Unauthorized access denied for user_id: {user_id}.")
            if update.callback_query:
                await update.callback_query.answer("🚫 You are not authorized to use this bot.", show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text("🚫 **Access Denied**\nYou are not authorized.", parse_mode='Markdown')
            return

        await send_debug_message(update, "Authorization successful.")
        return await func(update, context, *args, **kwargs)
    return wrapped

def admin_authorized(func):
    """Decorator to check if the user is the admin (first whitelisted user)."""
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = _extract_user_id(update)
        # Ensure there is a whitelist and the user is the first one in it
        if not config.whitelisted_users or user_id != config.whitelisted_users[0]:
            logger.warning(f"Admin access denied for user_id: {user_id}")
            if update.callback_query:
                await update.callback_query.answer("🚫 You are not authorized for this admin-only action.", show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text("🚫 **Access Denied**\nThis is an admin-only feature.", parse_mode='Markdown')
            return
        return await func(update, context, *args, **kwargs)
    return authorized(wrapped) # Chain with the general authorization check

def _resolve_message(update: Update):
    if update.message:
        return update.message
    if update.callback_query:
        return update.callback_query.message
    return None

# --- Add/Remove Server ---
@admin_authorized
async def add_server_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the conversation to add a new server."""
    prompt = (
        "🖥️ **Add a New Server**\n\n"
        "Let's add a new server. First, what is the short alias for this server? (e.g., `webserver`)"
    )

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(prompt, parse_mode='Markdown')
    else:
        await update.effective_message.reply_text(prompt, parse_mode='Markdown')

    return ALIAS


async def get_alias(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['alias'] = update.message.text
    await update.message.reply_text("Great. Now, what is the hostname or IP address?")
    return HOSTNAME

async def get_hostname(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['hostname'] = update.message.text
    await update.message.reply_text("And the SSH username?")
    return USER

async def get_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['user'] = update.message.text
    keyboard = [[InlineKeyboardButton("🔑 Key", callback_data='key'), InlineKeyboardButton("🔒 Password", callback_data='password')]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("How would you like to authenticate?", reply_markup=reply_markup)
    return AUTH_METHOD

async def get_auth_method(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == 'password':
        await query.message.reply_text("Please enter the SSH password.")
        return PASSWORD
    else:
        await query.message.reply_text("Please enter the full path to your SSH private key on the server running this bot.")
        return KEY_PATH

async def get_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['password'] = update.message.text
    await save_server(update, context)
    return ConversationHandler.END

async def get_key_path(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['key_path'] = update.message.text
    await save_server(update, context)
    return ConversationHandler.END

async def save_server(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Saves the server to the database."""
    try:
        add_server(
            alias=context.user_data['alias'],
            hostname=context.user_data['hostname'],
            user=context.user_data['user'],
            password=context.user_data.get('password'),
            key_path=context.user_data.get('key_path')
        )
        ssh_manager.refresh_server_configs()
        await update.message.reply_text(f"✅ **Server '{context.user_data['alias']}' added successfully!**", parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ **Error:** {e}")
    finally:
        context.user_data.clear()

async def cancel_add_server(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the add server conversation."""
    await update.message.reply_text("Server addition cancelled.")
    context.user_data.clear()
    return ConversationHandler.END

@admin_authorized
async def remove_server_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays a menu of servers to remove."""
    servers = get_all_servers()
    if not servers:
        message = "🤷 **No servers to remove.**"
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(message, parse_mode='Markdown')
        else:
            await update.effective_message.reply_text(message, parse_mode='Markdown')
        return

    keyboard = [
        [InlineKeyboardButton(f"Remove {server['alias']}", callback_data=f"remove_{server['alias']}")]
        for server in servers
    ]
    keyboard.append([InlineKeyboardButton("Cancel", callback_data="main_menu")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            '🗑️ **Select a server to remove:**',
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    else:
        await update.effective_message.reply_text(
            '🗑️ **Select a server to remove:**',
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

# --- Navigation ---
@authorized
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends the main menu."""
    user_id = update.effective_user.id
    logger.info(f"User {user_id} initiated /start command.")
    await main_menu(update, context)

async def main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays the main menu with options, dynamically showing admin buttons."""
    user_id = _extract_user_id(update)
    logger.info(f"Displaying main menu for user_id: {user_id}.")

    # Base keyboard for all users
    keyboard = [
        [InlineKeyboardButton("🔌 Connect to a Server", callback_data='connect_server_menu')],
    ]

    # Admin-only buttons
    if config.whitelisted_users and user_id == config.whitelisted_users[0]:
        admin_buttons = [
            [
                InlineKeyboardButton("➕ Add Server", callback_data='add_server_start'),
                InlineKeyboardButton("➖ Remove Server", callback_data='remove_server_menu')
            ],
            [
                InlineKeyboardButton("💾 Backup", callback_data='backup'),
                InlineKeyboardButton("🔄 Restore", callback_data='restore')
            ],
            [InlineKeyboardButton("🔄 Update Bot", callback_data='update_bot')],
        ]
        keyboard.extend(admin_buttons)

    reply_markup = InlineKeyboardMarkup(keyboard)
    menu_text = "🐧 **Welcome to your Linux Admin Bot!**\n\nWhat would you like to do?"

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(menu_text, reply_markup=reply_markup, parse_mode='Markdown')
    else:
        await update.message.reply_text(menu_text, reply_markup=reply_markup, parse_mode='Markdown')


# --- Server Connection ---
@authorized
async def connect_server_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays a menu of servers to connect to."""
    logger.info("Displaying connect server menu.")
    servers = get_all_servers()
    if not servers:
        await update.callback_query.answer("No servers configured. Add one first!", show_alert=True)
        return

    keyboard = []
    for server in servers:
        keyboard.append([InlineKeyboardButton(f"🖥️ {server['alias']}", callback_data=f"connect_{server['alias']}")])
    keyboard.append([InlineKeyboardButton("🔙 Back to Main Menu", callback_data='main_menu')])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.answer()
    await update.callback_query.edit_message_text('**Select a server to connect to:**', reply_markup=reply_markup, parse_mode='Markdown')

@authorized
async def handle_server_connection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the connection to a selected server and shows the command menu."""
    query = update.callback_query
    await query.answer()
    alias = query.data.split('_', 1)[1]
    user_id = update.effective_user.id

    try:
        await query.edit_message_text(f"🔌 **Connecting to {alias}...**", parse_mode='Markdown')

        # Establish the SSH connection by running a simple command
        async for _, __ in ssh_manager.run_command(alias, "echo 'Connection successful'"):
            pass

        # Store the active connection alias for the user
        user_connections[user_id] = alias

        logger.info(f"User {user_id} connected to server '{alias}'.")

        keyboard = [
            [InlineKeyboardButton("▶️ Run a Command", callback_data=f"run_command_{alias}")],
            [InlineKeyboardButton("🖥️ Open Interactive Shell", callback_data=f"start_shell_{alias}")],
            [InlineKeyboardButton("📊 Server Status", callback_data=f"server_status_menu_{alias}")],
            [InlineKeyboardButton("🔧 Service Management", callback_data=f"service_management_menu_{alias}")],
            [InlineKeyboardButton("📦 Package Management", callback_data=f"package_management_menu_{alias}")],
            [InlineKeyboardButton("🐳 Docker Management", callback_data=f"docker_management_menu_{alias}")],
            [InlineKeyboardButton("📁 File Manager", callback_data=f"file_manager_menu_{alias}")],
            [InlineKeyboardButton("⚙️ Process Management", callback_data=f"process_management_menu_{alias}")],
            [InlineKeyboardButton("🔥 Firewall Management", callback_data=f"firewall_management_menu_{alias}")],
            [InlineKeyboardButton("⚙️ System Commands", callback_data=f"system_commands_menu_{alias}")],
            [InlineKeyboardButton("🔌 Disconnect", callback_data=f"disconnect_{alias}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            f"✅ **Connected to {alias}!**\n\nWhat would you like to do?",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    # --- User-Friendly Error Handling ---
    # Catch common, understandable errors and provide clear feedback to the user.
    except asyncssh.PermissionDenied:
        error_message = "❌ **Authentication Failed:**\nPermission denied. This is likely due to an incorrect username, password, or SSH key."
        logger.error(f"Authentication failed for {alias}")
        await query.edit_message_text(error_message, parse_mode='Markdown')
    except ConnectionRefusedError:
        error_message = f"❌ **Connection Refused:**\nCould not connect to `{alias}`. The server may be down or the port may be incorrect."
        logger.error(f"Connection refused for {alias}")
        await query.edit_message_text(error_message, parse_mode='Markdown')
    except (socket.gaierror, OSError) as e:
        error_message = f"❌ **Host Not Found:**\nCould not resolve hostname `{alias}`. Please check the server address.\n\n`{e}`"
        logger.error(f"Hostname could not be resolved for {alias}")
        await query.edit_message_text(error_message, parse_mode='Markdown')
    except Exception:
        # For any other exception, defer to the global error handler to send the full traceback
        logger.error(f"An unexpected error occurred while connecting to {alias}", exc_info=True)
        raise


# --- Debugging ---
async def send_debug_message(update: Update, text: str):
    """Sends a debug message to the user if debug mode is enabled."""
    if DEBUG_MODE:
        await update.effective_chat.send_message(f"🐞 **DEBUG:** {text}", parse_mode='Markdown')

@authorized
async def toggle_debug_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggles debug mode on or off."""
    global DEBUG_MODE
    DEBUG_MODE = not DEBUG_MODE
    status = "ON" if DEBUG_MODE else "OFF"
    await update.message.reply_text(f"🐞 **Debug Mode is now {status}**", parse_mode='Markdown')


# --- Command Execution ---
@authorized
async def run_command_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Asks the user for a command to run."""
    query = update.callback_query
    await query.answer()

    context.user_data['alias'] = query.data.split('_', 2)[2]

    await query.edit_message_text(
        f"Ok, please send the command you want to run on **{context.user_data['alias']}**.",
        parse_mode='Markdown'
    )
    return AWAIT_COMMAND

@authorized
async def execute_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Executes a command on the connected remote server with live streaming and safe handling."""
    user_id = update.effective_user.id
    alias = user_connections.get(user_id)
    command = update.message.text.strip()

    if not alias:
        await update.message.reply_text("⚠️ No active connection. Please connect to a server first.")
        return ConversationHandler.END

    # Initial message
    result_message = await update.message.reply_text(
        f"🛰️ Running `{command}` on `{alias}`...",
        parse_mode='Markdown'
    )

    output_buffer = []
    last_sent_text = ""
    edit_interval = 1.5  # seconds between UI refreshes
    last_edit_time = asyncio.get_event_loop().time()

    try:
        async for item, stream in ssh_manager.run_command(alias, command):
            # Skip PID events (AsyncSSH doesn’t expose it natively)
            if stream == 'pid':
                continue

            if stream in ('stdout', 'stderr'):
                output_buffer.append(item)

                # Periodically update Telegram message without flooding API
                now = asyncio.get_event_loop().time()
                if now - last_edit_time >= edit_interval:
                    partial_output = ''.join(output_buffer)[-3800:]  # keep last chunk for preview
                    if partial_output != last_sent_text:
                        try:
                            await result_message.edit_text(
                                f"```\n{partial_output}\n```",
                                parse_mode='Markdown'
                            )
                            last_sent_text = partial_output
                        except BadRequest as e:
                            if "Message is not modified" not in str(e):
                                logger.warning(f"Telegram update error: {e}")
                        last_edit_time = now

        # Combine all output once command completes
        final_output = ''.join(output_buffer).strip()
        if not final_output:
            final_output = "[No output returned]"

        final_text = f"✅ **Command completed on `{alias}`**\n\n```\n{final_output}\n```"
        if len(final_text) > 4096:
            with tempfile.NamedTemporaryFile(mode="w+", delete=False, suffix=".txt") as f:
                f.write(final_output)
                f.flush()
            await result_message.delete()
            await update.message.reply_document(
                document=open(f.name, "rb"),
                caption=f"Output for `{command}`"
            )
            os.remove(f.name)
        else:
            await result_message.edit_text(final_text, parse_mode='Markdown')

    except asyncio.TimeoutError:
        await result_message.edit_text(
            f"⏰ **Timeout:** Command `{command}` took too long and was terminated.",
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Error executing '{command}' on '{alias}': {e}", exc_info=True)
        await result_message.edit_text(
            f"❌ **Error while executing command:**\n`{e}`",
            parse_mode='Markdown'
        )

    return ConversationHandler.END


@authorized
async def cancel_command_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancels a running command."""
    query = update.callback_query
    await query.answer()
    _, alias, pid = query.data.split('_', 2)

    try:
        await ssh_manager.kill_process(alias, int(pid))
        await query.edit_message_text(f"✅ **Command (PID: {pid}) cancelled on `{alias}`.**", parse_mode='Markdown')
    except Exception as e:
        await query.edit_message_text(f"❌ **Error:** Could not cancel command.\n`{e}`", parse_mode='Markdown')


# --- Interactive Shell ---
@authorized
async def start_shell_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Starts an interactive shell session for the user."""
    query = update.callback_query
    await query.answer()
    alias = query.data.split('_', 2)[2]
    user_id = update.effective_user.id

    try:
        await ssh_manager.start_shell_session(alias)
        SHELL_MODE_USERS.add(user_id)
        user_connections[user_id] = alias  # Ensure connection is tracked
        await query.edit_message_text(
            f"🖥️ **Interactive shell started on `{alias}`.**\n\n"
            "Send any message to execute it as a command. Send `/exit_shell` to end the session.",
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Error starting shell on {alias} for user {user_id}: {e}", exc_info=True)
        await query.edit_message_text(f"❌ **Error:** Could not start shell.\n`{e}`", parse_mode='Markdown')

async def handle_shell_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles commands sent by a user in shell mode."""
    user_id = update.effective_user.id
    if user_id not in SHELL_MODE_USERS:
        # This message is not a shell command, process as normal (or ignore)
        return

    alias = user_connections.get(user_id)
    command = update.message.text

    if command.strip() == 'exit':
        await exit_shell(update, context)
        return

    try:
        output = await ssh_manager.run_command_in_shell(alias, command)
        if not output:
            output = "[No output]"

        if len(output) > 4000:
            with tempfile.NamedTemporaryFile(mode="w+", delete=False, suffix=".txt") as f:
                f.write(output)
                f.flush()
            await update.message.reply_document(document=open(f.name, "rb"), caption=f"Shell output for `{command}`")
            os.remove(f.name)
        else:
            await update.message.reply_text(f"```\n{output}\n```", parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Error in shell on {alias} for user {user_id}: {e}", exc_info=True)
        await update.message.reply_text(f"❌ **Error:**\n`{e}`", parse_mode='Markdown')

@authorized
async def exit_shell(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Exits the interactive shell mode."""
    user_id = update.effective_user.id
    if user_id in SHELL_MODE_USERS:
        SHELL_MODE_USERS.remove(user_id)
        alias = user_connections.get(user_id)
        if alias:
            await ssh_manager.disconnect(alias)
            del user_connections[user_id]

        await update.message.reply_text("🔌 **Shell session terminated.**", parse_mode='Markdown')
        await main_menu(update, context)


# --- Error Handling ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the error and send a detailed message to the user."""
    logger.error("Exception while handling an update:", exc_info=context.error)

    if isinstance(update, Update) and update.effective_chat:
        # Format the traceback
        tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
        tb_string = "".join(tb_list)

        # Sanitize and prepare the message
        error_message = f"❌ **An unexpected error occurred:**\n\n```\n{tb_string}\n```"

        # Ensure the message is not too long
        if len(error_message) > 4096:
            error_message = error_message[:4090] + "\n...```"

        try:
            await update.effective_chat.send_message(error_message, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Failed to send detailed error message to user: {e}", exc_info=True)
            # Fallback to a simpler message if the detailed one fails
            try:
                await update.effective_chat.send_message(
                    "❌ **An unexpected error occurred.**\nThe technical details have been logged.",
                    parse_mode='Markdown'
                )
            except Exception as fallback_e:
                logger.error(f"Failed to send even the fallback error message: {fallback_e}", exc_info=True)


@authorized
async def server_status_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays the server status menu."""
    query = update.callback_query
    await query.answer()
    alias = query.data.split('_', 3)[3]

    keyboard = [
        [InlineKeyboardButton("ℹ️ System Info", callback_data=f"static_info_{alias}")],
        [InlineKeyboardButton("📈 Resource Usage", callback_data=f"resource_usage_{alias}")],
        [InlineKeyboardButton("🔴 Live Monitoring", callback_data=f"live_monitoring_{alias}")],
        [InlineKeyboardButton("🔙 Back to Server Menu", callback_data=f"connect_{alias}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        f"**📊 Server Status for {alias}**\n\nSelect an option:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

@authorized
async def get_static_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Gets static system information from the server."""
    query = update.callback_query
    await query.answer()
    alias = query.data.split('_', 2)[2]

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
            async for item, stream in ssh_manager.run_command(alias, command):
                if stream in ('stdout', 'stderr'):
                    output += item
            info_message += f"**{key}:**\n```{output.strip()}```\n\n"
        except Exception as e:
            info_message += f"**{key}:**\n`Error fetching info: {str(e)}`\n\n"

    keyboard = [[InlineKeyboardButton("🔙 Back to Status Menu", callback_data=f"server_status_menu_{alias}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(info_message, reply_markup=reply_markup, parse_mode='Markdown')

@authorized
async def get_resource_usage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Gets a snapshot of the server's resource usage."""
    query = update.callback_query
    await query.answer()
    alias = query.data.split('_', 2)[2]

    commands = {
        "Memory Usage": "free -m",
        "CPU Usage": "top -bn1 | head -n 5"
    }

    usage_message = f"**📈 Resource Usage for {alias}**\n\n"

    for key, command in commands.items():
        output = ""
        try:
            output = ""
            async for item, stream in ssh_manager.run_command(alias, command):
                if stream in ('stdout', 'stderr'):
                    output += item
            usage_message += f"**{key}:**\n```{output.strip()}```\n\n"
        except Exception as e:
            usage_message += f"**{key}:**\n`Error fetching info: {str(e)}`\n\n"

    keyboard = [[InlineKeyboardButton("🔙 Back to Status Menu", callback_data=f"server_status_menu_{alias}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(usage_message, reply_markup=reply_markup, parse_mode='Markdown')


@authorized
async def live_monitoring(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Starts live monitoring of the server's resource usage."""
    query = update.callback_query
    await query.answer()
    alias = query.data.split('_', 2)[2]
    user_id = update.effective_user.id

    if user_id in MONITORING_TASKS:
        MONITORING_TASKS[user_id].cancel()

    keyboard = [[InlineKeyboardButton("⏹️ Stop Monitoring", callback_data=f"stop_live_monitoring_{alias}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    message = await query.edit_message_text(
        f"**🔴 Live Monitoring for {alias}**\n\nStarting...",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

    async def _update_stats():
        while True:
            command = "top -bn1 | head -n 5"
            output = ""
            try:
                output = ""
                async for item, stream in ssh_manager.run_command(alias, command):
                    if stream in ('stdout', 'stderr'):
                        output += item
                await message.edit_text(
                    f"**🔴 Live Monitoring for {alias}**\n\n```{output.strip()}```",
                    reply_markup=reply_markup,
                    parse_mode='Markdown'
                )
            except Exception as e:
                await message.edit_text(
                    f"**🔴 Live Monitoring for {alias}**\n\n`Error fetching info: {str(e)}`",
                    reply_markup=reply_markup,
                    parse_mode='Markdown'
                )
            await asyncio.sleep(5)

    MONITORING_TASKS[user_id] = asyncio.create_task(_update_stats())

@authorized
async def stop_live_monitoring(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stops the live monitoring task."""
    query = update.callback_query
    await query.answer()
    alias = query.data.split('_', 3)[3]
    user_id = update.effective_user.id

    if user_id in MONITORING_TASKS:
        MONITORING_TASKS[user_id].cancel()
        del MONITORING_TASKS[user_id]

    keyboard = [[InlineKeyboardButton("🔙 Back to Status Menu", callback_data=f"server_status_menu_{alias}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        f"**🔴 Live Monitoring for {alias}**\n\nMonitoring stopped.",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

@authorized
async def package_management_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays the package management menu."""
    query = update.callback_query
    await query.answer()
    alias = query.data.split('_', 3)[3]

    keyboard = [
        [InlineKeyboardButton("🔄 Update Package Lists", callback_data=f"pkg_update_{alias}")],
        [InlineKeyboardButton("⬆️ Upgrade All Packages", callback_data=f"pkg_upgrade_{alias}")],
        [InlineKeyboardButton("➕ Install a Package", callback_data=f"pkg_install_{alias}")],
        [InlineKeyboardButton("🔙 Back to Server Menu", callback_data=f"connect_{alias}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        f"**📦 Package Management for {alias}**\n\nSelect an action:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

@authorized
async def package_manager_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles package management actions like update and upgrade."""
    query = update.callback_query
    await query.answer()

    _, action, alias = query.data.split('_', 2)

    if action == "update":
        command = "sudo apt-get update"
    elif action == "upgrade":
        command = "sudo apt-get upgrade -y"
    else:
        return

    result_message = await query.edit_message_text(f"Running `{command}` on `{alias}`...", parse_mode='Markdown')

    output = ""
    try:
        output = ""
        async for item, stream in ssh_manager.run_command(alias, command):
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
            await result_message.edit_text(final_message, parse_mode='Markdown')

    except Exception as e:
        await result_message.edit_text(f"❌ **Error:**\n`{str(e)}`", parse_mode='Markdown')

@authorized
async def install_package_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the conversation to install a package."""
    query = update.callback_query
    await query.answer()

    alias = query.data.split('_', 2)[2]
    context.user_data['alias'] = alias

    await query.edit_message_text(f"Please enter the name of the package to install on `{alias}`.", parse_mode='Markdown')
    return AWAIT_PACKAGE_NAME

async def execute_install_package(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Executes the package installation."""
    package_name = shlex.quote(update.message.text)
    alias = context.user_data['alias']

    command = f"sudo apt-get install -y {package_name}"

    result_message = await update.message.reply_text(f"Running `{command}` on `{alias}`...", parse_mode='Markdown')

    output = ""
    try:
        output = ""
        async for item, stream in ssh_manager.run_command(alias, command):
            if stream in ('stdout', 'stderr'):
                output += item
        await result_message.edit_text(f"✅ **Command completed on `{alias}`**\n\n```{output.strip()}```", parse_mode='Markdown')
    except Exception as e:
        await result_message.edit_text(f"❌ **Error:**\n`{str(e)}`", parse_mode='Markdown')

    context.user_data.clear()
    return ConversationHandler.END

async def cancel_install_package(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the package installation conversation."""
    await update.message.reply_text("Package installation cancelled.")
    context.user_data.clear()
    return ConversationHandler.END


@authorized
async def docker_management_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays the Docker management menu."""
    query = update.callback_query
    await query.answer()
    alias = query.data.split('_', 3)[3]

    keyboard = [
        [InlineKeyboardButton("📜 List Containers", callback_data=f"docker_ps_{alias}")],
        [InlineKeyboardButton("📜 List All Containers", callback_data=f"docker_ps_a_{alias}")],
        [InlineKeyboardButton("📄 View Logs", callback_data=f"docker_logs_{alias}")],
        [InlineKeyboardButton("▶️ Start Container", callback_data=f"docker_start_{alias}")],
        [InlineKeyboardButton("⏹️ Stop Container", callback_data=f"docker_stop_{alias}")],
        [InlineKeyboardButton("🔙 Back to Server Menu", callback_data=f"connect_{alias}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        f"**🐳 Docker Management for {alias}**\n\nSelect an action:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )


@authorized
async def docker_action_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts a conversation for a Docker action that requires a container name."""
    query = update.callback_query
    await query.answer()

    action = query.data.split('_')[1]
    alias = query.data.split('_', 2)[2]

    context.user_data['docker_action'] = action
    context.user_data['alias'] = alias

    await query.edit_message_text(f"Please enter the name or ID of the container to `{action}`.", parse_mode='Markdown')
    return AWAIT_CONTAINER_NAME


async def execute_docker_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Executes a Docker action on a specific container."""
    container_name = shlex.quote(update.message.text)
    action = context.user_data['docker_action']
    alias = context.user_data['alias']

    command = f"docker {action} {container_name}"

    result_message = await update.message.reply_text(f"Running `{command}` on `{alias}`...", parse_mode='Markdown')

    output = ""
    try:
        output = ""
        async for item, stream in ssh_manager.run_command(alias, command):
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
            await result_message.edit_text(final_message, parse_mode='Markdown')
    except Exception as e:
        await result_message.edit_text(f"❌ **Error:**\n`{str(e)}`", parse_mode='Markdown')

    context.user_data.clear()
    return ConversationHandler.END


@authorized
async def docker_ps(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Lists Docker containers."""
    query = update.callback_query
    await query.answer()

    command = "docker ps"
    if query.data.startswith("docker_ps_a"):
        command += " -a"
    alias = query.data.split('_', 2)[-1]

    result_message = await query.edit_message_text(f"Running `{command}` on `{alias}`...", parse_mode='Markdown')

    output = ""
    try:
        output = ""
        async for item, stream in ssh_manager.run_command(alias, command):
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
            await result_message.edit_text(final_message, parse_mode='Markdown')
    except Exception as e:
        await result_message.edit_text(f"❌ **Error:**\n`{str(e)}`", parse_mode='Markdown')


async def cancel_docker_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the Docker action conversation."""
    await update.message.reply_text("Docker action cancelled.")
    context.user_data.clear()
    return ConversationHandler.END


@authorized
async def file_manager_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays the file manager menu."""
    query = update.callback_query
    await query.answer()
    alias = query.data.split('_', 3)[3]

    keyboard = [
        [InlineKeyboardButton("📜 List Files", callback_data=f"fm_ls_{alias}")],
        [InlineKeyboardButton("📥 Download File", callback_data=f"fm_download_{alias}")],
        [InlineKeyboardButton("📤 Upload File", callback_data=f"fm_upload_{alias}")],
        [InlineKeyboardButton("🔙 Back to Server Menu", callback_data=f"connect_{alias}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        f"**📁 File Manager for {alias}**\n\nSelect an action:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )


@authorized
async def file_manager_action_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts a conversation for a file manager action."""
    query = update.callback_query
    await query.answer()

    action = query.data.split('_')[1]
    alias = query.data.split('_', 2)[2]

    context.user_data['file_manager_action'] = action
    context.user_data['alias'] = alias

    if action == "ls":
        prompt = "Please enter the directory path to list."
    elif action == "download":
        prompt = "Please enter the full path of the file to download."
    elif action == "upload":
        await query.edit_message_text(f"Please enter the full destination path on `{alias}`.", parse_mode='Markdown')
        return AWAIT_FILE_PATH

    await query.edit_message_text(prompt, parse_mode='Markdown')
    return AWAIT_FILE_PATH


async def file_manager_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Dispatches to the correct file manager function based on the action."""
    action = context.user_data.get('file_manager_action')
    if action == "ls":
        return await list_files(update, context)
    elif action == "download":
        return await download_file(update, context)
    elif action == "upload":
        return await upload_file_path(update, context)
    else:
        await update.message.reply_text("Unknown file manager action.")
        return ConversationHandler.END

async def list_files(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Lists files in a directory."""
    path = shlex.quote(update.message.text)
    alias = context.user_data['alias']
    command = f"ls -la {path}"

    result_message = await update.message.reply_text(f"Running `{command}` on `{alias}`...", parse_mode='Markdown')

    output = ""
    try:
        output = ""
        async for item, stream in ssh_manager.run_command(alias, command):
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
            await result_message.edit_text(final_message, parse_mode='Markdown')
    except Exception as e:
        await result_message.edit_text(f"❌ **Error:**\n`{str(e)}`", parse_mode='Markdown')

    context.user_data.clear()
    return ConversationHandler.END


async def download_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Downloads a file from the server."""
    remote_path = shlex.quote(update.message.text)
    alias = context.user_data['alias']

    await update.message.reply_text(f"📥 Downloading `{remote_path}` from `{alias}`...", parse_mode='Markdown')

    try:
        with tempfile.NamedTemporaryFile(delete=False) as f:
            local_path = f.name
        await ssh_manager.download_file(alias, remote_path, local_path)
        await update.message.reply_document(document=open(local_path, 'rb'))
        os.remove(local_path)
    except Exception as e:
        await update.message.reply_text(f"❌ **Error:**\n`{e}`", parse_mode='Markdown')

    context.user_data.clear()
    return ConversationHandler.END


async def upload_file_path(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Gets the destination path for the file upload."""
    context.user_data['remote_path'] = update.message.text
    await update.message.reply_text("Okay, now please upload the file to be sent to the server.")
    return AWAIT_UPLOAD_FILE


async def upload_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Uploads a file to the server."""
    document = update.message.document
    alias = context.user_data['alias']
    remote_path = context.user_data['remote_path']

    local_path = document.file_name
    file = await document.get_file()
    await file.download_to_drive(local_path)

    await update.message.reply_text(f"📤 Uploading `{local_path}` to `{remote_path}` on `{alias}`...", parse_mode='Markdown')

    try:
        await ssh_manager.upload_file(alias, local_path, remote_path)
        await update.message.reply_text("✅ **File uploaded successfully!**", parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ **Error:**\n`{e}`", parse_mode='Markdown')
    finally:
        os.remove(local_path)

    context.user_data.clear()
    return ConversationHandler.END


async def cancel_file_manager_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the file manager action conversation."""
    await update.message.reply_text("File manager action cancelled.")
    context.user_data.clear()
    return ConversationHandler.END


@authorized
async def process_management_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays the process management menu."""
    query = update.callback_query
    await query.answer()
    alias = query.data.split('_', 3)[3]

    keyboard = [
        [InlineKeyboardButton("📜 List Processes", callback_data=f"ps_aux_{alias}")],
        [InlineKeyboardButton("❌ Kill Process", callback_data=f"kill_process_{alias}")],
        [InlineKeyboardButton("🔙 Back to Server Menu", callback_data=f"connect_{alias}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        f"**⚙️ Process Management for {alias}**\n\nSelect an action:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )


@authorized
async def list_processes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Lists running processes on the server."""
    query = update.callback_query
    await query.answer()
    alias = query.data.split('_', 2)[2]

    command = "ps aux"
    result_message = await query.edit_message_text(f"Running `{command}` on `{alias}`...", parse_mode='Markdown')

    output = ""
    try:
        output = ""
        async for item, stream in ssh_manager.run_command(alias, command):
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
            await result_message.edit_text(final_message, parse_mode='Markdown')
    except Exception as e:
        await result_message.edit_text(f"❌ **Error:**\n`{str(e)}`", parse_mode='Markdown')


@authorized
async def kill_process_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the conversation to kill a process."""
    query = update.callback_query
    await query.answer()

    alias = query.data.split('_', 2)[2]
    context.user_data['alias'] = alias

    await query.edit_message_text(f"Please enter the PID of the process to kill on `{alias}`.", parse_mode='Markdown')
    return AWAIT_PID


async def execute_kill_process(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Executes the kill command."""
    pid = shlex.quote(update.message.text)
    alias = context.user_data['alias']

    command = f"kill -9 {pid}"

    result_message = await update.message.reply_text(f"Running `{command}` on `{alias}`...", parse_mode='Markdown')

    output = ""
    try:
        output = ""
        async for item, stream in ssh_manager.run_command(alias, command):
            if stream in ('stdout', 'stderr'):
                output += item
        await result_message.edit_text(f"✅ **Command completed on `{alias}`**\n\n```{output.strip()}```", parse_mode='Markdown')
    except Exception as e:
        await result_message.edit_text(f"❌ **Error:**\n`{str(e)}`", parse_mode='Markdown')

    context.user_data.clear()
    return ConversationHandler.END


async def cancel_kill_process(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the kill process conversation."""
    await update.message.reply_text("Kill process cancelled.")
    context.user_data.clear()
    return ConversationHandler.END


@authorized
async def firewall_management_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays the firewall management menu."""
    query = update.callback_query
    await query.answer()
    alias = query.data.split('_', 3)[3]

    keyboard = [
        [InlineKeyboardButton("📜 View Rules", callback_data=f"fw_status_{alias}")],
        [InlineKeyboardButton("➕ Allow Port", callback_data=f"fw_allow_{alias}")],
        [InlineKeyboardButton("➖ Deny Port", callback_data=f"fw_deny_{alias}")],
        [InlineKeyboardButton("🗑️ Delete Rule", callback_data=f"fw_delete_{alias}")],
        [InlineKeyboardButton("🔙 Back to Server Menu", callback_data=f"connect_{alias}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        f"**🔥 Firewall Management for {alias}**\n\nSelect an action:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

@authorized
async def firewall_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Gets the firewall status from the server."""
    query = update.callback_query
    await query.answer()
    alias = query.data.split('_', 2)[2]

    command = "sudo ufw status verbose"
    output = ""
    try:
        output = ""
        async for item, stream in ssh_manager.run_command(alias, command):
            if stream in ('stdout', 'stderr'):
                output += item
        keyboard = [[InlineKeyboardButton("🔙 Back to Firewall Menu", callback_data=f"firewall_management_menu_{alias}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(f"**🔥 Firewall Status for `{alias}`**\n\n```{output.strip()}```", reply_markup=reply_markup, parse_mode='Markdown')
    except Exception as e:
        await query.edit_message_text(f"❌ **Error:**\n`{str(e)}`", parse_mode='Markdown')


# --- Firewall Management ---
@authorized
async def firewall_action_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the firewall management conversation."""
    query = update.callback_query
    await query.answer()

    action = query.data.split('_')[1]
    alias = query.data.split('_', 2)[2]

    context.user_data['firewall_action'] = action
    context.user_data['alias'] = alias

    if action == "delete":
        prompt = f"Please enter the rule number to `{action}`."
    else:
        prompt = f"Please enter the port number to `{action}`."

    await query.edit_message_text(prompt, parse_mode='Markdown')
    return AWAIT_FIREWALL_RULE

async def execute_firewall_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Executes the selected firewall action."""
    rule = shlex.quote(update.message.text)
    action = context.user_data['firewall_action']
    alias = context.user_data['alias']

    command = f"sudo ufw {action} {rule}"

    result_message = await update.message.reply_text(f"Running `{command}` on `{alias}`...", parse_mode='Markdown')

    output = ""
    try:
        output = ""
        async for item, stream in ssh_manager.run_command(alias, command):
            if stream in ('stdout', 'stderr'):
                output += item
        await result_message.edit_text(f"✅ **Command completed on `{alias}`**\n\n```{output.strip()}```", parse_mode='Markdown')
    except Exception as e:
        await result_message.edit_text(f"❌ **Error:**\n`{str(e)}`", parse_mode='Markdown')

    context.user_data.clear()
    return ConversationHandler.END

async def cancel_firewall_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the firewall management conversation."""
    await update.message.reply_text("Firewall action cancelled.")
    context.user_data.clear()
    return ConversationHandler.END


@authorized
async def service_management_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays the service management menu."""
    query = update.callback_query
    await query.answer()
    alias = query.data.split('_', 3)[3]

    keyboard = [
        [InlineKeyboardButton("🔍 Check Service Status", callback_data=f"check_service_{alias}")],
        [InlineKeyboardButton("▶️ Start a Service", callback_data=f"start_service_{alias}")],
        [InlineKeyboardButton("⏹️ Stop a Service", callback_data=f"stop_service_{alias}")],
        [InlineKeyboardButton("🔄 Restart a Service", callback_data=f"restart_service_{alias}")],
        [InlineKeyboardButton("🔙 Back to Server Menu", callback_data=f"connect_{alias}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        f"**🔧 Service Management for {alias}**\n\nSelect an action:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

# --- Service Management ---
@authorized
async def service_action_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the service management conversation."""
    query = update.callback_query
    await query.answer()

    action = query.data.split('_')[0]
    alias = query.data.split('_', 2)[2]

    context.user_data['service_action'] = action
    context.user_data['alias'] = alias

    await query.edit_message_text(f"Please enter the name of the service to `{action}`.", parse_mode='Markdown')
    return AWAIT_SERVICE_NAME

async def execute_service_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Executes the selected service action."""
    service_name = update.message.text
    action = context.user_data['service_action']
    alias = context.user_data['alias']

    command = f"systemctl {action} {service_name}"

    result_message = await update.message.reply_text(f"Running `{command}` on `{alias}`...", parse_mode='Markdown')

    output = ""
    try:
        output = ""
        async for item, stream in ssh_manager.run_command(alias, command):
            if stream in ('stdout', 'stderr'):
                output += item
        await result_message.edit_text(f"✅ **Command completed on `{alias}`**\n\n```{output.strip()}```", parse_mode='Markdown')
    except Exception as e:
        await result_message.edit_text(f"❌ **Error:**\n`{str(e)}`", parse_mode='Markdown')

    context.user_data.clear()
    return ConversationHandler.END

async def cancel_service_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the service management conversation."""
    await update.message.reply_text("Service action cancelled.")
    context.user_data.clear()
    return ConversationHandler.END

@authorized
async def system_commands_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays the system commands menu."""
    query = update.callback_query
    await query.answer()
    alias = query.data.split('_', 3)[3]

    keyboard = [
        [InlineKeyboardButton("💾 Disk Usage", callback_data=f"disk_usage_{alias}")],
        [InlineKeyboardButton("🌐 Network Info", callback_data=f"network_info_{alias}")],
        [InlineKeyboardButton("🔌 Open Ports", callback_data=f"open_ports_{alias}")],
        [InlineKeyboardButton("🔄 Reboot", callback_data=f"reboot_{alias}")],
        [InlineKeyboardButton(" Shutdown", callback_data=f"shutdown_{alias}")],
        [InlineKeyboardButton("🔙 Back to Server Menu", callback_data=f"connect_{alias}")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        f"**⚙️ System Commands for {alias}**\n\nSelect an action:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

@authorized
async def confirm_system_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Asks for confirmation before executing a system command."""
    query = update.callback_query
    await query.answer()

    action = query.data.split('_')[0]
    alias = query.data.split('_', 1)[1]

    keyboard = [
        [
            InlineKeyboardButton(f"✅ Yes, {action}", callback_data=f"execute_{action}_{alias}"),
            InlineKeyboardButton("❌ No", callback_data=f"system_commands_menu_{alias}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        f"**⚠️ Are you sure you want to {action} the server `{alias}`?**",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

@authorized
async def execute_system_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Executes a system command (reboot, shutdown) after confirmation."""
    query = update.callback_query
    await query.answer()

    _, action, alias = query.data.split('_', 2)

    if action == "reboot":
        command = "reboot"
    elif action == "shutdown":
        command = "shutdown now"
    else:
        return

    try:
        # We only need to start the command, not wait for output
        async for _, __ in ssh_manager.run_command(alias, f"sudo {command}"):
            pass
        await query.edit_message_text(f"✅ **Command `{command}` sent to `{alias}` successfully.**", parse_mode='Markdown')
    except Exception as e:
        await query.edit_message_text(f"❌ **Error:**\n`{e}`", parse_mode='Markdown')

@authorized
async def get_disk_usage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Gets disk usage information from the server."""
    query = update.callback_query
    await query.answer()
    alias = query.data.split('_', 2)[2]

    command = "df -h"
    output = ""
    try:
        output = ""
        async for item, stream in ssh_manager.run_command(alias, command):
            if stream in ('stdout', 'stderr'):
                output += item
        keyboard = [[InlineKeyboardButton("🔙 Back to System Commands", callback_data=f"system_commands_menu_{alias}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(f"**💾 Disk Usage for `{alias}`**\n\n```{output.strip()}```", reply_markup=reply_markup, parse_mode='Markdown')
    except Exception as e:
        await query.edit_message_text(f"❌ **Error:**\n`{str(e)}`", parse_mode='Markdown')

@authorized
async def get_network_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Gets network information from the server."""
    query = update.callback_query
    await query.answer()
    alias = query.data.split('_', 2)[2]

    command = "ip a"
    output = ""
    try:
        output = ""
        async for item, stream in ssh_manager.run_command(alias, command):
            if stream in ('stdout', 'stderr'):
                output += item
        keyboard = [[InlineKeyboardButton("🔙 Back to System Commands", callback_data=f"system_commands_menu_{alias}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(f"**🌐 Network Info for `{alias}`**\n\n```{output.strip()}```", reply_markup=reply_markup, parse_mode='Markdown')
    except Exception as e:
        await query.edit_message_text(f"❌ **Error:**\n`{str(e)}`", parse_mode='Markdown')

@authorized
async def get_open_ports(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Gets open ports from the server."""
    query = update.callback_query
    await query.answer()
    alias = query.data.split('_', 2)[2]

    command = "ss -tuln"
    output = ""
    try:
        output = ""
        async for item, stream in ssh_manager.run_command(alias, command):
            if stream in ('stdout', 'stderr'):
                output += item
        keyboard = [[InlineKeyboardButton("🔙 Back to System Commands", callback_data=f"system_commands_menu_{alias}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(f"**🔌 Open Ports for `{alias}`**\n\n```{output.strip()}```", reply_markup=reply_markup, parse_mode='Markdown')
    except Exception as e:
        await query.edit_message_text(f"❌ **Error:**\n`{str(e)}`", parse_mode='Markdown')


async def disconnect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Disconnects the user from the server."""
    query = update.callback_query
    await query.answer()
    alias = query.data.split('_', 1)[1]
    user_id = update.effective_user.id

    try:
        await ssh_manager.disconnect(alias)
        if user_id in user_connections:
            del user_connections[user_id]

        logger.info(f"User {user_id} disconnected from {alias}.")
        await query.edit_message_text("🔌 **Disconnected successfully.**", parse_mode='Markdown')
        await main_menu(update, context)

    except Exception as e:
        logger.error(f"Error disconnecting from {alias} for user {user_id}: {e}", exc_info=True)
        await query.edit_message_text(f"❌ **Error:** Could not disconnect.\n`{e}`", parse_mode='Markdown')


@admin_authorized
async def backup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Creates a backup of the config and database files."""
    query = update.callback_query
    await query.answer()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_filename = f"tla_backup_{timestamp}.zip"

    files_to_backup = ["config.json", "database.db"]

    try:
        with zipfile.ZipFile(backup_filename, 'w') as zipf:
            for file in files_to_backup:
                if os.path.exists(file):
                    zipf.write(file)
                else:
                    logger.warning(f"File {file} not found for backup.")

        with open(backup_filename, 'rb') as backup_file:
            await context.bot.send_document(chat_id=update.effective_chat.id, document=backup_file)

        keyboard = [[InlineKeyboardButton("🔙 Back to Main Menu", callback_data='main_menu')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.reply_text("Backup complete.", reply_markup=reply_markup)

    except Exception as e:
        logger.error(f"Error creating backup: {e}", exc_info=True)
        await query.message.reply_text(f"❌ **Error:** Could not create backup.\n`{e}`", parse_mode='Markdown')
    finally:
        if os.path.exists(backup_filename):
            os.remove(backup_filename)

# --- Restore ---
@admin_authorized
async def restore_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the restore process."""
    query = update.callback_query
    await query.answer()

    keyboard = [[InlineKeyboardButton("⚠️ Yes, I'm sure", callback_data='restore_yes')]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text(
        "**⚠️ DANGER ZONE: RESTORE**\n\n"
        "Restoring from a backup will overwrite your current configuration and database. "
        "This action is irreversible. Are you sure you want to continue?",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    return AWAIT_RESTORE_CONFIRMATION

async def restore_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Confirms the restore and asks for the backup file."""
    query = update.callback_query
    await query.answer()

    if query.data == 'restore_yes':
        await query.edit_message_text("Okay, please upload the backup file (`.zip`).")
        return AWAIT_RESTORE_FILE
    else:
        await query.edit_message_text("Restore cancelled.")
        return ConversationHandler.END

async def restore_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receives the backup file, validates it, and performs the restore."""
    document = update.message.document
    if not document.file_name.endswith('.zip'):
        await update.message.reply_text("Invalid file format. Please upload a `.zip` file.")
        return AWAIT_RESTORE_FILE

    backup_file = await document.get_file()
    await backup_file.download_to_drive(document.file_name)

    try:
        with zipfile.ZipFile(document.file_name, 'r') as zipf:
            if "config.json" not in zipf.namelist() or "database.db" not in zipf.namelist():
                raise ValueError("Backup file is missing required files.")
            zipf.extractall()

        await update.message.reply_text("✅ **Restore successful!**\n\nThe bot will now restart to apply the changes.", parse_mode='Markdown')

        # This is a simplified restart. A more robust solution would use a process manager.
        os.execv(sys.executable, ['python'] + sys.argv)

    except Exception as e:
        await update.message.reply_text(f"❌ **Error:**\n`{e}`", parse_mode='Markdown')
    finally:
        if os.path.exists(document.file_name):
            os.remove(document.file_name)

    return ConversationHandler.END

async def cancel_restore(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the restore conversation."""
    await update.message.reply_text("Restore cancelled.")
    return ConversationHandler.END

async def remove_server_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Removes a server after confirmation."""
    query = update.callback_query
    await query.answer()

    alias = query.data.split('_', 1)[1]
    remove_server(alias)
    ssh_manager.refresh_server_configs()
    await query.edit_message_text(f"✅ **Server '{alias}' removed successfully!**", parse_mode='Markdown')

# --- Update ---
@admin_authorized
async def update_bot_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles the bot update process by launching the updater script in a detached process.
    This ensures the update can proceed even after the main bot service is stopped.
    """
    if update.callback_query:
        await update.callback_query.answer()
        base_message = update.callback_query.message
    else:
        base_message = update.effective_message

    try:
        # Get the path to the current python interpreter and the updater script
        python_executable = sys.executable
        updater_script = os.path.join(os.path.dirname(__file__), 'updater.py')

        # Use Popen to launch the updater in a new, detached process.
        # This allows the updater to outlive the main bot process.
        logger.info("Launching updater script in a detached process.")
        if os.name == 'posix':
            subprocess.Popen([python_executable, updater_script, "--auto"], start_new_session=True)
        else: # For Windows, Popen is detached by default
            subprocess.Popen([python_executable, updater_script, "--auto"])

        await base_message.reply_text(
            "✅ **Update process started successfully!**\n\n"
            "The bot will restart shortly to apply the new version. "
            "You can check `updater.log` for progress details.",
            parse_mode='Markdown'
        )

    except Exception as exc:
        logger.error("Failed to launch the update process: %s", exc, exc_info=True)
        await base_message.reply_text(
            f"❌ **Failed to start the update process.**\n\n"
            f"An error occurred: `{exc}`\n"
            "Please check the bot's main log for more details.",
            parse_mode='Markdown'
        )


def create_lock_file():
    """Creates a lock file to prevent multiple instances."""
    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))

def remove_lock_file():
    """Removes the lock file."""
    if os.path.exists(LOCK_FILE):
        os.remove(LOCK_FILE)

async def post_shutdown(application: Application) -> None:
    """Gracefully shuts down the SSH manager and database connections."""
    logger.info("Bot is shutting down...")
    remove_lock_file()
    if ssh_manager:
        await ssh_manager.close_all_connections()
    close_db_connection()


async def post_init(application: Application):
    """Actions to run after the bot has been initialized."""
    await application.bot.set_my_commands([
        BotCommand("start", "Display the main menu"),
        BotCommand("add_server", "Start the guided process to add a new server"),
        BotCommand("check_updates", "Check for new bot updates"),
        BotCommand("update_bot", "Update the bot to the latest version"),
        BotCommand("debug", "Toggle verbose debug messages"),
        BotCommand("exit_shell", "Terminate an active interactive shell session"),
        BotCommand("cancel", "Cancel the current multi-step operation (like adding a server)")
    ])

def main() -> None:
    """
    Initializes and runs the Telegram bot application.
    This is the main entry point of the bot.
    """
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, "r") as f:
                pid = int(f.read().strip())
            # Check if the process is running
            os.kill(pid, 0)
            logger.error(f"Lock file exists and process {pid} is running. Another instance of the bot is likely running.")
            sys.exit(1)
        except (IOError, ValueError, OSError):
            # Lock file is stale, remove it
            remove_lock_file()

    create_lock_file()

    global ssh_manager

    # --- Pre-flight Checks ---
    if not config.telegram_token:
        logger.error("FATAL: Telegram token is not configured. Please run the setup wizard.")
        sys.exit(1)

    # --- Core Component Initialization ---
    try:
        initialize_database()
        ssh_manager = SSHManager()
    except Exception as e:
        logger.critical(f"FATAL: Error during database or SSH manager initialization: {e}", exc_info=True)
        sys.exit(1)

    # --- Application Setup ---
    application = Application.builder().token(config.telegram_token).post_init(post_init).post_shutdown(post_shutdown).build()

    # --- Error Handling ---
    application.add_error_handler(error_handler)

    # --- Conversation Handlers ---
    add_server_handler = ConversationHandler(
        entry_points=[
            CommandHandler('add_server', add_server_start),
            CallbackQueryHandler(add_server_start, pattern='^add_server_start$')
        ],
        states={
            ALIAS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_alias)],
            HOSTNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_hostname)],
            USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_user)],
            AUTH_METHOD: [CallbackQueryHandler(get_auth_method)],
            PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_password)],
            KEY_PATH: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_key_path)],
        },
        fallbacks=[CommandHandler('cancel', cancel_add_server)],
    )

    run_command_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(run_command_start, pattern='^run_command_')],
        states={
            AWAIT_COMMAND: [MessageHandler(filters.TEXT & ~filters.COMMAND, execute_command)],
        },
        fallbacks=[],
    )
    application.add_handler(add_server_handler)
    application.add_handler(run_command_handler)

    restore_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(restore_start, pattern='^restore$')],
        states={
            AWAIT_RESTORE_CONFIRMATION: [CallbackQueryHandler(restore_confirmation)],
            AWAIT_RESTORE_FILE: [MessageHandler(filters.ATTACHMENT, restore_file)],
        },
        fallbacks=[CommandHandler('cancel', cancel_restore)],
    )
    application.add_handler(restore_handler)

    service_management_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(service_action_start, pattern='^check_service_'),
            CallbackQueryHandler(service_action_start, pattern='^start_service_'),
            CallbackQueryHandler(service_action_start, pattern='^stop_service_'),
            CallbackQueryHandler(service_action_start, pattern='^restart_service_'),
        ],
        states={
            AWAIT_SERVICE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, execute_service_action)],
        },
        fallbacks=[CommandHandler('cancel', cancel_service_action)],
    )
    application.add_handler(service_management_handler)

    install_package_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(install_package_start, pattern='^pkg_install_')],
        states={
            AWAIT_PACKAGE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, execute_install_package)],
        },
        fallbacks=[CommandHandler('cancel', cancel_install_package)],
    )
    application.add_handler(install_package_handler)

    docker_action_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(docker_action_start, pattern='^docker_logs_'),
            CallbackQueryHandler(docker_action_start, pattern='^docker_start_'),
            CallbackQueryHandler(docker_action_start, pattern='^docker_stop_'),
        ],
        states={
            AWAIT_CONTAINER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, execute_docker_action)],
        },
        fallbacks=[CommandHandler('cancel', cancel_docker_action)],
    )
    application.add_handler(docker_action_handler)

    file_manager_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(file_manager_action_start, pattern='^fm_ls_'),
            CallbackQueryHandler(file_manager_action_start, pattern='^fm_download_'),
            CallbackQueryHandler(file_manager_action_start, pattern='^fm_upload_'),
        ],
        states={
            AWAIT_FILE_PATH: [MessageHandler(filters.TEXT & ~filters.COMMAND, file_manager_dispatch)],
            AWAIT_UPLOAD_FILE: [MessageHandler(filters.ATTACHMENT, upload_file)],
        },
        fallbacks=[CommandHandler('cancel', cancel_file_manager_action)],
    )
    application.add_handler(file_manager_handler)

    kill_process_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(kill_process_start, pattern='^kill_process_')],
        states={
            AWAIT_PID: [MessageHandler(filters.TEXT & ~filters.COMMAND, execute_kill_process)],
        },
        fallbacks=[CommandHandler('cancel', cancel_kill_process)],
    )
    application.add_handler(kill_process_handler)

    firewall_management_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(firewall_action_start, pattern='^fw_allow_'),
            CallbackQueryHandler(firewall_action_start, pattern='^fw_deny_'),
            CallbackQueryHandler(firewall_action_start, pattern='^fw_delete_'),
        ],
        states={
            AWAIT_FIREWALL_RULE: [MessageHandler(filters.TEXT & ~filters.COMMAND, execute_firewall_action)],
        },
        fallbacks=[CommandHandler('cancel', cancel_firewall_action)],
    )
    application.add_handler(firewall_management_handler)

    # --- UI & Menu Handlers ---
    application.add_handler(CallbackQueryHandler(main_menu, pattern='^main_menu$'))
    application.add_handler(CallbackQueryHandler(connect_server_menu, pattern='^connect_server_menu$'))
    application.add_handler(CallbackQueryHandler(remove_server_menu, pattern='^remove_server_menu$'))
    application.add_handler(CallbackQueryHandler(update_bot_command, pattern='^update_bot$'))
    application.add_handler(CallbackQueryHandler(handle_server_connection, pattern='^connect_'))
    application.add_handler(CallbackQueryHandler(start_shell_session, pattern='^start_shell_'))
    application.add_handler(CallbackQueryHandler(server_status_menu, pattern='^server_status_menu_'))
    application.add_handler(CallbackQueryHandler(get_static_info, pattern='^static_info_'))
    application.add_handler(CallbackQueryHandler(get_resource_usage, pattern='^resource_usage_'))
    application.add_handler(CallbackQueryHandler(live_monitoring, pattern='^live_monitoring_'))
    application.add_handler(CallbackQueryHandler(stop_live_monitoring, pattern='^stop_live_monitoring_'))
    application.add_handler(CallbackQueryHandler(backup, pattern='^backup$'))
    application.add_handler(CallbackQueryHandler(service_management_menu, pattern='^service_management_menu_'))
    application.add_handler(CallbackQueryHandler(package_management_menu, pattern='^package_management_menu_'))
    application.add_handler(CallbackQueryHandler(package_manager_action, pattern='^pkg_update_'))
    application.add_handler(CallbackQueryHandler(package_manager_action, pattern='^pkg_upgrade_'))
    application.add_handler(CallbackQueryHandler(docker_management_menu, pattern='^docker_management_menu_'))
    application.add_handler(CallbackQueryHandler(docker_ps, pattern='^docker_ps_'))
    application.add_handler(CallbackQueryHandler(file_manager_menu, pattern='^file_manager_menu_'))
    application.add_handler(CallbackQueryHandler(process_management_menu, pattern='^process_management_menu_'))
    application.add_handler(CallbackQueryHandler(list_processes, pattern='^ps_aux_'))
    application.add_handler(CallbackQueryHandler(firewall_management_menu, pattern='^firewall_management_menu_'))
    application.add_handler(CallbackQueryHandler(firewall_status, pattern='^fw_status_'))
    application.add_handler(CallbackQueryHandler(cancel_command_callback, pattern='^cancel_command_'))
    application.add_handler(CallbackQueryHandler(system_commands_menu, pattern='^system_commands_menu_'))
    application.add_handler(CallbackQueryHandler(confirm_system_command, pattern='^reboot_'))
    application.add_handler(CallbackQueryHandler(confirm_system_command, pattern='^shutdown_'))
    application.add_handler(CallbackQueryHandler(execute_system_command, pattern='^execute_'))
    application.add_handler(CallbackQueryHandler(get_disk_usage, pattern='^disk_usage_'))
    application.add_handler(CallbackQueryHandler(get_network_info, pattern='^network_info_'))
    application.add_handler(CallbackQueryHandler(get_open_ports, pattern='^open_ports_'))
    application.add_handler(CallbackQueryHandler(disconnect, pattern='^disconnect_'))
    application.add_handler(CallbackQueryHandler(remove_server_confirm, pattern='^remove_'))

    # --- Command Handlers ---
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('debug', toggle_debug_mode))
    application.add_handler(CommandHandler('exit_shell', exit_shell))
    application.add_handler(CommandHandler('update_bot', update_bot_command))

    # --- Start Bot ---
    logger.info("Bot is starting...")

    # Special mode for CI/CD smoke test
    if os.environ.get("SMOKE_TEST"):
        logger.info("Smoke test mode enabled. Bot will not connect to Telegram.")
        loop = asyncio.get_event_loop()
        loop.run_until_complete(asyncio.sleep(15))
    else:
        try:
            application.run_polling()
        except InvalidToken:
            logger.critical(
                "FATAL: The Telegram token is invalid. "
                "Please run the setup wizard again to configure your bot with a valid token."
            )
            sys.exit(1)

if __name__ == "__main__":
    main()
