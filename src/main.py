import logging
import json
import asyncio
import os
import sys
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
# Standard logging setup to provide visibility into the bot's operations.
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ssh_manager: A global instance of the SSHManager to handle all SSH connections.
ssh_manager = None
# user_connections: A dictionary to track which server a user is currently connected to.
# Format: {user_id: "server_alias"}
user_connections = {}
# RESTORING: A flag to prevent the bot from handling commands while a database restore is in progress.
RESTORING = False
# SHELL_MODE_USERS: A set to track users who are currently in an interactive shell session.
SHELL_MODE_USERS = set()
# DEBUG_MODE: A flag to enable or disable verbose debugging messages in the chat.
DEBUG_MODE = False

# --- Conversation States ---
# These constants define the different steps (states) in a conversation, used by ConversationHandlers.
# This makes the code more readable than using raw integers.
(AWAIT_COMMAND, ALIAS, HOSTNAME, USER, AUTH_METHOD, PASSWORD, KEY_PATH) = range(7)

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
                await update.effective_chat.send_message(
                    "🚫 **Access Denied**\nWe could not verify your identity for this request.",
                    parse_mode='Markdown'
                )
            return

        await send_debug_message(update, f"Checking authorization for user_id: {user_id}...")

        if user_id not in config.whitelisted_users:
            logger.warning(f"Unauthorized access denied for user_id: {user_id}")
            await send_debug_message(update, f"Unauthorized access denied for user_id: {user_id}.")
            if update.callback_query:
                await update.callback_query.answer("🚫 You are not authorized to use this bot.", show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text(
                    "🚫 **Access Denied**\nYou are not authorized to use this bot.",
                    parse_mode='Markdown'
                )
            return

        await send_debug_message(update, "Authorization successful.")
        return await func(update, context, *args, **kwargs)

    return wrapped


def _resolve_message(update: Update):
    """Return the message associated with an update, falling back to callback messages."""
    if update.message:
        return update.message
    if update.callback_query:
        return update.callback_query.message
    return None

# --- Add/Remove Server ---
@authorized
async def add_server_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the conversation to add a new server."""
<<<<<<< ours
<<<<<<< ours
    if (query := update.callback_query) is not None:
        await query.answer()
    message = _resolve_message(update)
    if message is None:
        logger.warning("Add server requested but no message context is available.")
        return ConversationHandler.END

    await message.reply_text(
        "🖥️ **Add a New Server**\n\nLet's add a new server. "
        "First, what is the short alias for this server? (e.g., 'webserver')"
    )
=======
=======
>>>>>>> theirs
    prompt = (
        "🖥️ **Add a New Server**\n\n"
        "Let's add a new server. First, what is the short alias for this server? (e.g., `webserver`)"
    )

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(prompt, parse_mode='Markdown')
    else:
        await update.effective_message.reply_text(prompt, parse_mode='Markdown')

<<<<<<< ours
>>>>>>> theirs
=======
>>>>>>> theirs
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
    if (query := update.callback_query) is not None:
        await query.answer()
    message = _resolve_message(update)
    if message is None:
        logger.warning("Remove server menu requested but no message context is available.")
        return

    servers = get_all_servers()
    if not servers:
<<<<<<< ours
<<<<<<< ours
        await message.reply_text("No servers to remove.")
=======
=======
>>>>>>> theirs
        message = "🤷 **No servers to remove.**"
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(message, parse_mode='Markdown')
        else:
            await update.effective_message.reply_text(message, parse_mode='Markdown')
<<<<<<< ours
>>>>>>> theirs
=======
>>>>>>> theirs
        return

    keyboard = [
        [InlineKeyboardButton(f"Remove {server['alias']}", callback_data=f"remove_{server['alias']}")]
        for server in servers
    ]
    keyboard.append([InlineKeyboardButton("Cancel", callback_data="main_menu")])

    reply_markup = InlineKeyboardMarkup(keyboard)
<<<<<<< ours
<<<<<<< ours
    await message.reply_text("Select a server to remove:", reply_markup=reply_markup)
=======
=======
>>>>>>> theirs
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
<<<<<<< ours
>>>>>>> theirs
=======
>>>>>>> theirs

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

        # Establish the SSH connection
        await ssh_manager.get_connection(alias)

        # Store the active connection alias for the user
        user_connections[user_id] = alias

        logger.info(f"User {user_id} connected to server '{alias}'.")

        keyboard = [
            [InlineKeyboardButton("▶️ Run a Command", callback_data=f"run_command_{alias}")],
            [InlineKeyboardButton("🖥️ Open Interactive Shell", callback_data=f"start_shell_{alias}")],
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


@authorized
<<<<<<< ours
<<<<<<< ours
async def button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Parses the CallbackQuery and updates the message text."""
    query = update.callback_query
    await query.answer()

    logger.info(f"Button pressed with data: {query.data}")

    if query.data == 'main_menu':
        await main_menu(update, context)
    elif query.data == 'connect_server_menu':
        await connect_server_menu(update, context)
    elif query.data == 'add_server_start':
        await add_server_start(update, context)
    elif query.data == 'remove_server_menu':
        await remove_server_menu(update, context)
    elif query.data == 'update_bot':
        await update_bot_command(update, context)


=======
>>>>>>> theirs
=======
>>>>>>> theirs
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
        async for line, stream in ssh_manager.run_command(alias, command):
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
<<<<<<< ours
<<<<<<< ours
    message = _resolve_message(update)
    if message is None:
        logger.warning("Check for updates command triggered without message context.")
        return

    await message.reply_text("Checking for updates...")
    result = check_for_updates()
    await message.reply_text(result["message"])
=======
    await update.effective_message.reply_text("🔎 Checking for updates...")
    result = check_for_updates()
    await update.effective_message.reply_text(result["message"], disable_web_page_preview=True)
>>>>>>> theirs
=======
    await update.effective_message.reply_text("🔎 Checking for updates...")
    result = check_for_updates()
    await update.effective_message.reply_text(result["message"], disable_web_page_preview=True)
>>>>>>> theirs

@authorized
async def update_bot_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the bot update process with real-time feedback."""
<<<<<<< ours
<<<<<<< ours
    message = _resolve_message(update)
    if message is None:
        logger.warning("Update command triggered without message context.")
        return

    progress_message = await message.reply_text(
        "Update initiated...\n\nThis may take a few minutes. The log will appear here once the process is complete.",
=======
=======
>>>>>>> theirs
    if update.callback_query:
        await update.callback_query.answer()
        base_message = update.callback_query.message
    else:
        base_message = update.effective_message

    message = await base_message.reply_text(
        "⏳ **Update initiated...**\n\nThis may take a few minutes. "
        "The log will appear here once the process is complete.",
>>>>>>> theirs
        parse_mode='Markdown'
    )

    try:
        update_log = apply_update()
        await progress_message.edit_text(update_log, parse_mode='Markdown')
    except Exception as exc:
        logger.error("An error occurred in the update process: %s", exc, exc_info=True)
        await progress_message.edit_text(
            "An unexpected error occurred.\nCheck the logs for more details.\n\n`{}`".format(exc),
            parse_mode='Markdown'
        )


async def post_shutdown(application: Application) -> None:
    """Gracefully shuts down the SSH manager and database connections."""
    logger.info("Bot is shutting down...")
    if ssh_manager:
        ssh_manager.stop_health_check()
        await ssh_manager.close_all_connections()
    close_db_connection()

async def post_init(application: Application) -> None:
    """Starts the SSH health check after the application has been initialized."""
    ssh_manager.start_health_check()

def main() -> None:
    """
    Initializes and runs the Telegram bot application.
    This is the main entry point of the bot.
    """
    global ssh_manager

    # --- Pre-flight Checks ---
    # Ensure the bot token is configured before proceeding.
    if not config.telegram_token:
        logger.error("FATAL: Telegram token is not configured. Please run the setup wizard.")
        sys.exit(1)

    # --- Core Component Initialization ---
    # Set up the database and the SSH connection manager.
    try:
        initialize_database()
        ssh_manager = SSHManager()
    except Exception as e:
        logger.critical(f"FATAL: Error during database or SSH manager initialization: {e}", exc_info=True)
        sys.exit(1)

    # --- Application Setup ---
    # Create the Telegram Application object, linking it to our lifecycle hooks.
    application = Application.builder().token(config.telegram_token).post_init(post_init).post_shutdown(post_shutdown).build()

    # --- Error Handling ---
    # Register a global error handler to catch any unhandled exceptions and prevent crashes.
    application.add_error_handler(error_handler)

    # --- Conversation Handlers ---
    # These handlers manage multi-step interactions with the user, like adding a server or running a command.
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

    # --- UI & Menu Handlers ---
    # These handlers respond to button presses (CallbackQuery) from the user.
    # Specific callback handlers keep the routing explicit and easier to audit.
    application.add_handler(CallbackQueryHandler(main_menu, pattern='^main_menu$'))
    application.add_handler(CallbackQueryHandler(connect_server_menu, pattern='^connect_server_menu$'))
    application.add_handler(CallbackQueryHandler(remove_server_menu, pattern='^remove_server_menu$'))
    application.add_handler(CallbackQueryHandler(update_bot_command, pattern='^update_bot$'))
    application.add_handler(CallbackQueryHandler(handle_server_connection, pattern='^connect_'))
    application.add_handler(CallbackQueryHandler(start_shell_session, pattern='^start_shell_'))
    application.add_handler(CallbackQueryHandler(disconnect, pattern='^disconnect_'))
    application.add_handler(CallbackQueryHandler(remove_server_confirm, pattern='^remove_'))

    # --- Command Handlers ---
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('debug', toggle_debug_mode))
    application.add_handler(CommandHandler('exit_shell', exit_shell))
    application.add_handler(CommandHandler('check_updates', check_for_updates_command))
    application.add_handler(CommandHandler('update_bot', update_bot_command))

    # --- Start Health Check & Run Bot ---
    logger.info("Bot is starting...")

    # Special mode for CI/CD smoke test
    if os.environ.get("SMOKE_TEST"):
        logger.info("Smoke test mode enabled. Bot will not connect to Telegram.")
        # Keep the event loop running for a short time for the test
        loop = asyncio.get_event_loop()
        loop.run_until_complete(asyncio.sleep(15))
    else:
        application.run_polling()

if __name__ == "__main__":
    main()
