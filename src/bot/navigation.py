"""Purpose: Entry points, main menu and language selection.

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
from telegram.ext import ContextTypes

from ..config import config
from ..database import set_user_language_preference
from ..localization import get_language_label
from ..localization import translate
from ..parse_mode import MessageBuilder

from .support import _build_language_keyboard, _edit_message_safely, _extract_user_id, _resolve_message, _send_message_safely

logger = logging.getLogger(__name__)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends the main menu."""
    user_id = update.effective_user.id
    logger.info(f"User {user_id} initiated /start command.")
    await main_menu(update, context)

async def main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays the main menu with options, dynamically showing admin buttons."""
    user_id = _extract_user_id(update)
    logger.info(f"Displaying main menu for user_id: {user_id}.")
    language = state._get_user_language(user_id)

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

    # Pro version: Use MessageBuilder for safe, rich text formatting
    parse_mode = state._get_user_parse_mode(user_id)
    builder = MessageBuilder(parse_mode)
    builder.add_bold("🐧 Welcome to your Linux Admin Bot!")
    builder.add_line()
    builder.add_line()
    builder.add_text("What would you like to do?")

    menu_text = builder.build()

    if update.callback_query:
        await update.callback_query.answer()
        await _edit_message_safely(update.callback_query.message, menu_text, user_id, reply_markup=reply_markup, preformatted=True)
    else:
        await _send_message_safely(update.message.chat, menu_text, user_id, reply_markup=reply_markup, preformatted=True)

async def language_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays or handles the language selection menu."""
    user_id = _extract_user_id(update)
    language = state._get_user_language(user_id)
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
        await _edit_message_safely(update.callback_query.message, text, user_id, reply_markup=reply_markup)
    elif message:
        await _send_message_safely(message.chat, text, user_id, reply_markup=reply_markup)

async def set_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Saves the selected language preference for the current user."""
    query = update.callback_query
    user_id = update.effective_user.id
    lang_code = query.data.split('_', 2)[2]

    if lang_code not in state.SUPPORTED_LANGUAGE_SET:
        await query.answer(state._translate_for_user(user_id, 'language_unsupported'), show_alert=True)
        return

    set_user_language_preference(user_id, lang_code)
    state.user_language_cache[user_id] = lang_code
    await query.answer(translate('language_saved_toast', lang_code), show_alert=False)

    title = translate('language_updated', lang_code, language_name=get_language_label(lang_code))
    description = translate(
        'language_menu_description',
        lang_code,
        language_name=get_language_label(lang_code)
    )
    text = f"{title}\n\n{description}"
    reply_markup = _build_language_keyboard(lang_code)
    await _edit_message_safely(query.message, text, user_id, reply_markup=reply_markup)
