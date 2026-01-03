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
import time
import re
import hashlib
import shutil
from datetime import datetime
from pathlib import Path
from telegram import BotCommand, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, ContextTypes,
    CallbackQueryHandler, ConversationHandler
)
from telegram.error import BadRequest, InvalidToken, TimedOut, NetworkError
try:
    from httpcore import ReadTimeout as HTTPCoreReadTimeout
except ImportError:
    HTTPCoreReadTimeout = None
try:
    from httpx import ReadTimeout as HTTPXReadTimeout
except ImportError:
    HTTPXReadTimeout = None
import asyncssh
from .ssh_manager import SSHManager
from .database import (
    get_all_servers,
    initialize_database,
    close_db_connection,
    add_server,
    remove_server,
    get_user_language_preference,
    set_user_language_preference,
    get_user_server_limit,
    get_user_server_count,
    get_total_users,
    get_users_joined_today,
    get_total_servers,
    get_servers_added_today,
    get_plan_distribution,
    get_language_distribution,
    get_recent_servers,
    get_active_users_count,
    get_servers_per_user_stats,
    get_servers_added_this_week,
    get_top_users_by_servers,
    get_database_size,
    get_system_health,
)
from .config import config
from functools import wraps, cache
from typing import Optional, Dict, TypedDict, Literal, Any
from dataclasses import dataclass, field
from contextlib import asynccontextmanager
import secrets
from .localization import (
    translate,
    SUPPORTED_LANGUAGES,
    DEFAULT_LANGUAGE,
    get_language_label,
)
from .parse_mode import (
    get_parse_mode,
    escape_text,
    format_bold,
    format_code,
    format_code_block,
    safe_format_message,
    MessageBuilder,
)

# --- Globals & Logging ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Modern Type Definitions (2026 Standards) ---
@dataclass(frozen=True, slots=True)
class ServerInfo:
    """Immutable server information."""
    alias: str
    hostname: str
    user: str
    owner_id: int


@dataclass(slots=True)
class UserSession:
    """User session state."""
    user_id: int
    connected_server: str | None = None
    language: str = DEFAULT_LANGUAGE
    last_activity: datetime = field(default_factory=datetime.now)


class DashboardStats(TypedDict, total=False):
    """Type-safe dashboard statistics."""
    total_users: int
    active_users: int
    total_servers: int
    servers_today: int
    servers_week: int
    cpu_percent: float | str
    memory_percent: float | str
    disk_percent: float | str


# --- Modern Logging (2026 Standards) ---
try:
    from .logger_config import setup_logging, get_logger
    # Setup structured logging
    log_file = Path("var/log/bot.log")
    setup_logging(
        level=logging.INFO,
        use_json=False,  # Human-readable for console, JSON for file
        log_file=log_file
    )
    logger = get_logger(__name__)
except ImportError:
    # Fallback to basic logging
    logging.basicConfig(
        format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
        level=logging.INFO,
        handlers=[logging.StreamHandler()],
    )
    logger = logging.getLogger(__name__)

# Structured logging helper (2026 standards)
def log_structured(level: int, message: str, **kwargs: Any) -> None:
    """Structured logging helper with context."""
    extra = {f"ctx_{k}": v for k, v in kwargs.items()}
    logger.log(level, message, extra=extra)

ssh_manager: SSHManager | None = None
user_connections: dict[int, str] = {}
user_sessions: dict[int, UserSession] = {}
user_language_cache: dict[int, str] = {}
RESTORING = False
SHELL_MODE_USERS: set[int] = set()
DEBUG_MODE = False
LOCK_FILE = Path("bot.lock")
MONITORING_TASKS: dict[int, asyncio.Task] = {}
SUPPORTED_LANGUAGE_SET = set(SUPPORTED_LANGUAGES)

# --- Conversation States ---
(
    AWAIT_COMMAND, ALIAS, HOSTNAME, USER, AUTH_METHOD, PASSWORD, KEY_PATH,
    AWAIT_RESTORE_CONFIRMATION, AWAIT_RESTORE_FILE, AWAIT_SERVICE_NAME, AWAIT_PACKAGE_NAME, AWAIT_CONTAINER_NAME,
    AWAIT_FILE_PATH, AWAIT_UPLOAD_FILE, AWAIT_PID, AWAIT_FIREWALL_RULE
) = range(16)

# --- Authorization ---
def _extract_user_id(update: Update) -> int | None:
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
    """Decorator to welcome any user."""
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

        # No whitelist check, all users are welcome.
        await send_debug_message(update, f"Processing request for user_id: {user_id}...")
        return await func(update, context, *args, **kwargs)
    return wrapped

