"""PURPOSE: In-chat Security Center — the admin's single panel for every
access-control decision.

OWNER: src/bot/security_center.py (new in 3.5).
CONSUMES: src.access_control (permissions, rate_limiter), src.config,
  src.audit, src.bot.state, src.bot.support (auth decorators, safe
  send/edit helpers), src.bot.ui_kit (styled builders).
PROVIDES: the ``/security`` command and the ``sec_`` callback family:
  overview panel, access-mode switching, whitelist listing/adding/removal,
  configuration summary and audit tail.  All handlers are
  ``admin_authorized`` and every mutation is audited.
INVARIANTS:
  * Fail-closed: only the admin reaches any handler here; denials are
    audited and counted exactly like the rest of the codebase.
  * Mutations go through :class:`src.config.Config` so changes persist
    atomically (0600) and take effect immediately via
    ``permissions.update`` — no restart.
  * ``open`` mode is offered with a visible warning and requires a second
    confirmation tap; the callback namespace never re-uses existing wire
    formats (all payloads start with ``sec_``).
EXTEND: add a ``sec_<feature>`` callback here, register it in
  ``application.py`` ahead of the catch-all ``sec_`` route, and extend the
  overview keyboard in :func:`_overview_markup`.
"""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ..access_control import OPEN
from ..audit import audit
from ..localization import translate
from . import state
from .support import (
    _answer_callback_query_safely,
    _edit_message_safely,
    _extract_user_id,
    _resolve_message,
    _send_message_safely,
    admin_authorized,
    rate_limit,
)
from .ui_kit import ICONS, kv_panel, styled_menu

logger = logging.getLogger(__name__)

#: context.user_data key holding a pending whitelist-add while the admin
#: types the next message.
_AWAIT_ADD = "sec_await_whitelist_add"


def _overview_text(user_id: int | None) -> str:
    language = state._get_user_language(user_id)
    permissions = state.permissions
    cfg = state.config
    builder = kv_panel(
        f"{ICONS['security']} {translate('sec_title', language)}",
        [
            (translate("sec_mode_label", language), permissions.mode),
            (
                translate("sec_admin_label", language),
                str(permissions.admins[0]) if permissions.admins else "-",
            ),
            (translate("sec_whitelist_label", language), str(len(permissions.whitelisted))),
            (
                translate("sec_pin_label", language),
                translate("sec_pin_armed", language)
                if state.setup_enrollment.enabled
                else translate("sec_pin_off", language),
            ),
            (translate("sec_policy_label", language), cfg.destination_policy),
            (
                translate("sec_token_label", language),
                f"…{cfg.telegram_token[-4:]}"
                if cfg.telegram_token
                else translate("sec_token_missing", language),
            ),
        ],
    )
    return builder[0]


def _overview_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(f"{ICONS['ok']} admin_only", callback_data="sec_mode_admin_only"),
            InlineKeyboardButton(f"{ICONS['warn']} whitelist", callback_data="sec_mode_whitelist"),
        ],
        [
            InlineKeyboardButton(f"{ICONS['add']} Whitelist+", callback_data="sec_add_start"),
            InlineKeyboardButton(f"{ICONS['remove']} List", callback_data="sec_whitelist"),
        ],
        [
            InlineKeyboardButton(f"{ICONS['settings']} Config", callback_data="sec_config"),
            InlineKeyboardButton(f"{ICONS['security']} Audit", callback_data="sec_audit"),
        ],
        [InlineKeyboardButton(f"{ICONS['home']} Main Menu", callback_data="main_menu")],
    ]
    return InlineKeyboardMarkup(rows)


