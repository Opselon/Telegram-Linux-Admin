"""Purpose: Server CRUD conversations and connection handling.

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
import asyncssh

from ..audit import audit
from ..database import add_server
from ..database import get_all_servers
from ..database import get_user_server_count
from ..database import get_user_server_limit
from ..database import remove_server
from ..destination_policy import DESTINATIONS
from ..destination_policy import DestinationDenied
from ..localization import translate
from ..metrics import METRICS
from ..ssh_manager import HostKeyMismatch
from ..validators import validate_alias
from ..validators import validate_hostname
from ..validators import validate_key_path
from ..validators import validate_username
import asyncio
import re
import socket

from . import state
from .state import (
    ALIAS, AUTH_METHOD, HOSTNAME, KEY_PATH, PASSWORD, USER, ssh_manager, user_connections,
)

from .navigation import main_menu
from .support import _edit_message_safely, _extract_user_id, _host_key_mismatch_message, _send_connection_error, _send_message_safely

logger = logging.getLogger(__name__)


async def add_server_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the conversation to add a new server."""
    user_id = _extract_user_id(update)
    language = state._get_user_language(user_id)
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
            await _send_message_safely(update.effective_message.chat, message, user_id)
        return ConversationHandler.END

    prompt = translate('add_server_prompt', language)

    if update.callback_query:
        await update.callback_query.answer()
        await _edit_message_safely(update.callback_query.message, prompt, user_id)
    else:
        await _send_message_safely(update.effective_message.chat, prompt, user_id)

    return ALIAS