def admin_authorized(func):
    """Decorator to check if the user is the admin (first whitelisted user)."""
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

        # Ensure there is a whitelist and the user is the first one in it
        if not config.whitelisted_users or user_id != config.whitelisted_users[0]:
            logger.warning(f"Admin access denied for user_id: {user_id}")
            if update.callback_query:
                await update.callback_query.answer("🚫 You are not authorized for this admin-only action.", show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text("🚫 **Access Denied**\nThis is an admin-only feature.", parse_mode='Markdown')
            return

        await send_debug_message(update, "Admin authorization successful.")
        return await func(update, context, *args, **kwargs)
    return wrapped

def _resolve_message(update: Update):
    if update.message:
        return update.message
    if update.callback_query:
        return update.callback_query.message
    return None


@cache
def _get_user_language(user_id: int | None) -> str:
    """Returns the cached language preference for a user."""
    if user_id is None:
        return DEFAULT_LANGUAGE

    language = user_language_cache.get(user_id)
    if language:
        return language

    preference = get_user_language_preference(user_id)
    if preference not in SUPPORTED_LANGUAGE_SET:
        preference = DEFAULT_LANGUAGE

    user_language_cache[user_id] = preference
    return preference


def _translate_for_user(user_id: int | None, key: str, **kwargs: str) -> str:
    """Translates a key using the user's language preference."""
    language = _get_user_language(user_id)
    return translate(key, language, **kwargs)


@cache
def _get_user_parse_mode(user_id: int | None) -> str | None:
    """Returns the best parse mode for the user's language."""
    language = _get_user_language(user_id)
    return get_parse_mode(language)


def _safe_send_message(
    chat,
    text: str,
    user_id: int | None = None,
    reply_markup=None,
    **kwargs
):
    """
    Pro version: Safely sends a message with language-aware parse mode.
    
    Args:
        chat: Chat object to send message to
        text: Message text
        user_id: User ID for language detection
        reply_markup: Optional reply markup
        **kwargs: Additional arguments for send_message
    """
    parse_mode = _get_user_parse_mode(user_id) if user_id else get_parse_mode()
    return chat.send_message(
        text,
        parse_mode=parse_mode,
        reply_markup=reply_markup,
        **kwargs
    )


def _safe_edit_message_text(
    message_or_query,
    text: str,
    user_id: int | None = None,
    reply_markup=None,
    **kwargs
):
    """
    Pro version: Safely edits message text with language-aware parse mode.
    
    Args:
        message_or_query: Message or CallbackQuery object
        text: New message text
        user_id: User ID for language detection
        reply_markup: Optional reply markup
        **kwargs: Additional arguments for edit_message_text
    """
    parse_mode = _get_user_parse_mode(user_id) if user_id else get_parse_mode()
    
    if hasattr(message_or_query, 'edit_message_text'):
        # It's a CallbackQuery
        return message_or_query.edit_message_text(
            text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            **kwargs
        )
    else:
        # It's a Message
        return message_or_query.edit_text(
            text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            **kwargs
        )


def _build_language_keyboard(active_language: str) -> InlineKeyboardMarkup:
    """Builds the inline keyboard for language selection."""
    buttons = []
    for code in SUPPORTED_LANGUAGES:
        prefix = "✅ " if code == active_language else ""
        buttons.append([
            InlineKeyboardButton(
                f"{prefix}{get_language_label(code)}",
                callback_data=f"set_language_{code}"
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            translate('button_back_main_menu', active_language),
            callback_data='main_menu'
        )
    ])
    return InlineKeyboardMarkup(buttons)


async def _send_connection_error(
    query: object,
    user_id: int | None,
    key: str,
    **kwargs: str,
) -> None:
    """Pro version: Sends a translated connection error message with language-aware parse mode."""
    try:
        error_message = _translate_for_user(user_id, key, **kwargs)
        parse_mode = _get_user_parse_mode(user_id)
        await query.edit_message_text(error_message, parse_mode=parse_mode)
    except BadRequest as e:
        # Ignore "Message is not modified" errors - this happens when the message content hasn't changed
        if "Message is not modified" in str(e):
            logger.debug(f"Message not modified when sending connection error (expected): {key}")
        else:
            logger.warning(f"Failed to send connection error message: {e}")
    except Exception as e:
        logger.warning(f"Unexpected error when sending connection error: {e}")

# --- Add/Remove Server ---
@authorized
async def add_server_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the conversation to add a new server."""
    user_id = _extract_user_id(update)
    language = _get_user_language(user_id)
    limit = get_user_server_limit(user_id)
    current = get_user_server_count(user_id)

    if current >= limit:
        message = translate(
            'server_limit_reached',
            language,
            limit=limit,
        )
        if update.callback_query:
            await update.callback_query.answer(message, show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text(message)
        return ConversationHandler.END

    prompt = translate('add_server_prompt', language)

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(prompt, parse_mode='Markdown')
    else:
        await update.effective_message.reply_text(prompt, parse_mode='Markdown')

    return ALIAS


async def get_alias(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = _extract_user_id(update)
    language = _get_user_language(user_id)
    context.user_data['alias'] = update.message.text
    await update.message.reply_text(
        translate('prompt_hostname', language),
    )
    return HOSTNAME

async def get_hostname(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = _extract_user_id(update)
    language = _get_user_language(user_id)
    context.user_data['hostname'] = update.message.text
    await update.message.reply_text(
        translate('prompt_username', language),
    )
    return USER

async def get_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = _extract_user_id(update)
    language = _get_user_language(user_id)
    context.user_data['user'] = update.message.text
    keyboard = [[
        InlineKeyboardButton(
            translate('button_auth_key', language),
            callback_data='key'
        ),
        InlineKeyboardButton(
            translate('button_auth_password', language),
            callback_data='password'
        )
    ]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        translate('prompt_auth_method', language),
        reply_markup=reply_markup,
    )
    return AUTH_METHOD

async def get_auth_method(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user_id = _extract_user_id(update)
    language = _get_user_language(user_id)
    await query.answer()

    if query.data == 'password':
        await query.message.reply_text(
            translate('prompt_password', language),
        )
        return PASSWORD
    else:
        await query.message.reply_text(
            translate('prompt_key_path', language),
        )
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
    user_id = _extract_user_id(update)
    language = _get_user_language(user_id)
    try:
        user_id = _extract_user_id(update)
        language = _get_user_language(user_id)
        owner_id = update.effective_user.id
        add_server(
            owner_id,
            alias=context.user_data['alias'],
            hostname=context.user_data['hostname'],
            user=context.user_data['user'],
            password=context.user_data.get('password'),
            key_path=context.user_data.get('key_path')
        )
        confirmation = translate('server_added', language, alias=context.user_data['alias'])
        await update.message.reply_text(confirmation, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(
            translate('server_add_error', language, error=str(e)),
            parse_mode='Markdown'
        )
    finally:
        context.user_data.clear()

async def cancel_add_server(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the add server conversation."""
    user_id = _extract_user_id(update)
    language = _get_user_language(user_id)
    await update.message.reply_text(
        translate('server_add_cancelled', language),
    )
    context.user_data.clear()
    return ConversationHandler.END

@authorized
async def remove_server_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays a menu of servers to remove."""
    user_id = _extract_user_id(update)
    language = _get_user_language(user_id)
    servers = get_all_servers(user_id)
    if not servers:
        message = translate('no_servers_to_remove', language)
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(message, parse_mode='Markdown')
        else:
            await update.effective_message.reply_text(message, parse_mode='Markdown')
        return

    remove_label = translate('button_remove_server', language)
    keyboard = [
        [InlineKeyboardButton(f"{remove_label} {server['alias']}", callback_data=f"remove_{server['alias']}")]
        for server in servers
    ]
    keyboard.append([
        InlineKeyboardButton(
            translate('button_back_main_menu', language),
            callback_data="main_menu"
        )
    ])

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
    language = _get_user_language(user_id)

    # Base keyboard for all users
    keyboard = [
        [InlineKeyboardButton(
            translate('button_connect_server', language),
            callback_data='connect_server_menu'
        )],
        [
            InlineKeyboardButton(
                translate('button_add_server', language),
                callback_data='add_server_start'
            ),
            InlineKeyboardButton(
                translate('button_remove_server', language),
                callback_data='remove_server_menu'
            )
        ],
        [InlineKeyboardButton(
            translate('button_language_settings', language),
            callback_data='language_menu'
        )],
    ]

    # Admin-only buttons
    if config.whitelisted_users and user_id == config.whitelisted_users[0]:
        admin_buttons = [
            [
                InlineKeyboardButton("💾 Backup", callback_data='backup'),
                InlineKeyboardButton("🔄 Restore", callback_data='restore')
            ],
            [InlineKeyboardButton("🔄 Update Bot", callback_data='update_bot')],
        ]
        keyboard.extend(admin_buttons)

    reply_markup = InlineKeyboardMarkup(keyboard)
    menu_text = _translate_for_user(user_id, 'main_menu_welcome')

    parse_mode = _get_user_parse_mode(user_id)
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(escape_text(menu_text, parse_mode), reply_markup=reply_markup, parse_mode=parse_mode)
    else:
        await update.message.reply_text(escape_text(menu_text, parse_mode), reply_markup=reply_markup, parse_mode=parse_mode)


@authorized
async def language_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays or handles the language selection menu."""
    user_id = _extract_user_id(update)
    language = _get_user_language(user_id)
    title = translate('language_menu_title', language)
    description = translate(
        'language_menu_description',
        language,
        language_name=get_language_label(language)
    )
    text = f"{title}\n\n{description}"
    reply_markup = _build_language_keyboard(language)

    message = _resolve_message(update)
    if update.callback_query:
        await update.callback_query.answer()
        parse_mode = _get_user_parse_mode(user_id)
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    elif message:
        parse_mode = _get_user_parse_mode(user_id)
        await message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)


