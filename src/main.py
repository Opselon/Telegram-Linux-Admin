import logging
import json
import asyncio
import os
import sys
import zipfile
from datetime import datetime
from telegram import BotCommand, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, ContextTypes,
    CallbackQueryHandler, ConversationHandler
)
from telegram.error import BadRequest
import asyncssh
from .ssh_manager import SSHManager
from .updater import check_for_updates, apply_update
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
    AWAIT_RESTORE_CONFIRMATION, AWAIT_RESTORE_FILE
) = range(9)

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

def _resolve_message(update: Update):
    if update.message:
        return update.message
    if update.callback_query:
        return update.callback_query.message
    return None

# --- Add/Remove Server ---
@authorized
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

@authorized
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
    """Displays the main menu with options."""
    logger.info("Displaying main menu.")
    keyboard = [
        [InlineKeyboardButton("🔌 Connect to a Server", callback_data='connect_server_menu')],
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
        async for _ in ssh_manager.run_command(alias, "echo 'Connection successful'"):
            pass

        # Store the active connection alias for the user
        user_connections[user_id] = alias

        logger.info(f"User {user_id} connected to server '{alias}'.")

        keyboard = [
            [InlineKeyboardButton("▶️ Run a Command", callback_data=f"run_command_{alias}")],
            [InlineKeyboardButton("🖥️ Open Interactive Shell", callback_data=f"start_shell_{alias}")],
            [InlineKeyboardButton("📊 Server Status", callback_data=f"server_status_menu_{alias}")],
            [InlineKeyboardButton("🔌 Disconnect", callback_data=f"disconnect_{alias}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            f"✅ **Connected to {alias}!**\n\nWhat would you like to do?",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

    except asyncssh.PermissionDenied:
        error_message = "❌ **Authentication Failed:**\nPermission denied. Please check your username, password, or SSH key."
        logger.error(f"Authentication failed for {alias}")
        await query.edit_message_text(error_message, parse_mode='Markdown')
    except asyncssh.ConnectionRefusedError:
        error_message = f"❌ **Connection Refused:**\nCould not connect to `{alias}`. Please ensure the server is running and the port is correct."
        logger.error(f"Connection refused for {alias}")
        await query.edit_message_text(error_message, parse_mode='Markdown')
    except (asyncssh.SocketError, asyncssh.misc.gaierror):
        error_message = f"❌ **Host Not Found:**\nCould not resolve hostname `{alias}`. Please check the server address."
        logger.error(f"Hostname could not be resolved for {alias}")
        await query.edit_message_text(error_message, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"An unexpected error occurred while connecting to {alias}: {e}", exc_info=True)
        await query.edit_message_text(f"❌ **An unexpected error occurred:**\n`{e}`", parse_mode='Markdown')


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

async def execute_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Executes the command on the remote server."""
    user_id = update.effective_user.id
    alias = user_connections.get(user_id)
    command = update.message.text

    if not alias:
        await update.message.reply_text("No active connection. Please connect to a server first.")
        return ConversationHandler.END

    result_message = await update.message.reply_text(f"Running `{command}` on `{alias}`...", parse_mode='Markdown')

    output = ""
    last_output = ""
    try:
        async_generator = ssh_manager.run_command(alias, command)
        async for line, stream in async_generator:
            output += line
            # To avoid hitting Telegram's rate limits and API errors, edit the message only periodically and if it has changed
            if len(output) % 100 == 0 and output != last_output:
                try:
                    await result_message.edit_text(f"```\n{output}\n```", parse_mode='Markdown')
                    last_output = output
                except BadRequest as e:
                    if "Message is not modified" not in str(e):
                        logger.warning(f"Error editing message: {e}")

        final_message = f"✅ **Command completed on `{alias}`**\n\n```\n{output}\n```"
        if len(final_message) > 4096:
            # If the message is too long, send it as a file
            with open("output.txt", "w") as f:
                f.write(output)
            await result_message.delete()
            await update.message.reply_document(document=open("output.txt", "rb"), caption=f"Command output for `{command}`")
            os.remove("output.txt")
        else:
            await result_message.edit_text(final_message, parse_mode='Markdown')

    except Exception as e:
        logger.error(f"Error executing command '{command}' on '{alias}': {e}", exc_info=True)
        await result_message.edit_text(f"❌ **Error:**\n`{e}`", parse_mode='Markdown')

    return ConversationHandler.END

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the command execution."""
    await update.message.reply_text("Command cancelled.")
    return ConversationHandler.END


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
            with open("shell_output.txt", "w") as f:
                f.write(output)
            await update.message.reply_document(document=open("shell_output.txt", "rb"), caption=f"Shell output for `{command}`")
            os.remove("shell_output.txt")
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
    """Log the error and send a telegram message to notify the user."""
    logger.error("Exception while handling an update:", exc_info=context.error)

    # Try to send a user-friendly message
    if isinstance(update, Update) and update.effective_chat:
        try:
            await update.effective_chat.send_message(
                "❌ **An unexpected error occurred.**\n"
                "The technical details have been logged. If the problem persists, please check the bot's logs.",
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Failed to send error message to user: {e}", exc_info=True)


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
            async_generator = ssh_manager.run_command(alias, command)
            async for line, stream in async_generator:
                output += line
            info_message += f"**{key}:**\n`{output.strip()}`\n\n"
        except Exception as e:
            info_message += f"**{key}:**\n`Error fetching info: {e}`\n\n"

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
            async_generator = ssh_manager.run_command(alias, command)
            async for line, stream in async_generator:
                output += line
            usage_message += f"**{key}:**\n`{output.strip()}`\n\n"
        except Exception as e:
            usage_message += f"**{key}:**\n`Error fetching info: {e}`\n\n"

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
                async_generator = ssh_manager.run_command(alias, command)
                async for line, stream in async_generator:
                    output += line

                await message.edit_text(
                    f"**🔴 Live Monitoring for {alias}**\n\n`{output.strip()}`",
                    reply_markup=reply_markup,
                    parse_mode='Markdown'
                )
            except Exception as e:
                await message.edit_text(
                    f"**🔴 Live Monitoring for {alias}**\n\n`Error fetching info: {e}`",
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


@authorized
@authorized
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
            await context.bot.send_document(chat_id=query.effective_chat.id, document=backup_file)

    except Exception as e:
        logger.error(f"Error creating backup: {e}", exc_info=True)
        await query.message.reply_text(f"❌ **Error:** Could not create backup.\n`{e}`", parse_mode='Markdown')
    finally:
        if os.path.exists(backup_filename):
            os.remove(backup_filename)

    keyboard = [[InlineKeyboardButton("🔙 Back to Main Menu", callback_data='main_menu')]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text("Backup complete.", reply_markup=reply_markup)

# --- Restore ---
@authorized
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
@authorized
async def check_for_updates_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Checks for updates and notifies the user."""
    await update.effective_message.reply_text("🔎 Checking for updates...")
    result = check_for_updates()
    await update.effective_message.reply_text(result["message"], disable_web_page_preview=True)

@authorized
async def update_bot_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the bot update process with real-time feedback."""
    if update.callback_query:
        await update.callback_query.answer()
        base_message = update.callback_query.message
    else:
        base_message = update.effective_message

    message = await base_message.reply_text(
        "⏳ **Update initiated...**\n\nThis may take a few minutes. "
        "The log will appear here once the process is complete.",
        parse_mode='Markdown'
    )

    try:
        update_log = apply_update()
        await message.edit_text(update_log, parse_mode='Markdown')
    except Exception as exc:
        logger.error("An error occurred in the update process: %s", exc, exc_info=True)
        await message.edit_text(
            "An unexpected error occurred.\nCheck the logs for more details.\n\n`{}`".format(exc),
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
        fallbacks=[CommandHandler('cancel', cancel_command)],
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
    application.add_handler(CallbackQueryHandler(disconnect, pattern='^disconnect_'))
    application.add_handler(CallbackQueryHandler(remove_server_confirm, pattern='^remove_'))

    # --- Command Handlers ---
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('debug', toggle_debug_mode))
    application.add_handler(CommandHandler('exit_shell', exit_shell))
    application.add_handler(CommandHandler('check_updates', check_for_updates_command))
    application.add_handler(CommandHandler('update_bot', update_bot_command))

    # --- Start Bot ---
    logger.info("Bot is starting...")

    # Special mode for CI/CD smoke test
    if os.environ.get("SMOKE_TEST"):
        logger.info("Smoke test mode enabled. Bot will not connect to Telegram.")
        loop = asyncio.get_event_loop()
        loop.run_until_complete(asyncio.sleep(15))
    else:
        application.run_polling()

if __name__ == "__main__":
    main()
