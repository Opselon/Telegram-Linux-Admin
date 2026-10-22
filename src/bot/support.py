"""Purpose: Shared handler helpers: identity, i18n, safe messaging, keyboards.

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

from . import state

import logging

from telegram import InlineKeyboardButton
from telegram import InlineKeyboardMarkup
from telegram import Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from ..access_control import permissions
from ..access_control import rate_limiter
from ..audit import audit
from ..localization import DEFAULT_LANGUAGE
from ..localization import SUPPORTED_LANGUAGES
from ..localization import get_language_label
from ..localization import translate
from ..metrics import METRICS
from ..parse_mode import escape_text
from ..ssh_manager import HostKeyMismatch
from dataclasses import field
from datetime import datetime
from functools import wraps
from typing import Any
from typing import TypedDict

logger = logging.getLogger(__name__)


class ServerInfo:
    """Immutable server information."""
    alias: str
    hostname: str
    user: str
    owner_id: int

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

# Structured logging helper (2026 standards)
def log_structured(level: int, message: str, **kwargs: Any) -> None:
    """Structured logging helper with context."""
    extra = {f"ctx_{k}": v for k, v in kwargs.items()}
    logger.log(level, message, extra=extra)

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
    """Enforces the configured access mode before any handler runs.

    The check is fail-closed: an unidentifiable user, an unknown mode or an
    empty whitelist in ``whitelist``/``admin_only`` mode all deny access.
    Every denial is audited and counted so incidents are visible.
    """
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = _extract_user_id(update)
        decision = permissions.check(user_id)
        if not decision.allowed:
            METRICS.inc("access_denied_total")
            audit.record(
                "access_denied",
                user_id=user_id,
                outcome="denied",
                reason=decision.reason,
                handler=getattr(func, "__name__", "unknown"),
            )
            logger.warning("Access denied for user %s: %s", user_id, decision.reason)
            language = state._get_user_language(user_id)
            message = translate("access_denied", language, reason=decision.reason)
            if update and update.callback_query:
                await update.callback_query.answer(message, show_alert=True)
            elif update and update.effective_chat:
                await _send_message_safely(update.effective_chat, message, user_id, preformatted=True)
            return

        METRICS.inc("access_allowed_total")
        await send_debug_message(update, f"Processing request for user_id: {user_id}...")
        return await func(update, context, *args, **kwargs)
    return wrapped

def rate_limit(scope: str, *, max_calls: int, period: float):
    """Sliding-window per-user rate limiting for PTB handlers.

    Denied calls never reach the handler, are answered with a localized
    wait time and are both audited and exposed via ``tla_rate_limited_total``.
    """
    def decorator(func):
        @wraps(func)
        async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
            user_id = _extract_user_id(update)
            allowed, retry_after = state.rate_limiter.check(
                user_id, scope, max_calls=max_calls, period=period
            )
            if not allowed:
                METRICS.inc("rate_limited_total", scope=scope)
                audit.record(
                    "rate_limited",
                    user_id=user_id,
                    outcome="denied",
                    scope=scope,
                    retry_after_seconds=round(retry_after, 2),
                )
                language = state._get_user_language(user_id)
                message = translate(
                    "rate_limited", language, seconds=max(1, int(retry_after) + 1)
                )
                if update and update.callback_query:
                    await update.callback_query.answer(message, show_alert=True)
                elif update and update.effective_message:
                    await _send_message_safely(
                        update.effective_message.chat, message, user_id, preformatted=True
                    )
                return
            return await func(update, context, *args, **kwargs)
        return wrapped
    return decorator

def admin_authorized(func):
    """Restricts a handler to the bot's administrator.

    The admin identity comes from the permission manager (the first entry of
    ``whitelisted_users``), so runtime changes to the whitelist take effect
    without a restart. Every denial is audited.
    """
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = _extract_user_id(update)
        if user_id is None or not permissions.is_admin(user_id):
            METRICS.inc("admin_access_denied_total")
            audit.record(
                "admin_access_denied",
                user_id=user_id,
                outcome="denied",
                handler=getattr(func, "__name__", "unknown"),
            )
            logger.warning("Admin access denied for user_id: %s", user_id)
            message = translate(
                "admin_only", state._get_user_language(user_id)
            )
            if update is not None and update.callback_query:
                await update.callback_query.answer(message, show_alert=True)
            elif update is not None and update.effective_chat:
                await _send_message_safely(
                    update.effective_chat, message, user_id, preformatted=True
                )
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

async def _answer_callback_query_safely(query: Update.callback_query):
    """Answers a callback query and handles any potential errors."""
    if query:
        try:
            await query.answer()
        except Exception as e:
            logger.warning(f"Failed to answer callback query: {e}")

async def _send_message_safely(
    chat,
    text: str,
    user_id: int | None = None,
    reply_markup=None,
    preformatted: bool = False,
    **kwargs
):
    """
    Sends a message with fail-safe rendering. Escapes text by default.
    Tries with a preferred parse mode first, then falls back to plain text if a
    BadRequest error occurs.
    """
    parse_mode = state._get_user_parse_mode(user_id)

    # If the text is not pre-formatted (i.e., not from MessageBuilder), escape it.
    text_to_send = text if preformatted else escape_text(text, parse_mode)

    try:
        return await chat.send_message(
            text_to_send,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            **kwargs
        )
    except BadRequest as e:
        if "Can't parse entities" in str(e):
            logger.warning(f"Parse error with mode '{parse_mode}'. Retrying with original text and no parse mode. Error: {e}")
            logger.debug(f"Original text: {text}")
            # Fallback to the original, unescaped text with no parsing
            return await chat.send_message(
                text,
                parse_mode=None,
                reply_markup=reply_markup,
                **kwargs
            )
        else:
            raise e

async def _edit_message_safely(
    message_or_query,
    text: str,
    user_id: int | None = None,
    reply_markup=None,
    preformatted: bool = False,
    **kwargs
):
    """
    Edits a message with fail-safe rendering. Escapes text by default.
    Tries with a preferred parse mode first, then falls back to plain text if a
    BadRequest error occurs.
    """
    parse_mode = state._get_user_parse_mode(user_id)

    edit_func = None
    if hasattr(message_or_query, 'edit_message_text'):
        edit_func = message_or_query.edit_message_text
    elif hasattr(message_or_query, 'edit_text'):
        edit_func = message_or_query.edit_text

    if not edit_func:
        logger.error("Attempted to edit an object with no edit function.")
        return

    # If the text is not pre-formatted (i.e., not from MessageBuilder), escape it.
    text_to_send = text if preformatted else escape_text(text, parse_mode)

    try:
        return await edit_func(
            text_to_send,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            **kwargs
        )
    except BadRequest as e:
        if "Message is not modified" in str(e):
            return  # This is a harmless, expected error.
        elif "Can't parse entities" in str(e):
            logger.warning(f"Parse error with mode '{parse_mode}'. Retrying with original text and no parse mode. Error: {e}")
            logger.debug(f"Original text: {text}")
            # Fallback to the original, unescaped text with no parsing
            return await edit_func(
                text,
                parse_mode=None,
                reply_markup=reply_markup,
                **kwargs
            )
        else:
            raise e

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

def _host_key_mismatch_message(alias: str, exc: HostKeyMismatch, language: str) -> str:
    """Explains a pinned-host-key mismatch without leaking anything else."""
    return translate(
        "host_key_mismatch",
        language,
        alias=alias,
        expected=exc.expected,
        actual=exc.actual,
    )

async def _send_connection_error(
    query: object,
    user_id: int | None,
    key: str,
    **kwargs: str,
) -> None:
    """Pro version: Sends a translated connection error message with language-aware parse mode."""
    error_message = state._translate_for_user(user_id, key, **kwargs)
    await _edit_message_safely(query, error_message, user_id)

# --- Debugging ---
async def send_debug_message(update: Update, text: str):
    """Sends a debug message to the user if debug mode is enabled."""
    if state.DEBUG_MODE:
        user_id = _extract_user_id(update)
        await _send_message_safely(update.effective_chat, f"🐞 **DEBUG:** {text}", user_id, preformatted=True)

async def toggle_debug_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggles debug mode on or off."""
    state.DEBUG_MODE = not state.DEBUG_MODE
    status = "ON" if state.DEBUG_MODE else "OFF"
    user_id = _extract_user_id(update)
    await _send_message_safely(update.message.chat, f"🐞 **Debug Mode is now {status}**", user_id, preformatted=True)