@authorized
async def set_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Saves the selected language preference for the current user."""
    query = update.callback_query
    user_id = update.effective_user.id
    lang_code = query.data.split('_', 2)[2]

    if lang_code not in SUPPORTED_LANGUAGE_SET:
        await query.answer(_translate_for_user(user_id, 'language_unsupported'), show_alert=True)
        return

    set_user_language_preference(user_id, lang_code)
    user_language_cache[user_id] = lang_code
    await query.answer(translate('language_saved_toast', lang_code), show_alert=False)

    title = translate('language_updated', lang_code, language_name=get_language_label(lang_code))
    description = translate(
        'language_menu_description',
        lang_code,
        language_name=get_language_label(lang_code)
    )
    text = f"{title}\n\n{description}"
    reply_markup = _build_language_keyboard(lang_code)
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')


# --- Server Connection ---
@authorized
async def connect_server_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays a menu of servers to connect to."""
    logger.info("Displaying connect server menu.")
    user_id = _extract_user_id(update)
    language = _get_user_language(user_id)
    servers = get_all_servers(user_id)
    if not servers:
        await update.callback_query.answer(translate('no_servers_configured', language), show_alert=True)
        return

    keyboard = []
    for server in servers:
        keyboard.append([InlineKeyboardButton(f"🖥️ {server['alias']}", callback_data=f"connect_{server['alias']}")])
    keyboard.append([InlineKeyboardButton(
        translate('button_back_main_menu', language),
        callback_data='main_menu'
    )])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.answer()
    parse_mode = get_parse_mode(language)
    await update.callback_query.edit_message_text(
        translate('connect_menu_prompt', language),
        reply_markup=reply_markup,
        parse_mode=parse_mode
    )

_user_cooldowns = {}

@authorized
async def handle_server_connection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the connection with Maximum Security (Validation + Rate Limit + Timeout)."""
    query = update.callback_query
    
    # 1. SECURITY: Rate Limiting (جلوگیری از اسپم کردن دکمه اتصال)
    user_id = update.effective_user.id
    current_time = time.time()
    last_request = _user_cooldowns.get(user_id, 0)
    
    # اگر فاصله بین درخواست‌ها کمتر از ۳ ثانیه بود، درخواست رو رد کن
    if current_time - last_request < 3.0:
        await query.answer("⚠️ Please wait a moment before connecting again.", show_alert=True)
        return
    _user_cooldowns[user_id] = current_time

    # استخراج اطلاعات
    try:
        raw_alias = query.data.split('_', 1)[1]
    except IndexError:
        await query.answer("❌ Invalid request data.", show_alert=True)
        return

    # 2. SECURITY: Strict Input Validation (جلوگیری از تزریق کد یا کاراکتر مخرب)
    # فقط حروف انگلیسی، اعداد، آندرلاین و خط تیره مجازه.
    if not re.match(r'^[a-zA-Z0-9_-]+$', raw_alias):
        logger.warning(f"SECURITY ALERT: User {user_id} tried malicious alias: '{raw_alias}'")
        await query.answer("🚫 Security Violation: Invalid alias format.", show_alert=True)
        return

    alias = raw_alias # حالا که تمیزه، استفاده‌ش می‌کنیم
    language = _get_user_language(user_id)
    await query.answer() # پاسخ به کال‌بک تلگرام تا لودینگ بالای صفحه بره

    # --- Helper: Pro Safe Edit ---
    async def safe_edit(text_key: str, reply_markup: InlineKeyboardMarkup = None, **kwargs):
        """Pro version: Safely edit message text with language-aware parse mode."""
        try:
            text = translate(text_key, language, **kwargs)
            parse_mode = get_parse_mode(language)
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        except BadRequest as e:
            # Ignore "Message is not modified" errors - this is expected when content hasn't changed
            if "Message is not modified" in str(e):
                logger.debug(f"Message not modified (expected) for {alias}: {text_key}")
            else:
                logger.warning(f"UI update failed for {alias}: {e}")
        except Exception as e:
            # Handle other exceptions (like timeouts) gracefully
            logger.warning(f"Unexpected error in safe_edit for {alias}: {e}")
            # Don't raise - continue execution

    try:
        await safe_edit('connecting_to_server', alias=alias)

        # 3. SECURITY: Hard Timeout (جلوگیری از فریز شدن ربات)
        # اتصال نباید بیشتر از ۱۰ ثانیه طول بکشه.
        async def _connect_with_timeout():
            async for _, __ in ssh_manager.run_command(user_id, alias, "echo 'Secure Handshake'"):
                pass

        # Modern async timeout (Python 3.11+)
        try:
            async with asyncio.timeout(10.0):
                await _connect_with_timeout()
        except TimeoutError:
            logger.warning(f"Connection timeout for {alias} (User: {user_id})")
            await safe_edit('error_connection_refused', alias=alias) # پیام مناسب تایم‌اوت
            return

        # ثبت موفقیت
        user_connections[user_id] = alias
        logger.info(f"SECURE CONN: User {user_id} -> Server '{alias}'")

        # چیدمان دکمه‌ها (همون چیدمان تمیز قبلی)
        keyboard = [
            [InlineKeyboardButton("▶️ Run Command", callback_data=f"run_command_{alias}"),
             InlineKeyboardButton("🖥️ Terminal", callback_data=f"start_shell_{alias}")],
            [InlineKeyboardButton("📊 Status", callback_data=f"server_status_menu_{alias}"),
             InlineKeyboardButton("⚙️ Sys Ops", callback_data=f"system_commands_menu_{alias}")],
            [InlineKeyboardButton("🔧 Services", callback_data=f"service_management_menu_{alias}"),
             InlineKeyboardButton("⚙️ Processes", callback_data=f"process_management_menu_{alias}")],
            [InlineKeyboardButton("🐳 Docker", callback_data=f"docker_management_menu_{alias}"),
             InlineKeyboardButton("📦 Packages", callback_data=f"package_management_menu_{alias}")],
            [InlineKeyboardButton("📁 Files", callback_data=f"file_manager_menu_{alias}"),
             InlineKeyboardButton("🔥 Firewall", callback_data=f"firewall_management_menu_{alias}")],
            [InlineKeyboardButton("🔌 Disconnect", callback_data=f"disconnect_{alias}")]
        ]
        
        await safe_edit('connected_to_server', reply_markup=InlineKeyboardMarkup(keyboard), alias=alias)

    # --- Secure Error Handling ---
    # ارورها رو کامل لاگ می‌کنیم ولی به کاربر جزئیات فنی نمیدیم
    except asyncssh.PermissionDenied:
        logger.warning(f"AUTH FAIL: {alias} | User: {user_id}")
        await _send_connection_error(query, user_id, 'error_auth_failed', alias=alias)
    except ConnectionRefusedError:
        logger.warning(f"CONN REFUSED: {alias}")
        await _send_connection_error(query, user_id, 'error_connection_refused', alias=alias)
    except socket.gaierror:
        logger.warning(f"DNS FAIL: {alias}")
        await _send_connection_error(query, user_id, 'error_host_not_found', alias=alias, error="DNS Error")
    except Exception as e:
        # امنیت: لاگ کردن کامل ارور برای ادمین، اما ارسال پیام عمومی برای کاربر
        logger.error(f"UNEXPECTED ERROR in connection handler: {e}", exc_info=True)
        await _send_connection_error(query, user_id, 'error_generic_connection', alias=alias, error="Internal Security Error")

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

    user_id = update.effective_user.id
    alias = context.user_data['alias']
    parse_mode = _get_user_parse_mode(user_id)
    builder = MessageBuilder(parse_mode)
    builder.add_text("Ok, please send the command you want to run on ")
    builder.add_bold(alias)
    builder.add_text(".")
    await query.edit_message_text(builder.build(), parse_mode=parse_mode)
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

    # Initial message - Pro version with language-aware formatting
    parse_mode = _get_user_parse_mode(user_id)
    builder = MessageBuilder(parse_mode)
    builder.add_text("🛰️ Running ")
    builder.add_code(command)
    builder.add_text(" on ")
    builder.add_code(alias)
    builder.add_text("...")
    result_message = await update.message.reply_text(builder.build(), parse_mode=parse_mode)

    output_buffer: list[str] = []
    last_sent_text = ""
    edit_interval = 1.5  # seconds between UI refreshes
    last_edit_time = time.monotonic()

    try:
        async for item, stream in ssh_manager.run_command(user_id, alias, command):
            # Skip PID events (AsyncSSH doesn’t expose it natively)
            if stream == 'pid':
                continue

            if stream in ('stdout', 'stderr'):
                output_buffer.append(item)

                # Periodically update Telegram message without flooding API
                # Use monotonic time for better performance
                now = time.monotonic()
                if now - last_edit_time >= edit_interval:
                    partial_output = ''.join(output_buffer)[-3800:]  # keep last chunk for preview
                    if partial_output != last_sent_text:
                        try:
                            # Use language-aware parse mode for code blocks
                            code_block = format_code_block(partial_output, "", parse_mode)
                            await result_message.edit_text(code_block, parse_mode=parse_mode)
                            last_sent_text = partial_output
                        except BadRequest as e:
                            if "Message is not modified" not in str(e):
                                logger.warning(f"Telegram update error: {e}")
                        last_edit_time = now

        # Combine all output once command completes
        final_output = ''.join(output_buffer).strip()
        if not final_output:
            final_output = "[No output returned]"

        # Pro version: Build final message with proper formatting
        parse_mode = _get_user_parse_mode(user_id)
        builder = MessageBuilder(parse_mode)
        builder.add_text("✅ ")
        builder.add_bold("Command completed on ")
        builder.add_code(alias)
        builder.add_line()
        builder.add_code_block(final_output)
        
        final_text = builder.build()
        if len(final_text) > 4096:
            with tempfile.NamedTemporaryFile(mode="w+", delete=False, suffix=".txt") as f:
                f.write(final_output)
                f.flush()
            await result_message.delete()
            caption_builder = MessageBuilder(parse_mode)
            caption_builder.add_text("Output for ")
            caption_builder.add_code(command)
            await update.message.reply_document(
                document=open(f.name, "rb"),
                caption=caption_builder.build(),
                parse_mode=parse_mode
            )
            os.remove(f.name)
        else:
            await result_message.edit_text(final_text, parse_mode=parse_mode)

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
        user_id = update.effective_user.id
        await ssh_manager.kill_process(user_id, alias, int(pid))
        user_id = update.effective_user.id
        parse_mode = _get_user_parse_mode(user_id)
        builder = MessageBuilder(parse_mode)
        builder.add_text("✅ ")
        builder.add_bold("Command (PID: ")
        builder.add_code(str(pid))
        builder.add_bold(") cancelled on ")
        builder.add_code(alias)
        builder.add_text(".")
        await query.edit_message_text(builder.build(), parse_mode=parse_mode)
    except Exception as e:
        user_id = update.effective_user.id
        parse_mode = _get_user_parse_mode(user_id)
        builder = MessageBuilder(parse_mode)
        builder.add_text("❌ ")
        builder.add_bold("Error:")
        builder.add_text(" Could not cancel command.")
        builder.add_line()
        builder.add_code(str(e))
        await query.edit_message_text(builder.build(), parse_mode=parse_mode)


# --- Interactive Shell ---
@authorized
async def start_shell_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Starts an interactive shell session for the user."""
    query = update.callback_query
    await query.answer()
    alias = query.data.split('_', 2)[2]
    user_id = update.effective_user.id

    try:
        await ssh_manager.start_shell_session(user_id, alias)
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
        output = await ssh_manager.run_command_in_shell(user_id, alias, command)
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
            await ssh_manager.disconnect(user_id, alias)
            del user_connections[user_id]

        await update.message.reply_text("🔌 **Shell session terminated.**", parse_mode='Markdown')
        await main_menu(update, context)


