# PURPOSE: Telegram operator surface for the 3.3 job queue: the "My Jobs"
#          panel (running + pending, one line per job, cancel + refresh).
# OWNER: lane `jobs-hardening` of wave 3.3 (branch feat/3.3-jobs-hardening)
# CONSUMES: src.bot.state (job_queue singleton, _get_user_language),
#           src.bot.support (_edit_message_safely, _send_message_safely,
#           _extract_user_id), src.jobs.models (Job/JobState/JobSpec),
#           src.localization.translate, src.metrics.METRICS.
# PROVIDES: jobs_status_menu(update, context), cancel_job_callback(update,
#           context), jobs_refresh_callback(update, context) for
#           src/bot/application.py to register (CallbackQueryHandler with
#           patterns '^jobs_status_menu$' / '^jobscancel_' / '^jobsrefresh').
# INVARIANTS: one dispatch per callback prefix (cancel/refresh matched by
#             exact prefix, never re-dispatched through the menu); every
#             user-visible string goes through translate() (new keys en-only
#             in src/localization.py); degraded mode (state.job_queue is None)
#             shows a single explanatory message and touches nothing else;
#             callbacks are answered BEFORE editing; cancel is scoped to the
#             pressing user's own jobs (queue.cancel enforces ownership);
#             a failed edit (message too old) must never raise out of a handler.
# EXTEND: new panel rows/actions belong in _build_jobs_panel(); new callback
#           prefixes are registered in src/bot/application.py.

"""Operator panel for the job queue: what is running, what is waiting, cancel."""

from __future__ import annotations

import logging
import time

from telegram import InlineKeyboardButton
from telegram import InlineKeyboardMarkup
from telegram import Update
from telegram.ext import ContextTypes

from ..metrics import METRICS
from ..jobs.models import Job, JobState
from ..localization import translate
from . import state
from .support import _edit_message_safely, _extract_user_id, _send_message_safely

logger = logging.getLogger(__name__)

#: Callback-data prefixes owned by this module (application.py matches these).
CANCEL_PREFIX = "jobscancel_"
REFRESH_CALLBACK = "jobsrefresh"
MENU_CALLBACK = "jobs_status_menu"

#: Maximum command prefix length shown per job line before ellipsis.
_COMMAND_PREVIEW_CHARS = 32

_STATE_ICONS: dict[str, str] = {
    JobState.RUNNING.value: "⏳",
    JobState.QUEUED.value: "🕒",
}


def _truncate_command(command: str, limit: int = _COMMAND_PREVIEW_CHARS) -> str:
    """One-line command preview, collapsed whitespace, ellipsis when cut."""
    collapsed = " ".join(command.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1] + "…"


def _job_line(job: Job, now: float) -> str:
    """Compact one-line-per-job: state icon, command prefix, age seconds."""
    icon = _STATE_ICONS.get(job.state.value, "•")
    started = job.started_at if job.started_at is not None else job.created_at
    age = max(0, int(now - started))
    return f"{icon} `{_truncate_command(job.spec.command)}` ({age}s)"


def _build_jobs_panel(user_id: int | None) -> tuple[str, InlineKeyboardMarkup]:
    """Renders the panel text + inline keyboard from the live queue state.

    Pure presentation: no I/O, no awaits, deterministic ordering (running jobs
    by queue order, then pending by run order). Returns (text, reply_markup).
    """
    language = state._get_user_language(user_id)
    queue = state.job_queue
    running = list(queue.active(user_id))
    pending = list(queue.list_pending(user_id))
    now = time.time()

    header = translate("jobs_panel_title", language)
    lines = [translate("jobs_panel_running", language, count=str(len(running)))]
    lines.extend(_job_line(job, now) for job in running)
    lines.append(translate("jobs_panel_pending", language, count=str(len(pending))))
    lines.extend(_job_line(job, now) for job in pending)
    text = "\n".join(lines)
    if not running and not pending:
        text = translate("jobs_panel_empty", language)

    keyboard: list[list[InlineKeyboardButton]] = []
    for job in pending:
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"❌ {translate('jobs_cancel_prefix', language)} {job.id}",
                    callback_data=f"{CANCEL_PREFIX}{job.id}",
                )
            ]
        )
    keyboard.append(
        [
            InlineKeyboardButton(
                f"🔄 {translate('jobs_refresh', language)}", callback_data=REFRESH_CALLBACK
            ),
        ]
    )
    return text, InlineKeyboardMarkup(keyboard)


async def jobs_status_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Opens the "My Jobs" panel for the pressing user (entry or refresh)."""
    user_id = _extract_user_id(update)
    language = state._get_user_language(user_id)
    if state.job_queue is None:
        METRICS.inc("jobs_panel_degraded_total")
        message = translate("jobs_degraded", language)
        if update.callback_query is not None:
            await update.callback_query.answer()
            await _edit_message_safely(update.callback_query, message, user_id)
        elif update.effective_chat is not None:
            await _send_message_safely(update.effective_chat, message, user_id, preformatted=True)
        return

    query = update.callback_query
    if query is not None:
        await query.answer()
        text, markup = _build_jobs_panel(user_id)
        await _edit_message_safely(query, text, user_id, reply_markup=markup)
    elif update.effective_chat is not None:
        text, markup = _build_jobs_panel(user_id)
        await _send_message_safely(
            update.effective_chat, text, user_id, reply_markup=markup, preformatted=True
        )


async def cancel_job_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancels one of the pressing user's jobs via the ``jobscancel_<id>`` button."""
    query = update.callback_query
    if query is None or state.job_queue is None:
        if query is not None:
            language = state._get_user_language(_extract_user_id(update))
            await query.answer(translate("jobs_degraded", language), show_alert=True)
        return

    user_id = _extract_user_id(update)
    language = state._get_user_language(user_id)
    await query.answer()
    job_id = (query.data or "")[len(CANCEL_PREFIX) :].strip()
    cancelled = state.job_queue.cancel(user_id, job_id)
    METRICS.inc("jobs_panel_cancel_total", outcome="ok" if cancelled else "miss")
    if cancelled:
        toast = translate("jobs_cancelled_toast", language, job_id=job_id)
    else:
        toast = translate("jobs_cancel_not_found", language, job_id=job_id)
    text, markup = _build_jobs_panel(user_id)
    try:
        await _edit_message_safely(query, f"{toast}\n\n{text}", user_id, reply_markup=markup)
    except Exception:  # pragma: no cover - defensive: stale panels must not raise
        logger.warning("jobs panel refresh after cancel failed", exc_info=True)


async def jobs_refresh_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Re-renders the panel in place (``jobsrefresh`` button), idempotently."""
    query = update.callback_query
    if query is None or state.job_queue is None:
        if query is not None:
            language = state._get_user_language(_extract_user_id(update))
            await query.answer(translate("jobs_degraded", language), show_alert=True)
        return

    user_id = _extract_user_id(update)
    language = state._get_user_language(user_id)
    await query.answer(translate("jobs_refreshed_toast", language))
    text, markup = _build_jobs_panel(user_id)
    try:
        await _edit_message_safely(query, text, user_id, reply_markup=markup)
    except Exception:  # pragma: no cover - defensive: stale panels must not raise
        logger.warning("jobs panel re-render failed", exc_info=True)
