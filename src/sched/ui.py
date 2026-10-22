"""Purpose: admin-only Telegram surface for scheduled backups (/backups).

OWNER: lane `backup` of wave 3.4 (branch feat/3.4-backup), package src/sched/.
CONSUMES: ``src.sched.storage`` (plan CRUD), ``src.sched.runner`` (one run),
  ``src.sched.models``, ``src.bot.state`` (user language, the 3.3 singleton
  pattern), ``src.bot.support`` (authorization + safe send/edit),
  ``src.bot.ui_kit.menu``, ``src.localization.translate``, ``src.metrics``.
PROVIDES: the handlers ``src/bot/application.py`` registers (orchestrator
  wiring, not this lane):

    /backups            CommandHandler  → backups_menu
    backups_menu        CallbackQueryHandler pattern '^backups_menu$'
    sched_toggle        CallbackQueryHandler pattern '^schedtoggle_'
    sched_delete        CallbackQueryHandler pattern '^scheddel_'
    sched_run           CallbackQueryHandler pattern '^schedrun_'
    sched_add_start     CallbackQueryHandler pattern '^schedadd$'
    sched_add_alias     CallbackQueryHandler pattern '^schedadd_alias_'
    sched_add_hour      CallbackQueryHandler pattern '^schedadd_hour_'
    sched_add_keep      CallbackQueryHandler pattern '^schedadd_keep_'
    sched_add_confirm   CallbackQueryHandler pattern '^schedadd_[^_]+_[0-9]+_[0-9]+$'

INVARIANTS
  * admin-only: every entry point is wrapped in ``admin_authorized``, so a
    non-admin gets the localized ``admin_only`` message and no state change
    (audited by the decorator in src/bot/support.py);
  * every user-visible string goes through ``translate()`` (keys are en-only
    additions at the END of the en dict per CONTRACT lane rules) — button
    labels are numeric-only where they show an hour;
  * every mutation is ownership-checked (``plan.owner_id == user_id``);
    a foreign or stale id renders ``backups_plan_not_found``, never an error;
  * a missing DB table cannot crash a handler: storage returns empty/False and
    the panel degrades to the empty state (hard law L5);
  * callbacks are answered before any edit (no Telegram "query too old");
  * "Run now" uses :func:`src.sched.runner.run_backup` with the injected
    ``_run_cmd`` port; when the orchestrator has not set it, the press reports
    the localized failure and counts ``sched_backup_total{status=retrieval_failed}``
    instead of raising.
EXTEND: new step → new ``sched*`` prefix documented above, and register it in
  src/bot/application.py (orchestrator).
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from telegram import CallbackQuery
from telegram import InlineKeyboardButton
from telegram import InlineKeyboardMarkup
from telegram import Update
from telegram.ext import ContextTypes

from .. import database
from ..localization import translate
from ..metrics import METRICS
from . import storage
from .models import RETRIEVAL_FAILED, BackupPlan
from .runner import RunCmd, run_backup
from ..bot import state, ui_kit
from ..bot.support import (
    _edit_message_safely,
    _extract_user_id,
    _send_message_safely,
    admin_authorized,
)

logger = logging.getLogger(__name__)

MENU_CALLBACK = "backups_menu"
TOGGLE_PREFIX = "schedtoggle_"
DELETE_PREFIX = "scheddel_"
RUN_PREFIX = "schedrun_"
ADD_CALLBACK = "schedadd"
ADD_ALIAS_PREFIX = "schedadd_alias_"
ADD_HOUR_PREFIX = "schedadd_hour_"
ADD_KEEP_PREFIX = "schedadd_keep_"
CONFIRM_PREFIX = "schedadd_"

#: Hour choices offered by the UI: 00, 02, ... 22 (one row of twelve).
HOUR_CHOICES = tuple(range(0, 24, 2))
#: Retention choices offered by the UI.
KEEP_CHOICES = (3, 7, 14, 30)
#: Hour buttons per keyboard row (12 choices → two rows of six).
HOURS_PER_ROW = 6
#: Plans shown per panel before the list is truncated (ui_kit.menu clamp).
MAX_PLANS_SHOWN = 8
#: Server aliases offered in the add flow (ui_kit.menu clamp).
MAX_ALIASES_SHOWN = 8

#: The injected archive producer ("public entry" port — see runner docstring).
#: The orchestrator sets it once at startup with the same function the /backup
#: handler uses.  It lives here rather than in src/bot/state.py because that
#: module is outside this lane's file ownership.
_run_cmd: RunCmd | None = None

#: Optional rotation root override (orchestrator/tests); ``None`` uses the
#: runner's ``data/backups/scheduled`` default.
_backup_base: Path | None = None


def set_run_cmd(run_cmd: RunCmd | None) -> None:
    """Installs the archive producer used by *Run now* (orchestrator wiring)."""
    global _run_cmd
    _run_cmd = run_cmd


def set_backup_base(base: Path | None) -> None:
    """Points the rotation root somewhere other than ``data/backups/scheduled``."""
    global _backup_base
    _backup_base = base


def get_backup_base() -> Path | None:
    """The configured rotation root (``None`` = the runner's default)."""
    return _backup_base


def get_run_cmd() -> RunCmd | None:
    """The currently installed archive producer (``None`` until wired)."""
    return _run_cmd


# -- rendering ---------------------------------------------------------------


def _plan_rows(plans: list[BackupPlan]) -> list[list[InlineKeyboardButton]]:
    """One action row per plan: toggle, delete, run now, plus its own line."""
    rows: list[list[InlineKeyboardButton]] = []
    for plan in plans:
        mark = "✅" if plan.enabled else "⏸️"
        rows.append(
            [
                InlineKeyboardButton(
                    f"{mark} {translate('backups_toggle')}",
                    callback_data=f"{TOGGLE_PREFIX}{plan.id}",
                ),
                InlineKeyboardButton(
                    f"🗑️ {translate('backups_delete')}",
                    callback_data=f"{DELETE_PREFIX}{plan.id}",
                ),
                InlineKeyboardButton(
                    f"▶️ {translate('backups_run_now')}",
                    callback_data=f"{RUN_PREFIX}{plan.id}",
                ),
            ]
        )
    return rows


def _plan_line(plan: BackupPlan) -> str:
    """One human-readable line per plan; every word routes through translate()."""
    return translate(
        "backups_panel_plan",
        enabled="✅" if plan.enabled else "⏸️",
        alias=plan.alias,
        hour=plan.hour,
        keep=plan.keep,
    )


def build_panel(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Renders the /backups panel for one admin. Pure apart from the DB read."""
    language = state._get_user_language(user_id)
    plans = storage.list_plans(user_id)
    title = translate("backups_panel_title", language)
    text = "\n".join([title] + [_plan_line(plan) for plan in plans[:MAX_PLANS_SHOWN]])
    if not plans:
        text = f"{title}\n{translate('backups_empty', language)}"

    rows = _plan_rows(plans[:MAX_PLANS_SHOWN])
    rows.append(
        [InlineKeyboardButton(translate("backups_add", language), callback_data=ADD_CALLBACK)]
    )
    return text, InlineKeyboardMarkup(rows)


async def _render(update: Update, text: str, reply_markup=None) -> None:
    """Edits in place for a callback, sends for a command. Never raises."""
    user_id = _extract_user_id(update)
    query = update.callback_query
    try:
        if query is not None:
            await _edit_message_safely(query, text, user_id, reply_markup=reply_markup)
        elif update.effective_chat is not None:
            await _send_message_safely(
                update.effective_chat, text, user_id, reply_markup=reply_markup, preformatted=True
            )
    except Exception:  # pragma: no cover - a stale panel must never raise
        logger.warning("scheduled-backup panel render failed", exc_info=True)


async def _render_panel(update: Update, toast: str | None = None) -> None:
    """Re-renders the panel, optionally prefixed with a toast line."""
    user_id = _extract_user_id(update) or 0
    text, markup = build_panel(user_id)
    await _render(update, f"{toast}\n\n{text}" if toast else text, markup)


def _parse_int(text: str) -> int | None:
    """Parses a callback id fragment; ``None`` when it is not a clean int."""
    return int(text) if text.isdigit() else None


def _pressed(update: Update) -> tuple[CallbackQuery, int, str] | None:
    """The pressing query, its user id and its data — non-optional for mypy.

    Returns ``None`` when the update carries no callback query (a stray
    message reaching a callback handler): the caller answers nothing and
    returns, which is what PTB does for an unmatched update.
    """
    query = update.callback_query
    if query is None:
        return None
    user_id = _extract_user_id(update)
    return query, user_id if user_id is not None else 0, query.data or ""


def _parse_plan_payload(payload: str) -> tuple[str, int | None, int | None]:
    """Splits ``<alias>_<hour>_<keep>`` from the right (aliases may hold ``_``)."""
    parts = payload.rsplit("_", 2)
    if len(parts) != 3:
        return payload, None, None
    alias, hour_text, keep_text = parts
    return alias, _parse_int(hour_text), _parse_int(keep_text)


# -- handlers ---------------------------------------------------------------


@admin_authorized
async def backups_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/backups`` and the ``backups_menu`` button: the plan list panel."""
    if update.callback_query is not None:
        await update.callback_query.answer()
    await _render_panel(update)


@admin_authorized
async def sched_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``schedtoggle_<id>``: flips one of the admin's plans on/off."""
    pressed = _pressed(update)
    if pressed is None:
        return
    query, user_id, data = pressed
    language = state._get_user_language(user_id)
    await query.answer()
    plan_id = _parse_int(data[len(TOGGLE_PREFIX):])
    plan = storage.get_plan(plan_id, user_id) if plan_id is not None else None
    if plan is None:
        METRICS.inc("sched_plan_toggle_total", outcome="miss")
        await _render_panel(update, translate("backups_plan_not_found", language))
        return
    updated = BackupPlan(
        alias=plan.alias,
        hour=plan.hour,
        keep=plan.keep,
        enabled=not plan.enabled,
        owner_id=plan.owner_id,
        id=plan.id,
    )
    saved = storage.set_plan(updated)
    METRICS.inc("sched_plan_toggle_total", outcome="ok" if saved else "miss")
    toast = translate(
        "backups_plan_toggled_on" if updated.enabled else "backups_plan_toggled_off", language
    )
    await _render_panel(update, toast if saved else translate("backups_plan_not_found", language))


@admin_authorized
async def sched_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``scheddel_<id>``: deletes one of the admin's plans."""
    pressed = _pressed(update)
    if pressed is None:
        return
    query, user_id, data = pressed
    language = state._get_user_language(user_id)
    await query.answer()
    plan_id = _parse_int(data[len(DELETE_PREFIX):])
    deleted = storage.delete_plan(plan_id, user_id) if plan_id is not None else False
    METRICS.inc("sched_plan_delete_total", outcome="ok" if deleted else "miss")
    toast = translate(
        "backups_plan_deleted" if deleted else "backups_plan_not_found", language
    )
    await _render_panel(update, toast)


@admin_authorized
async def sched_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``schedrun_<id>``: runs one plan immediately through the shared runner."""
    pressed = _pressed(update)
    if pressed is None:
        return
    query, user_id, data = pressed
    language = state._get_user_language(user_id)
    await query.answer()
    plan_id = _parse_int(data[len(RUN_PREFIX):])
    plan = storage.get_plan(plan_id, user_id) if plan_id is not None else None
    if plan is None:
        await _render_panel(update, translate("backups_plan_not_found", language))
        return

    await _render(update, translate("backups_running", language, alias=plan.alias))
    run_cmd = get_run_cmd()
    if run_cmd is None:
        logger.warning("run-now pressed but no archive producer is wired")
        METRICS.inc("sched_backup_total", status=RETRIEVAL_FAILED)
        await _render_panel(
            update,
            translate(
                "backups_run_failed", language, alias=plan.alias, status=RETRIEVAL_FAILED
            ),
        )
        return

    run = await run_backup(plan, run_cmd, base=get_backup_base())
    if run.ok:
        toast = translate("backups_run_ok", language, alias=plan.alias, path=run.detail)
    else:
        toast = translate(
            "backups_run_failed", language, alias=plan.alias, status=run.status
        )
    await _render_panel(update, toast)


@admin_authorized
async def sched_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``schedadd``: step 1 — pick the server alias for the new plan."""
    pressed = _pressed(update)
    if pressed is None:
        return
    query, user_id, _data = pressed
    language = state._get_user_language(user_id)
    await query.answer()

    servers = database.get_all_servers(user_id) or []
    aliases = [str(server.get("alias", "")) for server in servers if server.get("alias")]
    if not aliases:
        await _render_panel(update, translate("backups_no_servers", language))
        return

    text, markup = ui_kit.menu(
        translate("backups_pick_alias", language),
        [(alias, f"{ADD_ALIAS_PREFIX}{alias}") for alias in aliases[:MAX_ALIASES_SHOWN]],
        back=MENU_CALLBACK,
    )
    await _render(update, text, markup)


@admin_authorized
async def sched_add_alias(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``schedadd_alias_<alias>``: step 2 — pick the daily hour."""
    pressed = _pressed(update)
    if pressed is None:
        return
    query, user_id, data = pressed
    language = state._get_user_language(user_id)
    await query.answer()

    alias = data[len(ADD_ALIAS_PREFIX):]
    if not alias:
        await _render_panel(update)
        return

    row: list[InlineKeyboardButton] = []
    rows: list[list[InlineKeyboardButton]] = []
    for hour in HOUR_CHOICES:
        row.append(
            InlineKeyboardButton(
                translate("backups_hour_label", language, hour=hour),
                callback_data=f"{ADD_HOUR_PREFIX}{alias}_{hour}",
            )
        )
        if len(row) == HOURS_PER_ROW:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(translate("ui_back", language), callback_data=ADD_CALLBACK)])
    await _render(
        update,
        translate("backups_pick_hour", language, alias=alias),
        InlineKeyboardMarkup(rows),
    )


@admin_authorized
async def sched_add_hour(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``schedadd_hour_<alias>_<hour>``: step 3 — pick how many to keep."""
    pressed = _pressed(update)
    if pressed is None:
        return
    query, user_id, data = pressed
    language = state._get_user_language(user_id)
    await query.answer()

    payload = data[len(ADD_HOUR_PREFIX):]
    alias, _, hour_text = payload.rpartition("_")
    hour = _parse_int(hour_text)
    if not alias or hour is None or hour not in HOUR_CHOICES:
        await _render_panel(update, translate("backups_plan_not_found", language))
        return

    text, markup = ui_kit.menu(
        translate("backups_pick_keep", language, alias=alias),
        [
            (
                translate("backups_keep_label", language, keep=keep),
                f"{ADD_KEEP_PREFIX}{alias}_{hour}_{keep}",
            )
            for keep in KEEP_CHOICES
        ],
        back=ADD_CALLBACK,
    )
    await _render(update, text, markup)


@admin_authorized
async def sched_add_keep(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``schedadd_keep_<alias>_<hour>_<keep>``: step 4 — confirm the plan."""
    pressed = _pressed(update)
    if pressed is None:
        return
    query, user_id, data = pressed
    language = state._get_user_language(user_id)
    await query.answer()

    payload = data[len(ADD_KEEP_PREFIX):]
    alias, hour, keep = _parse_plan_payload(payload)
    if not alias or hour is None or keep is None or keep not in KEEP_CHOICES:
        await _render_panel(update, translate("backups_plan_not_found", language))
        return

    summary = translate("backups_plan_saved", language, alias=alias)
    text = f"{summary}\n\n{_plan_line(BackupPlan(alias=alias, hour=hour, keep=keep))}"
    markup = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    translate("backups_confirm", language),
                    callback_data=f"{CONFIRM_PREFIX}{alias}_{hour}_{keep}",
                )
            ],
            [
                InlineKeyboardButton(
                    translate("ui_back", language), callback_data=ADD_CALLBACK
                )
            ],
        ]
    )
    await _render(update, text, markup)


@admin_authorized
async def sched_add_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``schedadd_<alias>_<hour>_<keep>``: persists the plan and re-renders."""
    pressed = _pressed(update)
    if pressed is None:
        return
    query, user_id, data = pressed
    language = state._get_user_language(user_id)
    await query.answer()

    payload = data[len(CONFIRM_PREFIX):]
    alias, hour, keep = _parse_plan_payload(payload)
    try:
        plan = BackupPlan(alias=alias, hour=hour, keep=keep, owner_id=user_id)  # type: ignore[arg-type]
    except ValueError as exc:
        logger.info("rejected scheduled-backup plan from callback: %s", exc)
        METRICS.inc("sched_plan_add_total", outcome="invalid")
        await _render_panel(update, translate("backups_plan_not_found", language))
        return

    saved = storage.add_plan(plan)
    METRICS.inc("sched_plan_add_total", outcome="ok" if saved.id else "miss")
    toast = translate("backups_plan_saved", language, alias=saved.alias) if saved.id else (
        translate("backups_plan_not_found", language)
    )
    await _render_panel(update, toast)