# --- Error Handling ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the error and send a detailed message to the user."""
    error = context.error
    
    # Handle specific error types gracefully
    if isinstance(error, BadRequest) and "Message is not modified" in str(error):
        # This is a harmless error - message content hasn't changed
        logger.debug("Message not modified error (ignored): %s", error)
        return
    
    # Handle timeout errors (from various sources)
    timeout_errors = [TimedOut, NetworkError]
    if HTTPCoreReadTimeout:
        timeout_errors.append(HTTPCoreReadTimeout)
    if HTTPXReadTimeout:
        timeout_errors.append(HTTPXReadTimeout)
    
    # Check if it's a timeout error by type, cause, or message content
    is_timeout = isinstance(error, tuple(timeout_errors))
    if not is_timeout and hasattr(error, '__cause__') and error.__cause__:
        is_timeout = isinstance(error.__cause__, tuple(timeout_errors))
    if not is_timeout:
        error_str = str(error).lower()
        error_type_str = str(type(error)).lower()
        is_timeout = 'readtimeout' in error_type_str or 'timeout' in error_str
    
    if is_timeout:
        # Network/timeout errors - log but don't spam user with technical details
        logger.warning("Network/timeout error occurred: %s", error)
        if isinstance(update, Update) and update.effective_chat:
            try:
                await update.effective_chat.send_message(
                    "⚠️ **Connection timeout**\n\nThe request took too long to complete. Please try again.",
                    parse_mode='Markdown'
                )
            except Exception as e:
                logger.error(f"Failed to send timeout error message: {e}")
        return
    
    # Log other errors
    logger.error("Exception while handling an update:", exc_info=error)

    if isinstance(update, Update) and update.effective_chat:
        # Format the traceback
        tb_list = traceback.format_exception(None, error, error.__traceback__)
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
            async for item, stream in ssh_manager.run_command(user_id, alias, command):
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
            async for item, stream in ssh_manager.run_command(user_id, alias, command):
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
                async for item, stream in ssh_manager.run_command(user_id, alias, command):
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
    user_id = update.effective_user.id

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
        async for item, stream in ssh_manager.run_command(user_id, alias, command):
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
    user_id = update.effective_user.id

    command = f"sudo apt-get install -y {package_name}"

    result_message = await update.message.reply_text(f"Running `{command}` on `{alias}`...", parse_mode='Markdown')

    output = ""
    try:
        output = ""
        async for item, stream in ssh_manager.run_command(user_id, alias, command):
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
    user_id = update.effective_user.id

    command = f"docker {action} {container_name}"

    result_message = await update.message.reply_text(f"Running `{command}` on `{alias}`...", parse_mode='Markdown')

    output = ""
    try:
        output = ""
        async for item, stream in ssh_manager.run_command(user_id, alias, command):
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
    user_id = update.effective_user.id

    result_message = await query.edit_message_text(f"Running `{command}` on `{alias}`...", parse_mode='Markdown')

    output = ""
    try:
        output = ""
        async for item, stream in ssh_manager.run_command(user_id, alias, command):
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
    user_id = update.effective_user.id
    command = f"ls -la {path}"

    result_message = await update.message.reply_text(f"Running `{command}` on `{alias}`...", parse_mode='Markdown')

    output = ""
    try:
        output = ""
        async for item, stream in ssh_manager.run_command(user_id, alias, command):
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
    user_id = update.effective_user.id

    await update.message.reply_text(f"📥 Downloading `{remote_path}` from `{alias}`...", parse_mode='Markdown')

    try:
        with tempfile.NamedTemporaryFile(delete=False) as f:
            local_path = f.name
        await ssh_manager.download_file(user_id, alias, remote_path, local_path)
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
    user_id = update.effective_user.id

    local_path = document.file_name
    file = await document.get_file()
    await file.download_to_drive(local_path)

    await update.message.reply_text(f"📤 Uploading `{local_path}` to `{remote_path}` on `{alias}`...", parse_mode='Markdown')

    try:
        await ssh_manager.upload_file(user_id, alias, local_path, remote_path)
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
    user_id = update.effective_user.id

    command = "ps aux"
    result_message = await query.edit_message_text(f"Running `{command}` on `{alias}`...", parse_mode='Markdown')

    output = ""
    try:
        output = ""
        async for item, stream in ssh_manager.run_command(user_id, alias, command):
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
    user_id = update.effective_user.id

    command = f"kill -9 {pid}"

    result_message = await update.message.reply_text(f"Running `{command}` on `{alias}`...", parse_mode='Markdown')

    output = ""
    try:
        output = ""
        async for item, stream in ssh_manager.run_command(user_id, alias, command):
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
    user_id = update.effective_user.id

    command = "sudo ufw status verbose"
    output = ""
    try:
        output = ""
        async for item, stream in ssh_manager.run_command(user_id, alias, command):
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
    user_id = update.effective_user.id

    command = f"sudo ufw {action} {rule}"

    result_message = await update.message.reply_text(f"Running `{command}` on `{alias}`...", parse_mode='Markdown')

    output = ""
    try:
        output = ""
        async for item, stream in ssh_manager.run_command(user_id, alias, command):
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
    user_id = update.effective_user.id

    command = f"systemctl {action} {service_name}"

    result_message = await update.message.reply_text(f"Running `{command}` on `{alias}`...", parse_mode='Markdown')

    output = ""
    try:
        output = ""
        async for item, stream in ssh_manager.run_command(user_id, alias, command):
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
    user_id = update.effective_user.id

    if action == "reboot":
        command = "reboot"
    elif action == "shutdown":
        command = "shutdown now"
    else:
        return

    try:
        # We only need to start the command, not wait for output
        async for _, __ in ssh_manager.run_command(user_id, alias, f"sudo {command}"):
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
    user_id = update.effective_user.id

    command = "df -h"
    output = ""
    try:
        output = ""
        async for item, stream in ssh_manager.run_command(user_id, alias, command):
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
    user_id = update.effective_user.id

    command = "ip a"
    output = ""
    try:
        output = ""
        async for item, stream in ssh_manager.run_command(user_id, alias, command):
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
    user_id = update.effective_user.id

    command = "ss -tuln"
    output = ""
    try:
        output = ""
        async for item, stream in ssh_manager.run_command(user_id, alias, command):
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
        await ssh_manager.disconnect(user_id, alias)
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
    """
    Pro version: Creates a comprehensive backup with validation, metadata, and progress indication.
    Maximum speed and reliability.
    """
    query = update.callback_query
    user_id = update.effective_user.id
    parse_mode = _get_user_parse_mode(user_id)
    
    await query.answer()
    
    # Show progress message
    progress_msg = await query.message.reply_text("🔄 **Creating backup...**", parse_mode=parse_mode)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = tempfile.mkdtemp(prefix="tla_backup_")
    backup_filename = os.path.join(backup_dir, f"tla_backup_{timestamp}.zip")
    
    files_to_backup = [
        "config.json",
        "database.db",
        "var/encryption.key",
        "var/pq_encryption.key"
    ]
    backup_metadata = {
        "timestamp": timestamp,
        "version": "3.0",
        "bot_version": "Professional",
        "files": {},
        "checksums": {},
        "system_info": {
            "platform": sys.platform,
            "python_version": sys.version.split()[0]
        }
    }
    
    try:
        # Update progress
        builder = MessageBuilder(parse_mode)
        builder.add_text("📦 **Backup in Progress**")
        builder.add_line()
        builder.add_text("Validating files...")
        await progress_msg.edit_text(builder.build(), parse_mode=parse_mode)
        
        # Validate and collect files with metadata
        validated_files = []
        total_size = 0
        max_backup_size = 100 * 1024 * 1024  # 100MB limit
        critical_files = ["config.json", "database.db"]
        optional_files = ["var/encryption.key", "var/pq_encryption.key"]
        
        # Check critical files first
        for file_path in critical_files:
            if not os.path.exists(file_path):
                raise ValueError(f"Critical file missing: {file_path}")
        
        # Process all files
        for file_path in files_to_backup:
            if not os.path.exists(file_path):
                if file_path in optional_files:
                    logger.warning(f"Optional file {file_path} not found - skipping")
                    continue
                else:
                    raise ValueError(f"Required file missing: {file_path}")
            
            file_size = os.path.getsize(file_path)
            if file_size == 0:
                logger.warning(f"File {file_path} is empty - skipping")
                continue
            
            if file_size > max_backup_size:
                raise ValueError(f"File {file_path} is too large ({file_size} bytes). Maximum: {max_backup_size} bytes")
            
            total_size += file_size
            if total_size > max_backup_size:
                raise ValueError(f"Total backup size exceeds limit ({total_size} bytes). Maximum: {max_backup_size} bytes")
            
            # Calculate checksum for integrity verification (chunked for large files)
            file_hash = hashlib.sha256()
            with open(file_path, 'rb') as f:
                while chunk := f.read(8192):
                    file_hash.update(chunk)
            file_hash_hex = file_hash.hexdigest()
            
            backup_metadata["files"][file_path] = {
                "size": file_size,
                "modified": os.path.getmtime(file_path),
                "is_critical": file_path in critical_files
            }
            backup_metadata["checksums"][file_path] = file_hash_hex
            validated_files.append((file_path, file_hash_hex))
        
        if not validated_files:
            raise ValueError("No valid files found to backup")
        
        # Add summary to metadata
        backup_metadata["summary"] = {
            "total_files": len(validated_files),
            "total_size_bytes": total_size,
            "total_size_mb": round(total_size / (1024 * 1024), 2),
            "critical_files_count": sum(1 for f in validated_files if f[0] in critical_files)
        }
        
        # Update progress
        builder.clear()
        builder.add_text("📦 **Backup in Progress**")
        builder.add_line()
        builder.add_text(f"Compressing {len(validated_files)} file(s)...")
        await progress_msg.edit_text(builder.build(), parse_mode=parse_mode)
        
        # Create compressed backup with maximum compression
        with zipfile.ZipFile(backup_filename, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as zipf:
            for file_path, file_hash in validated_files:
                zipf.write(file_path, arcname=os.path.basename(file_path))
            
            # Add metadata as JSON
            metadata_json = json.dumps(backup_metadata, indent=2)
            zipf.writestr("backup_metadata.json", metadata_json)
        
        # Verify backup integrity
        backup_size = os.path.getsize(backup_filename)
        if backup_size == 0:
            raise ValueError("Backup file is empty")
        
        # Update progress
        builder.clear()
        builder.add_text("📦 **Backup in Progress**")
        builder.add_line()
        builder.add_text("Verifying backup integrity...")
        await progress_msg.edit_text(builder.build(), parse_mode=parse_mode)
        
        # Verify ZIP file integrity
        with zipfile.ZipFile(backup_filename, 'r') as zipf:
            if zipf.testzip() is not None:
                raise ValueError("Backup file integrity check failed")
        
        # Update progress
        builder.clear()
        builder.add_text("📦 **Backup in Progress**")
        builder.add_line()
        builder.add_text("Uploading backup...")
        await progress_msg.edit_text(builder.build(), parse_mode=parse_mode)
        
        # Send backup file
        with open(backup_filename, 'rb') as backup_file:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=backup_file,
                filename=f"tla_backup_{timestamp}.zip",
                caption=f"✅ Backup created successfully\n📅 {timestamp}\n📦 {len(validated_files)} file(s)\n💾 {backup_size / 1024:.2f} KB",
                parse_mode=parse_mode
            )
        
        # Success message
        keyboard = [[InlineKeyboardButton("🔙 Back to Main Menu", callback_data='main_menu')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        builder.clear()
        builder.add_text("✅ ")
        builder.add_bold("Backup Complete!")
        builder.add_line()
        builder.add_line()
        builder.add_text(f"📅 Created: {timestamp}")
        builder.add_line()
        builder.add_text(f"📦 Files: {len(validated_files)}")
        builder.add_line()
        builder.add_text(f"💾 Size: {backup_size / 1024:.2f} KB")
        builder.add_line()
        builder.add_text("🔒 Integrity: Verified")
        
        await progress_msg.edit_text(builder.build(), reply_markup=reply_markup, parse_mode=parse_mode)
        logger.info(f"Backup created successfully: {backup_filename} ({backup_size} bytes)")

    except Exception as e:
        logger.error(f"Error creating backup: {e}", exc_info=True)
        builder = MessageBuilder(parse_mode)
        builder.add_text("❌ ")
        builder.add_bold("Backup Failed")
        builder.add_line()
        builder.add_line()
        builder.add_text("Error: ")
        builder.add_code(str(e))
        await progress_msg.edit_text(builder.build(), parse_mode=parse_mode)
    finally:
        # Cleanup
        try:
            if os.path.exists(backup_filename):
                os.remove(backup_filename)
            if os.path.exists(backup_dir):
                shutil.rmtree(backup_dir, ignore_errors=True)
        except Exception as cleanup_error:
            logger.warning(f"Error during backup cleanup: {cleanup_error}")

# --- Restore (Pro Version) ---
@admin_authorized
async def restore_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Pro version: Starts the restore process with safety warnings."""
    query = update.callback_query
    user_id = update.effective_user.id
    parse_mode = _get_user_parse_mode(user_id)
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("⚠️ Yes, I'm sure", callback_data='restore_yes')],
        [InlineKeyboardButton("❌ Cancel", callback_data='restore_no')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    builder = MessageBuilder(parse_mode)
    builder.add_text("⚠️ ")
    builder.add_bold("DANGER ZONE: RESTORE")
    builder.add_line()
    builder.add_line()
    builder.add_text("Restoring from a backup will:")
    builder.add_line()
    builder.add_text("• Overwrite your current configuration")
    builder.add_line()
    builder.add_text("• Overwrite your current database")
    builder.add_line()
    builder.add_text("• Replace all server settings")
    builder.add_line()
    builder.add_text("⚠️ This action is ")
    builder.add_bold("IRREVERSIBLE")
    builder.add_text("!")
    builder.add_line()
    builder.add_line()
    builder.add_text("A backup of your current state will be created automatically.")
    builder.add_line()
    builder.add_text("Are you sure you want to continue?")
    
    await query.message.reply_text(builder.build(), reply_markup=reply_markup, parse_mode=parse_mode)
    return AWAIT_RESTORE_CONFIRMATION

async def restore_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Pro version: Confirms the restore and asks for the backup file."""
    query = update.callback_query
    user_id = update.effective_user.id
    parse_mode = _get_user_parse_mode(user_id)
    await query.answer()

    if query.data == 'restore_yes':
        builder = MessageBuilder(parse_mode)
        builder.add_text("📤 ")
        builder.add_bold("Ready to Restore")
        builder.add_line()
        builder.add_line()
        builder.add_text("Please upload the backup file (")
        builder.add_code(".zip")
        builder.add_text(").")
        builder.add_line()
        builder.add_line()
        builder.add_text("The file will be validated before restore.")
        await query.edit_message_text(builder.build(), parse_mode=parse_mode)
        return AWAIT_RESTORE_FILE
    else:
        builder = MessageBuilder(parse_mode)
        builder.add_text("❌ Restore cancelled.")
        await query.edit_message_text(builder.build(), parse_mode=parse_mode)
        return ConversationHandler.END

async def restore_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Pro version: Receives the backup file, validates it thoroughly, creates safety backup,
    and performs the restore with rollback capability.
    Maximum speed and reliability.
    """
    user_id = update.effective_user.id
    parse_mode = _get_user_parse_mode(user_id)
    
    document = update.message.document
    
    # Validate file extension
    if not document.file_name or not document.file_name.lower().endswith('.zip'):
        builder = MessageBuilder(parse_mode)
        builder.add_text("❌ ")
        builder.add_bold("Invalid File Format")
        builder.add_line()
        builder.add_line()
        builder.add_text("Please upload a ")
        builder.add_code(".zip")
        builder.add_text(" file.")
        await update.message.reply_text(builder.build(), parse_mode=parse_mode)
        return AWAIT_RESTORE_FILE
    
    # Check file size (max 100MB)
    max_file_size = 100 * 1024 * 1024
    if document.file_size and document.file_size > max_file_size:
        builder = MessageBuilder(parse_mode)
        builder.add_text("❌ ")
        builder.add_bold("File Too Large")
        builder.add_line()
        builder.add_line()
        builder.add_text(f"File size ({document.file_size / 1024 / 1024:.2f} MB) exceeds maximum (100 MB).")
        await update.message.reply_text(builder.build(), parse_mode=parse_mode)
        return AWAIT_RESTORE_FILE
    
    # Show progress
    progress_msg = await update.message.reply_text("🔄 **Starting restore process...**", parse_mode=parse_mode)
    
    backup_dir = tempfile.mkdtemp(prefix="tla_restore_")
    downloaded_file = os.path.join(backup_dir, document.file_name)
    safety_backup_dir = os.path.join(backup_dir, "safety_backup")
    os.makedirs(safety_backup_dir, exist_ok=True)
    
    try:
        # Step 1: Download file
        builder = MessageBuilder(parse_mode)
        builder.add_text("🔄 **Restore Progress**")
        builder.add_line()
        builder.add_text("📥 Downloading backup file...")
        await progress_msg.edit_text(builder.build(), parse_mode=parse_mode)
        
        # Download file with proper error handling
        backup_file = await document.get_file()
        try:
            await backup_file.download_to_drive(downloaded_file)
        except Exception as download_error:
            raise ValueError(f"Failed to download backup file: {download_error}")
        
        if not os.path.exists(downloaded_file) or os.path.getsize(downloaded_file) == 0:
            raise ValueError("Downloaded file is empty or corrupted")
        
        # Step 2: Validate ZIP file
        builder.clear()
        builder.add_text("🔄 **Restore Progress**")
        builder.add_line()
        builder.add_text("🔍 Validating backup file...")
        await progress_msg.edit_text(builder.build(), parse_mode=parse_mode)
        
        try:
            with zipfile.ZipFile(downloaded_file, 'r') as zipf:
                # Test ZIP integrity
                bad_file = zipf.testzip()
                if bad_file:
                    raise ValueError(f"Backup file is corrupted. Bad file: {bad_file}")
                
                # Check for required files
                namelist = zipf.namelist()
                required_files = ["config.json", "database.db"]
                missing_files = [f for f in required_files if f not in namelist]
                
                if missing_files:
                    raise ValueError(f"Backup file is missing required files: {', '.join(missing_files)}")
                
                # Check for metadata
                has_metadata = "backup_metadata.json" in namelist
                metadata = None
                if has_metadata:
                    try:
                        metadata_json = zipf.read("backup_metadata.json").decode('utf-8')
                        metadata = json.loads(metadata_json)
                    except Exception as e:
                        logger.warning(f"Could not read backup metadata: {e}")
        except zipfile.BadZipFile:
            raise ValueError("Invalid ZIP file format")
        
        # Step 3: Create safety backup of current state
        builder.clear()
        builder.add_text("🔄 **Restore Progress**")
        builder.add_line()
        builder.add_text("💾 Creating safety backup of current state...")
        await progress_msg.edit_text(builder.build(), parse_mode=parse_mode)
        
        safety_backup_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safety_backup_file = os.path.join(safety_backup_dir, f"safety_backup_{safety_backup_timestamp}.zip")
        
        current_files = ["config.json", "database.db"]
        with zipfile.ZipFile(safety_backup_file, 'w', zipfile.ZIP_DEFLATED) as safety_zip:
            for file_path in current_files:
                if os.path.exists(file_path):
                    safety_zip.write(file_path, arcname=os.path.basename(file_path))
        
        # Step 4: Extract backup files
        builder.clear()
        builder.add_text("🔄 **Restore Progress**")
        builder.add_line()
        builder.add_text("📦 Extracting backup files...")
        await progress_msg.edit_text(builder.build(), parse_mode=parse_mode)
        
        extract_dir = os.path.join(backup_dir, "extracted")
        os.makedirs(extract_dir, exist_ok=True)
        
        with zipfile.ZipFile(downloaded_file, 'r') as zipf:
            zipf.extractall(extract_dir)
        
        # Step 5: Verify extracted files
        builder.clear()
        builder.add_text("🔄 **Restore Progress**")
        builder.add_line()
        builder.add_text("✅ Verifying extracted files...")
        await progress_msg.edit_text(builder.build(), parse_mode=parse_mode)
        
        extracted_config = os.path.join(extract_dir, "config.json")
        extracted_db = os.path.join(extract_dir, "database.db")
        
        if not os.path.exists(extracted_config):
            raise ValueError("Extracted config.json not found")
        if not os.path.exists(extracted_db):
            raise ValueError("Extracted database.db not found")
        
        # Validate JSON structure
        try:
            with open(extracted_config, 'r', encoding='utf-8') as f:
                json.load(f)  # Validate JSON
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in config.json: {e}")
        
        # Step 6: Backup current files and restore
        builder.clear()
        builder.add_text("🔄 **Restore Progress**")
        builder.add_line()
        builder.add_text("🔄 Restoring files...")
        await progress_msg.edit_text(builder.build(), parse_mode=parse_mode)
        
        # Backup and replace files atomically
        files_to_restore = [
            ("config.json", extracted_config),
            ("database.db", extracted_db)
        ]
        
        for target_file, source_file in files_to_restore:
            # Backup current file if exists
            if os.path.exists(target_file):
                backup_path = os.path.join(safety_backup_dir, os.path.basename(target_file))
                shutil.copy2(target_file, backup_path)
            
            # Copy new file
            shutil.copy2(source_file, target_file)
            os.chmod(target_file, 0o600)  # Secure permissions
        
        # Step 7: Verify restore
        builder.clear()
        builder.add_text("🔄 **Restore Progress**")
        builder.add_line()
        builder.add_text("✅ Verifying restore...")
        await progress_msg.edit_text(builder.build(), parse_mode=parse_mode)
        
        # Verify files exist and are valid
        for target_file, _ in files_to_restore:
            if not os.path.exists(target_file):
                raise ValueError(f"Restored file {target_file} not found after restore")
            if os.path.getsize(target_file) == 0:
                raise ValueError(f"Restored file {target_file} is empty")
        
        # Success message
        builder.clear()
        builder.add_text("✅ ")
        builder.add_bold("Restore Successful!")
        builder.add_line()
        builder.add_line()
        if metadata:
            builder.add_text(f"📅 Backup Date: {metadata.get('timestamp', 'Unknown')}")
            builder.add_line()
        builder.add_text("✅ Files restored successfully")
        builder.add_line()
        builder.add_text("💾 Safety backup created")
        builder.add_line()
        builder.add_line()
        builder.add_text("🔄 The bot will restart to apply changes...")
        
        await progress_msg.edit_text(builder.build(), parse_mode=parse_mode)
        
        logger.info(f"Restore completed successfully. Safety backup: {safety_backup_file}")
        
        # Restart bot gracefully
        # Give time for message to be sent (modern async pattern)
        try:
            async with asyncio.timeout(2.0):
                await asyncio.sleep(2.0)
        except TimeoutError:
            pass
        
        # Use proper restart mechanism
        python = sys.executable
        os.execv(python, [python] + sys.argv)
        
    except Exception as e:
        logger.error(f"Error during restore: {e}", exc_info=True)
        
        # Try to restore from safety backup if available
        safety_backup_files = []
        if os.path.exists(safety_backup_dir):
            for file in os.listdir(safety_backup_dir):
                if file.endswith('.zip'):
                    safety_backup_files.append(os.path.join(safety_backup_dir, file))
        
        if safety_backup_files:
            try:
                logger.info("Attempting to restore from safety backup...")
                latest_backup = max(safety_backup_files, key=os.path.getmtime)
                with zipfile.ZipFile(latest_backup, 'r') as safety_zip:
                    safety_zip.extractall()
                logger.info("Safety backup restored successfully")
            except Exception as rollback_error:
                logger.error(f"Failed to restore from safety backup: {rollback_error}", exc_info=True)
        
        builder = MessageBuilder(parse_mode)
        builder.add_text("❌ ")
        builder.add_bold("Restore Failed")
        builder.add_line()
        builder.add_line()
        builder.add_text("Error: ")
        builder.add_code(str(e))
        builder.add_line()
        builder.add_line()
        if safety_backup_files:
            builder.add_text("⚠️ Attempted to restore from safety backup.")
        
        await progress_msg.edit_text(builder.build(), parse_mode=parse_mode)
        
    finally:
        # Cleanup temporary files (with delay to ensure files are closed)
        try:
            # Cleanup delay with timeout protection
            try:
                async with asyncio.timeout(1.0):
                    await asyncio.sleep(1.0)
            except TimeoutError:
                pass
            if os.path.exists(backup_dir):
                shutil.rmtree(backup_dir, ignore_errors=True)
        except Exception as cleanup_error:
            logger.warning(f"Error during restore cleanup: {cleanup_error}")

    return ConversationHandler.END

async def cancel_restore(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Pro version: Cancels the restore conversation."""
    user_id = update.effective_user.id
    parse_mode = _get_user_parse_mode(user_id)
    
    builder = MessageBuilder(parse_mode)
    builder.add_text("❌ Restore cancelled.")
    await update.message.reply_text(builder.build(), parse_mode=parse_mode)
    return ConversationHandler.END

async def remove_server_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Removes a server after confirmation."""
    query = update.callback_query
    await query.answer()

    alias = query.data.split('_', 1)[1]
    user_id = update.effective_user.id
    success = remove_server(user_id, alias)
    language = _get_user_language(user_id)
    if not success:
        await query.edit_message_text(
            translate('server_not_found', language, alias=alias),
            parse_mode='Markdown'
        )
        return

    await query.edit_message_text(
        translate('server_removed', language, alias=alias),
        parse_mode='Markdown'
    )

# --- Admin Dashboard ---
@admin_authorized
async def dashboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Pro version: Comprehensive admin dashboard showing:
    - Total users and new joins today
    - Total servers and servers added today
    - Plan distribution
    - Language distribution
    - Active users
    - Recent servers
    - Server statistics
    """
    user_id = update.effective_user.id
    parse_mode = _get_user_parse_mode(user_id)
    
    # Show loading message
    loading_msg = await update.message.reply_text("📊 **Loading dashboard...**", parse_mode=parse_mode)
    
    try:
        # Gather all statistics
        total_users = get_total_users()
        users_today = get_users_joined_today()
        total_servers = get_total_servers()
        servers_today = get_servers_added_today()
        active_users = get_active_users_count()
        plan_dist = get_plan_distribution()
        lang_dist = get_language_distribution()
        recent_servers = get_recent_servers(5)
        server_stats = get_servers_per_user_stats()
        
        # Build dashboard message
        builder = MessageBuilder(parse_mode)
        builder.add_text("📊 ")
        builder.add_bold("Admin Dashboard")
        builder.add_line()
        builder.add_line()
        
        # Users Section
        builder.add_text("👥 ")
        builder.add_bold("Users")
        builder.add_line()
        builder.add_text("Total Users: ")
        builder.add_bold(str(total_users))
        builder.add_line()
        builder.add_text("Active Users: ")
        builder.add_bold(str(active_users))
        builder.add_line()
        builder.add_text("New Today: ")
        builder.add_bold(str(users_today))
        builder.add_line()
        builder.add_line()
        
        # Servers Section
        builder.add_text("🖥️ ")
        builder.add_bold("Servers")
        builder.add_line()
        builder.add_text("Total Servers: ")
        builder.add_bold(str(total_servers))
        builder.add_line()
        builder.add_text("Added Today: ")
        builder.add_bold(str(servers_today))
        builder.add_line()
        builder.add_text("Avg per User: ")
        builder.add_bold(str(server_stats["avg"]))
        builder.add_text(" | Max: ")
        builder.add_bold(str(server_stats["max"]))
        builder.add_text(" | Min: ")
        builder.add_bold(str(server_stats["min"]))
        builder.add_line()
        builder.add_line()
        
        # Plan Distribution
        builder.add_text("💎 ")
        builder.add_bold("Plan Distribution")
        builder.add_line()
        if plan_dist:
            for plan, count in sorted(plan_dist.items()):
                builder.add_text(f"• {plan.capitalize()}: ")
                builder.add_bold(str(count))
                builder.add_line()
        else:
            builder.add_text("No data available")
            builder.add_line()
        builder.add_line()
        
        # Language Distribution
        builder.add_text("🌐 ")
        builder.add_bold("Language Distribution")
        builder.add_line()
        if lang_dist:
            # Show top 5 languages
            sorted_langs = sorted(lang_dist.items(), key=lambda x: x[1], reverse=True)[:5]
            for lang, count in sorted_langs:
                lang_label = get_language_label(lang)
                builder.add_text(f"• {lang_label}: ")
                builder.add_bold(str(count))
                builder.add_line()
        else:
            builder.add_text("No data available")
            builder.add_line()
        builder.add_line()
        
        # Recent Servers
        builder.add_text("🆕 ")
        builder.add_bold("Recent Servers")
        builder.add_line()
        if recent_servers:
            for server in recent_servers[:5]:
                builder.add_text("• ")
                builder.add_code(server["alias"])
                builder.add_text(" (")
                builder.add_code(str(server["owner_id"]))
                builder.add_text(")")
                if server.get("created_at"):
                    builder.add_text(" - ")
                    builder.add_text(str(server["created_at"])[:10])
                builder.add_line()
        else:
            builder.add_text("No recent servers")
            builder.add_line()
        
        # Weekly Statistics
        servers_week = get_servers_added_this_week()
        builder.add_line()
        builder.add_text("📈 ")
        builder.add_bold("Weekly Stats")
        builder.add_line()
        builder.add_text("Servers (7 days): ")
        builder.add_bold(str(servers_week))
        builder.add_line()
        builder.add_line()
        
        # Top Users
        top_users = get_top_users_by_servers(3)
        if top_users:
            builder.add_text("🏆 ")
            builder.add_bold("Top Users")
            builder.add_line()
            for idx, user in enumerate(top_users, 1):
                builder.add_text(f"{idx}. User ")
                builder.add_code(str(user["owner_id"]))
                builder.add_text(": ")
                builder.add_bold(str(user["server_count"]))
                builder.add_text(" servers")
                builder.add_line()
            builder.add_line()
        
        # System Health
        health = get_system_health()
        builder.add_text("💚 ")
        builder.add_bold("System Health")
        builder.add_line()
        if isinstance(health.get("cpu_percent"), (int, float)):
            builder.add_text("CPU: ")
            builder.add_bold(f"{health['cpu_percent']}%")
            builder.add_text(" | Memory: ")
            builder.add_bold(f"{health['memory_percent']}%")
            builder.add_line()
            builder.add_text("Disk: ")
            builder.add_bold(f"{health['disk_percent']}%")
            builder.add_text(" | Free: ")
            builder.add_bold(f"{health['disk_free_gb']} GB")
            builder.add_line()
        else:
            builder.add_text("System metrics unavailable")
            builder.add_line()
        builder.add_line()
        
        # Database Info
        db_size = get_database_size()
        builder.add_text("💾 ")
        builder.add_bold("Database")
        builder.add_line()
        builder.add_text("Size: ")
        builder.add_bold(f"{db_size['size_mb']} MB")
        builder.add_line()
        builder.add_line()
        
        # Footer
        builder.add_line()
        builder.add_text("📅 Last updated: ")
        builder.add_text(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        
        await loading_msg.edit_text(builder.build(), parse_mode=parse_mode)
        
    except Exception as e:
        logger.error(f"Error generating dashboard: {e}", exc_info=True)
        builder = MessageBuilder(parse_mode)
        builder.add_text("❌ ")
        builder.add_bold("Dashboard Error")
        builder.add_line()
        builder.add_line()
        builder.add_text("Error: ")
        builder.add_code(str(e))
        await loading_msg.edit_text(builder.build(), parse_mode=parse_mode)


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


def create_lock_file() -> None:
    """Creates a lock file to prevent multiple instances (2026 standards)."""
    try:
        LOCK_FILE.write_text(str(os.getpid()), encoding='utf-8')
        LOCK_FILE.chmod(0o600)  # Secure permissions
    except OSError as e:
        logger.error(f"Failed to create lock file: {e}")

def remove_lock_file() -> None:
    """Removes the lock file (2026 standards)."""
    try:
        if LOCK_FILE.exists():
            LOCK_FILE.unlink()
    except OSError as e:
        logger.warning(f"Failed to remove lock file: {e}")

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
    # Modern lock file handling (2026 standards)
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text(encoding='utf-8').strip())
            # Check if the process is running
            os.kill(pid, 0)
            logger.error(f"Lock file exists and process {pid} is running. Another instance of the bot is likely running.")
            sys.exit(1)
        except (OSError, ValueError, ProcessLookupError):
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
    # Configure request timeout to prevent ReadTimeout errors
    # Default timeout is 5 seconds, increasing to 30 seconds for better reliability
    application = (
        Application.builder()
        .token(config.telegram_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .build()
    )

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
    application.add_handler(CallbackQueryHandler(language_menu, pattern='^language_menu$'))
    application.add_handler(CallbackQueryHandler(set_language, pattern='^set_language_'))
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
    application.add_handler(CommandHandler('language', language_menu))
    application.add_handler(CommandHandler('debug', toggle_debug_mode))
    application.add_handler(CommandHandler('exit_shell', exit_shell))
    application.add_handler(CommandHandler('update_bot', update_bot_command))
    application.add_handler(CommandHandler('dashboard', dashboard_command))

    # --- Start Bot ---
    logger.info("Bot is starting...")

    # Special mode for CI/CD smoke test
    if os.environ.get("SMOKE_TEST"):
        logger.info("Smoke test mode enabled. Bot will not connect to Telegram.")
        # Modern async pattern - use asyncio.run for standalone execution
        import asyncio
        asyncio.run(asyncio.sleep(15))
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