@rate_limit("security", max_calls=20, period=60)
@admin_authorized
async def security_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/security`` + ``sec_menu`` callback: the security overview panel."""
    user_id = _extract_user_id(update)
    language = state._get_user_language(user_id)
    text = _overview_text(user_id)
    markup = _overview_keyboard()

    if update.callback_query:
        await _answer_callback_query_safely(update.callback_query)
        await _edit_message_safely(
            update.callback_query.message,
            text,
            user_id,
            reply_markup=markup,
            preformatted=True,
        )
    else:
        message = _resolve_message(update)
        if message:
            await _send_message_safely(
                message.chat,
                text,
                user_id,
                reply_markup=markup,
                preformatted=True,
            )


@admin_authorized
async def set_access_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles ``sec_mode_<mode>`` switches; ``open`` needs a confirm tap."""
    user_id = _extract_user_id(update)
    language = state._get_user_language(user_id)
    query = update.callback_query
    if query is None or query.data is None:
        return
    requested = query.data.removeprefix("sec_mode_")

    user_data = context.user_data if context.user_data is not None else {}
    if requested == OPEN and not user_data.get("sec_open_confirmed"):
        user_data["sec_open_confirmed"] = True
        await _answer_callback_query_safely(query)
        await _edit_message_safely(
            query.message,
            f"{ICONS['warn']} {translate('sec_open_confirm', language)}",
            user_id,
            reply_markup=_overview_keyboard(),
            preformatted=True,
        )
        return
    user_data.pop("sec_open_confirmed", None)

    try:
        mode = state.config.set_access_mode(requested)
    except ValueError:
        mode = state.config.access_mode
    state.permissions.set_mode(mode)
    audit.record(
        "security.access_mode_changed",
        user_id=user_id,
        outcome="success",
        access_mode=mode,
    )
    await _answer_callback_query_safely(query)
    await _edit_message_safely(
        query.message,
        f"{_overview_text(user_id)}\n\n{ICONS['ok']} {translate('sec_mode_set', language, mode=mode)}",
        user_id,
        reply_markup=_overview_keyboard(),
        preformatted=True,
    )


@admin_authorized
async def whitelist_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``sec_whitelist``: lists whitelisted users with per-user remove buttons."""
    user_id = _extract_user_id(update)
    language = state._get_user_language(user_id)
    query = update.callback_query
    users = state.permissions.whitelisted
    items = [(f"🆔 {uid}", f"sec_remove_{uid}") for uid in users] or []
    if items:
        title = f"{ICONS['security']} {translate('sec_whitelist_title', language)}"
        text, markup = styled_menu(title, items, back="sec_menu")
        remove_hint = f"\n{ICONS['warn']} {translate('sec_whitelist_hint', language)}"
        text = text + remove_hint
    else:
        text = f"{ICONS['warn']} {translate('sec_whitelist_empty', language)}"
        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(f"{ICONS['add']} Add", callback_data="sec_add_start"),
                    InlineKeyboardButton(f"{ICONS['back']} Back", callback_data="sec_menu"),
                ]
            ]
        )

    if query:
        await _answer_callback_query_safely(query)
        await _edit_message_safely(
            query.message, text, user_id, reply_markup=markup, preformatted=True
        )


@admin_authorized
async def whitelist_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``sec_add_start``: arms capture; the next text message is added."""
    user_id = _extract_user_id(update)
    language = state._get_user_language(user_id)
    if context.user_data is not None:
        context.user_data[_AWAIT_ADD] = True
    query = update.callback_query
    text = f"{ICONS['add']} {translate('sec_add_prompt', language)}"
    markup = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("❌ Cancel", callback_data="sec_menu"),
            ]
        ]
    )
    if query:
        await _answer_callback_query_safely(query)
        await _edit_message_safely(
            query.message, text, user_id, reply_markup=markup, preformatted=True
        )
    else:
        message = _resolve_message(update)
        if message:
            await _send_message_safely(
                message.chat, text, user_id, reply_markup=markup, preformatted=True
            )