async def get_alias(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = _extract_user_id(update)
    language = state._get_user_language(user_id)
    context.user_data['alias'] = update.message.text
    await _send_message_safely(update.message.chat, translate('prompt_hostname', language), user_id)
    return HOSTNAME

async def get_hostname(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = _extract_user_id(update)
    language = state._get_user_language(user_id)
    context.user_data['hostname'] = update.message.text
    await _send_message_safely(update.message.chat, translate('prompt_username', language), user_id)
    return USER

async def get_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = _extract_user_id(update)
    language = state._get_user_language(user_id)
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
    await _send_message_safely(update.message.chat, translate('prompt_auth_method', language), user_id, reply_markup=reply_markup)
    return AUTH_METHOD

async def auth_method_invalid_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles invalid text input when expecting a button press for auth method."""
    user_id = _extract_user_id(update)
    language = state._get_user_language(user_id)
    await _send_message_safely(update.message.chat, translate('error_invalid_auth_method_input', language), user_id)
    # Re-ask the question without ending the conversation
    return AUTH_METHOD

async def get_auth_method(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user_id = _extract_user_id(update)
    language = state._get_user_language(user_id)
    await query.answer()

    if query.data == 'password':
        await _send_message_safely(query.message.chat, translate('prompt_password', language), user_id)
        return PASSWORD
    else:
        await _send_message_safely(query.message.chat, translate('prompt_key_path', language), user_id)
        return KEY_PATH

async def get_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles receiving the password, saves the server, and ends the conversation."""
    context.user_data['password'] = update.message.text
    await save_server(update, context)
    return ConversationHandler.END

async def get_key_path(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles receiving the key path, saves the server, and ends the conversation."""
    context.user_data['key_path'] = update.message.text
    await save_server(update, context)
    return ConversationHandler.END

async def save_server(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Saves the server to the database. It sends a confirmation or error message
    but does not manage the conversation state, leaving that to the handlers.
    """
    user_id = _extract_user_id(update)
    language = state._get_user_language(user_id)
    owner_id = update.effective_user.id
    try:
        alias = validate_alias(context.user_data.get('alias', ''))
        hostname = validate_hostname(context.user_data.get('hostname', ''))
        username = validate_username(context.user_data.get('user', ''))
        key_path = context.user_data.get('key_path')
        if key_path:
            key_path = validate_key_path(key_path)

        decision = DESTINATIONS.evaluate(hostname)
        if not decision.allowed:
            METRICS.inc("destination_denied_total", reason=decision.reason)
            audit.record(
                "server.add_denied",
                user_id=owner_id,
                alias=alias,
                outcome="denied",
                hostname=hostname,
                reason=decision.reason,
            )
            await _send_message_safely(
                update.message.chat,
                translate(
                    "destination_denied",
                    language,
                    hostname=hostname,
                    reason=decision.reason,
                ),
                user_id,
                preformatted=True,
            )
            return

        add_server(
            owner_id,
            alias=alias,
            hostname=hostname,
            user=username,
            password=context.user_data.get('password'),
            key_path=key_path
        )
        METRICS.inc("servers_added_total")
        audit.record("server.added", user_id=owner_id, alias=alias, hostname=hostname)
        confirmation = translate('server_added', language, alias=alias)
        await _send_message_safely(update.message.chat, confirmation, user_id)
    except Exception as e:
        audit.record("server.add_failed", user_id=owner_id, outcome="error", error=str(e)[:200])
        await _send_message_safely(
            update.message.chat,
            translate('server_add_error', language, error=str(e)),
            user_id
        )

async def cancel_add_server(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the add server conversation."""
    user_id = _extract_user_id(update)
    language = state._get_user_language(user_id)
    await _send_message_safely(update.message.chat, translate('server_add_cancelled', language), user_id)
    context.user_data.clear()
    return ConversationHandler.END

async def remove_server_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays a menu of servers to remove."""
    user_id = _extract_user_id(update)
    language = state._get_user_language(user_id)
    servers = get_all_servers(user_id)
    if not servers:
        message = translate('no_servers_to_remove', language)
        if update.callback_query:
            await update.callback_query.answer()
            await _edit_message_safely(update.callback_query.message, message, user_id)
        else:
            await _send_message_safely(update.effective_chat, message, user_id)
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
    text = '🗑️ **Select a server to remove:**'
    if update.callback_query:
        await update.callback_query.answer()
        await _edit_message_safely(update.callback_query.message, text, user_id, reply_markup=reply_markup, preformatted=True)
    else:
        await _send_message_safely(update.effective_chat, text, user_id, reply_markup=reply_markup, preformatted=True)

async def remove_server_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Removes a server after confirmation."""
    query = update.callback_query
    await query.answer()

    alias = query.data.split('_', 1)[1]
    user_id = update.effective_user.id
    success = remove_server(user_id, alias)
    audit.record(
        "server.removed" if success else "server.remove_failed",
        user_id=user_id,
        alias=alias,
        outcome="success" if success else "not_found",
    )
    for owner, active_alias in list(state.user_connections.items()):
        if owner == user_id and active_alias == alias:
            state.user_connections.pop(owner, None)
    language = state._get_user_language(user_id)
    if not success:
        await _edit_message_safely(
            query.message,
            translate('server_not_found', language, alias=alias),
            user_id
        )
        return

    await _edit_message_safely(
        query.message,
        translate('server_removed', language, alias=alias),
        user_id
    )

async def connect_server_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays a menu of servers to connect to."""
    logger.info("Displaying connect server menu.")
    user_id = _extract_user_id(update)
    language = state._get_user_language(user_id)
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
    await _edit_message_safely(
        update.callback_query.message,
        translate('connect_menu_prompt', language),
        user_id,
        reply_markup=reply_markup
    )

async def handle_server_connection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Connects to a stored server after alias, policy and host key checks."""
    query = update.callback_query
    
    user_id = update.effective_user.id

    # استخراج اطلاعات
    try:
        raw_alias = query.data.split('_', 1)[1]
    except IndexError:
        await query.answer("❌ Invalid request data.", show_alert=True)
        return

    # Strict input validation: aliases are validated at add time, so any other
    # callback payload is an attempt to poke the bot.
    alias = raw_alias.strip()
    if not re.match(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$', alias):
        logger.warning("SECURITY ALERT: User %s tried malicious alias: %r", user_id, raw_alias)
        audit.record("connect.invalid_alias", user_id=user_id, outcome="denied", alias=raw_alias[:64])
        await query.answer("🚫 Security Violation: Invalid alias format.", show_alert=True)
        return

    language = state._get_user_language(user_id)
    server = await asyncio.to_thread(state.get_server_record, user_id, alias)
    if not server:
        await query.answer(translate('server_not_found', language, alias=alias), show_alert=True)
        return

    decision = DESTINATIONS.evaluate(server.get("hostname", ""))
    if not decision.allowed:
        METRICS.inc("destination_denied_total", reason=decision.reason)
        audit.record(
            "connect.destination_denied",
            user_id=user_id,
            alias=alias,
            outcome="denied",
            hostname=server.get("hostname"),
            reason=decision.reason,
        )
        await query.answer(
            translate(
                "destination_denied",
                language,
                hostname=server.get("hostname", alias),
                reason=decision.reason,
            ),
            show_alert=True,
        )
        return

    await query.answer()

    try:
        await _edit_message_safely(query.message, translate('connecting_to_server', language, alias=alias), user_id)

        # 3. SECURITY: Hard Timeout (جلوگیری از فریز شدن ربات)
        # اتصال نباید بیشتر از ۱۰ ثانیه طول بکشه.
        async def _connect_with_timeout():
            async for _, __ in state.ssh_manager.run_command(user_id, alias, "echo 'Secure Handshake'"):
                pass

        # Modern async timeout (Python 3.11+)
        try:
            async with asyncio.timeout(10.0):
                await _connect_with_timeout()
        except TimeoutError:
            logger.warning(f"Connection timeout for {alias} (User: {user_id})")
            await _edit_message_safely(query.message, translate('error_connection_refused', language, alias=alias), user_id)
            return

        # ثبت موفقیت
        state.user_connections[user_id] = alias
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
        
        text = translate('connected_to_server', language, alias=alias)
        await _edit_message_safely(query.message, text, user_id, reply_markup=InlineKeyboardMarkup(keyboard))

    # --- Secure Error Handling ---
    # ارورها رو کامل لاگ می‌کنیم ولی به کاربر جزئیات فنی نمیدیم
    except HostKeyMismatch as exc:
        METRICS.inc("ssh_host_key_mismatch_total")
        audit.record(
            "connect.host_key_mismatch",
            user_id=user_id,
            alias=alias,
            outcome="denied",
            expected=exc.expected,
            presented=exc.actual,
        )
        await _edit_message_safely(
            query.message,
            _host_key_mismatch_message(alias, exc, language),
            user_id,
            preformatted=True,
        )
    except DestinationDenied as exc:
        METRICS.inc("destination_denied_total", reason=exc.reason)
        audit.record(
            "connect.destination_denied",
            user_id=user_id,
            alias=alias,
            outcome="denied",
            reason=exc.reason,
        )
        await query.answer(
            translate("destination_denied", language, hostname=exc.hostname, reason=exc.reason),
            show_alert=True,
        )
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

async def disconnect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Disconnects the user from the server."""
    query = update.callback_query
    await query.answer()
    alias = query.data.split('_', 1)[1]
    user_id = update.effective_user.id

    try:
        await state.ssh_manager.disconnect(user_id, alias)
        if user_id in state.user_connections:
            del state.user_connections[user_id]

        logger.info(f"User {user_id} disconnected from {alias}.")
        await _edit_message_safely(query.message, "🔌 **Disconnected successfully.**", user_id, preformatted=True)
        await main_menu(update, context)

    except Exception as e:
        logger.error(f"Error disconnecting from {alias} for user {user_id}: {e}", exc_info=True)
        await _edit_message_safely(query.message, f"❌ **Error:** Could not disconnect.\n`{e}`", user_id, preformatted=True)
