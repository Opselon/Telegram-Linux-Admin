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
from .ssh_manager import SSHManager
from .updater import check_for_updates, apply_update
from .database import (
    get_whitelisted_users, get_all_servers, initialize_database, DB_FILE,
    close_db_connection, add_server, remove_server
)
from functools import wraps

# --- Globals & Logging ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

ssh_manager = None
user_connections = {}
whitelisted_users = []
telegram_token = ""
RESTORING = False
SHELL_MODE_USERS = set()

# Conversation states for adding a server
(ALIAS, HOSTNAME, USER, AUTH_METHOD, PASSWORD, KEY_PATH) = range(6)

# --- Authorization ---
def authorized(func):
    """Decorator to check if the user is authorized."""
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id not in whitelisted_users:
            if update.callback_query:
                await update.callback_query.answer("🚫 You are not authorized to use this bot.", show_alert=True)
            else:
                await update.message.reply_text("🚫 **Access Denied**\nYou are not authorized to use this bot.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

# --- Add/Remove Server ---
@authorized
async def add_server_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the conversation to add a new server."""
    await update.message.reply_text("🖥️ **Add a New Server**\n\nLet's add a new server. First, what is the short alias for this server? (e.g., 'webserver')")
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
        await update.message.reply_text("🤷 No servers to remove.")
        return

    keyboard = []
    for server in servers:
        keyboard.append([InlineKeyboardButton(f"🗑️ {server['alias']}", callback_data=f"remove_{server['alias']}")])
    keyboard.append([InlineKeyboardButton("🔙 Cancel", callback_data='main_menu')])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text('🗑️ **Select a server to remove:**', reply_markup=reply_markup)

@authorized
async def remove_server_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Removes a server after confirmation."""
    query = update.callback_query
    await query.answer()

    alias = query.data.split('_', 1)[1]
    remove_server(alias)
    ssh_manager.refresh_server_configs()
    await query.edit_message_text(f"✅ **Server '{alias}' removed successfully!**", parse_mode='Markdown')


# Other handlers remain the same...

def main() -> None:
    # ...

    add_server_handler = ConversationHandler(
        entry_points=[CommandHandler('add_server', add_server_start)],
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
    application.add_handler(add_server_handler)
    application.add_handler(CommandHandler('remove_server', remove_server_menu))
    application.add_handler(CallbackQueryHandler(remove_server_confirm, pattern='^remove_'))

    # ... (the rest of the main function is unchanged)

    logger.info("Bot started successfully!")
    application.run_polling()

if __name__ == "__main__":
    main()