async def whitelist_add_capture(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Text handler that adds the captured message text as a whitelisted user.

    Deliberately not decorated with ``admin_authorized``: this handler sees
    *every* text message in the default group, so it no-ops silently unless
    this user armed the capture (only ``sec_add_start`` — itself
    admin-gated — can do that) and then double-checks admin rights itself.
    """
    if not context.user_data or not context.user_data.get(_AWAIT_ADD):
        return
    user_id = _extract_user_id(update)
    if user_id is None or not state.permissions.is_admin(user_id):
        return
    language = state._get_user_language(user_id)
    if context.user_data is not None:
        context.user_data.pop(_AWAIT_ADD, None)
    raw = (update.message.text or "").strip() if update.message else ""
    try:
        state.config.add_whitelisted_user(raw)
    except ValueError:
        if update.message is None:
            return
        await _send_message_safely(
            update.message.chat,
            f"{ICONS['denied']} {translate('sec_add_invalid', language)}",
            user_id,
            preformatted=True,
        )
        return
    state.permissions.update(
        whitelisted=state.config.whitelisted_users,
        admins=list(state.config.whitelisted_users[:1]),
    )
    audit.record(
        "security.whitelist_added",
        user_id=user_id,
        outcome="success",
        target=raw,
    )
    if update.message is None:
        return
    await _send_message_safely(
        update.message.chat,
        f"{ICONS['ok']} {translate('sec_add_done', language, uid=raw)}",
        user_id,
        preformatted=True,
    )


@admin_authorized
async def whitelist_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``sec_remove_<id>``: removes a whitelisted user (never the last admin)."""
    user_id = _extract_user_id(update)
    language = state._get_user_language(user_id)
    query = update.callback_query
    if query is None or query.data is None:
        return
    target_raw = query.data.removeprefix("sec_remove_")
    try:
        target = int(target_raw)
    except ValueError:
        await _answer_callback_query_safely(query)
        return
    if state.config.whitelisted_users == [target] or len(state.config.whitelisted_users) <= 1:
        await _answer_callback_query_safely(query)
        await _edit_message_safely(
            query.message,
            f"{ICONS['denied']} {translate('sec_remove_last_admin', language)}",
            user_id,
            reply_markup=_overview_keyboard(),
            preformatted=True,
        )
        return
    removed = state.config.remove_whitelisted_user(target)
    state.permissions.update(
        whitelisted=state.config.whitelisted_users,
        admins=list(state.config.whitelisted_users[:1]),
    )
    audit.record(
        "security.whitelist_removed",
        user_id=user_id,
        outcome="success" if removed else "failure",
        target=target,
    )
    key = "sec_remove_done" if removed else "sec_remove_missing"
    await _answer_callback_query_safely(query)
    await _edit_message_safely(
        query.message,
        f"{ICONS['ok'] if removed else ICONS['warn']} {translate(key, language, uid=target)}",
        user_id,
        reply_markup=_overview_keyboard(),
        preformatted=True,
    )


@admin_authorized
async def config_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``sec_config``: shows the non-secret configuration surface."""
    user_id = _extract_user_id(update)
    language = state._get_user_language(user_id)
    cfg = state.config
    text, _markup = kv_panel(
        f"{ICONS['settings']} {translate('sec_config_title', language)}",
        [
            ("access_mode", cfg.access_mode),
            ("destination_policy", cfg.destination_policy),
            ("allowed_destinations", ",".join(cfg.allowed_destinations) or "-"),
            ("whitelisted", ",".join(str(u) for u in cfg.whitelisted_users) or "-"),
            ("config_file", str(cfg.path)),
            ("telegram_token", f"…{cfg.telegram_token[-4:]}" if cfg.telegram_token else "-"),
            (
                "setup_pin",
                translate("sec_pin_armed", language) if getattr(cfg, "setup_pin", "") else "-",
            ),
        ],
    )
    query = update.callback_query
    if query:
        await _answer_callback_query_safely(query)
        await _edit_message_safely(
            query.message,
            text,
            user_id,
            reply_markup=_overview_keyboard(),
            preformatted=True,
        )


@admin_authorized
async def audit_tail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``sec_audit``: last 10 security events inside the panel."""
    user_id = _extract_user_id(update)
    language = state._get_user_language(user_id)
    records = state.audit.tail(10)
    if not records:
        body = translate("audit_empty", language)
    else:
        body = "\n".join(
            "`{ts}` {event} {outcome}".format(
                ts=str(record.get("timestamp", ""))[11:19],
                event=record.get("event", "?"),
                outcome=record.get("outcome", "-"),
            )
            for record in records
        )
    text = f"{ICONS['security']} {translate('audit_title', language, count=len(records))}\n{body}"
    query = update.callback_query
    if query:
        await _answer_callback_query_safely(query)
        await _edit_message_safely(
            query.message,
            text,
            user_id,
            reply_markup=_overview_keyboard(),
            preformatted=True,
        )
