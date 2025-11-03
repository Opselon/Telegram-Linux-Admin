import logging
import json
import asyncio
import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from telegram.error import BadRequest
from src.ssh_manager import SSHManager
from src.updater import check_for_updates, apply_update
from src.database import get_whitelisted_users, get_all_servers, initialize_database, DB_FILE, close_db_connection
from functools import wraps

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

ssh_manager = None
user_connections = {}  # Maps user_id to server_alias
whitelisted_users = []
telegram_token = ""
RESTORING = False

def authorized(func):
    """Decorator to check if the user is authorized."""
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id not in whitelisted_users:
            if update.callback_query:
                await update.callback_query.answer("You are not authorized.", show_alert=True)
            else:
                await update.message.reply_text("You are not authorized to use this bot.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

@authorized
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a welcome message when the /start command is issued."""
    await menu(update, context)

@authorized
async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays the main menu."""
    keyboard = [
        [InlineKeyboardButton("Connect to Server", callback_data='connect_menu')],
        [InlineKeyboardButton("Backup & Restore", callback_data='backup_menu')],
        [InlineKeyboardButton("Check for Updates", callback_data='check_updates')],
        [InlineKeyboardButton("Reload Server List", callback_data='reload_servers')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text('Please choose an option:', reply_markup=reply_markup)

@authorized
async def backup_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays the backup and restore menu."""
    keyboard = [
        [InlineKeyboardButton("Backup Database", callback_data='backup')],
        [InlineKeyboardButton("Restore from Backup", callback_data='restore')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query:
        await update.callback_query.message.edit_text('Backup & Restore Options:', reply_markup=reply_markup)
    else:
        await update.message.reply_text('Backup & Restore Options:', reply_markup=reply_markup)

@authorized
async def backup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends the database file to the user."""
    query = update.callback_query
    await query.answer()
    try:
        await context.bot.send_document(chat_id=query.from_user.id, document=open(DB_FILE, 'rb'))
    except Exception as e:
        await query.message.reply_text(f"Failed to send backup: {e}")


@authorized
async def restore(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Prompts the user to upload a database file."""
    global RESTORING
    RESTORING = True
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Please upload the `database.db` file to restore.")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles file uploads for restoring the database."""
    global RESTORING
    if not RESTORING:
        return

    if not update.message.document or update.message.document.file_name != 'database.db':
        await update.message.reply_text("Invalid file. Please upload a `database.db` file.")
        return

    try:
        file = await context.bot.get_file(update.message.document.file_id)
        await file.download_to_drive(DB_FILE)
        await update.message.reply_text("Database restored. Restarting the bot to apply changes...")

        # This will trigger the systemd service to restart the bot
        os._exit(0)
    except Exception as e:
        await update.message.reply_text(f"Failed to restore database: {e}")
    finally:
        RESTORING = False


@authorized
async def connect_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays a menu of servers to connect to."""
    servers = get_all_servers()
    keyboard = []
    for server in servers:
        keyboard.append([InlineKeyboardButton(server['alias'], callback_data=f"connect_{server['alias']}")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query:
        await update.callback_query.message.reply_text('Select a server to connect to:', reply_markup=reply_markup)
    else:
        await update.message.reply_text('Select a server to connect to:', reply_markup=reply_markup)


@authorized
async def connect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Connects to a server."""
    query = update.callback_query
    await query.answer()

    alias = query.data.split('_', 1)[1]
    user_id = query.from_user.id

    try:
        await ssh_manager.get_connection(alias)
        user_connections[user_id] = alias
        await query.edit_message_text(text=f"Successfully connected to {alias}.")
    except (ValueError, ConnectionError) as e:
        await query.edit_message_text(text=str(e))

@authorized
async def disconnect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Disconnects from the current server."""
    user_id = update.message.from_user.id
    if user_id in user_connections:
        alias = user_connections[user_id]
        await ssh_manager.disconnect(alias)
        del user_connections[user_id]
        await update.message.reply_text(f"Disconnected from {alias}.")
    else:
        await update.message.reply_text("You are not connected to any server.")

@authorized
async def server(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shows the currently connected server."""
    user_id = update.message.from_user.id
    if user_id in user_connections:
        alias = user_connections[user_id]
        await update.message.reply_text(f"Currently connected to: {alias}")
    else:
        await update.message.reply_text("You are not connected to any server.")

@authorized
async def execute_shell_command(update: Update, context: ContextTypes.DEFAULT_TYPE, command: str) -> None:
    """Executes a shell command with real-time output."""
    user_id = update.message.from_user.id
    if user_id not in user_connections:
        await update.message.reply_text("Not connected to any server. Use /connect <server_alias> first.")
        return

    alias = user_connections[user_id]
    message = await update.message.reply_text(f"Executing on {alias}: `{command}`\n\n--- output ---")

    output = ""
    last_edit_time = asyncio.get_event_loop().time()

    try:
        async for line, stream in ssh_manager.run_command(alias, command):
            output += line
            current_time = asyncio.get_event_loop().time()
            if current_time - last_edit_time > 1.0:
                try:
                    truncated_output = output[-4000:]
                    await message.edit_text(f"Executing on {alias}: `{command}`\n\n--- output ---\n{truncated_output}")
                    last_edit_time = current_time
                except BadRequest as e:
                    if "Message is not modified" not in str(e):
                        logger.error(f"Error editing message: {e}")

        final_output = output[-4000:]
        await message.edit_text(f"Executing on {alias}: `{command}`\n\n--- output ---\n{final_output}\n\n--- command finished ---")

    except Exception as e:
        await message.edit_text(f"An error occurred: {e}")
        logger.error(f"Error executing command '{command}' on {alias}: {e}")

@authorized
async def handle_raw_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles raw shell commands."""
    await execute_shell_command(update, context, update.message.text)

@authorized
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs a system status command."""
    command = "uptime && echo '---' && free -h && echo '---' && df -h"
    await execute_shell_command(update, context, command)

@authorized
async def update_packages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs 'apt update'."""
    command = "apt update"
    await execute_shell_command(update, context, command)

@authorized
async def top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs 'top -b -n 1'."""
    command = "top -b -n 1"
    await execute_shell_command(update, context, command)

@authorized
async def check_updates_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Checks for updates when the button is pressed."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Checking for updates...")

    update_status = check_for_updates()
    if "An update is available!" in update_status:
        keyboard = [[InlineKeyboardButton("Apply Update", callback_data='apply_update')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("Update available!", reply_markup=reply_markup)
    else:
        await query.edit_message_text(update_status)

@authorized
async def apply_update_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Applies updates when the button is pressed."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Applying update...")

    result = apply_update()
    await query.edit_message_text(result)

@authorized
async def reload_servers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reloads the server list from the database."""
    query = update.callback_query
    await query.answer()
    ssh_manager.refresh_server_configs()
    await query.edit_message_text("Server list reloaded.")

async def post_shutdown(application: Application):
    await ssh_manager.disconnect_all()
    close_db_connection()


def main() -> None:
    """Start the bot."""
    global ssh_manager, whitelisted_users, telegram_token

    initialize_database()
    whitelisted_users = get_whitelisted_users()

    try:
        with open('config.json', 'r') as f:
            config = json.load(f)
            telegram_token = config['telegram_token']
    except (FileNotFoundError, KeyError):
        logger.error("config.json with telegram_token not found. Please run the setup script.")
        return

    ssh_manager = SSHManager()

    application = Application.builder().token(telegram_token).post_shutdown(post_shutdown).build()

    # Core commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("menu", menu))
    application.add_handler(CommandHandler("disconnect", disconnect))
    application.add_handler(CommandHandler("server", server))

    # Shortcut commands
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("update", update_packages))
    application.add_handler(CommandHandler("top", top))

    # Callback handlers
    application.add_handler(CallbackQueryHandler(connect_menu, pattern='^connect_menu$'))
    application.add_handler(CallbackQueryHandler(connect, pattern='^connect_'))
    application.add_handler(CallbackQueryHandler(check_updates_button, pattern='^check_updates$'))
    application.add_handler(CallbackQueryHandler(apply_update_button, pattern='^apply_update$'))
    application.add_handler(CallbackQueryHandler(reload_servers, pattern='^reload_servers$'))
    application.add_handler(CallbackQueryHandler(backup_menu, pattern='^backup_menu$'))
    application.add_handler(CallbackQueryHandler(backup, pattern='^backup$'))
    application.add_handler(CallbackQueryHandler(restore, pattern='^restore$'))

    # Generic command handler for any text
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_raw_command))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))


    application.run_polling()

if __name__ == "__main__":
    main()
