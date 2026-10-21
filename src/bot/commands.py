"""Purpose: Policy-checked free-form command execution with streaming output.

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
from telegram.error import BadRequest
from telegram.ext import ContextTypes
from telegram.ext import ConversationHandler

from ..audit import audit
from ..command_policy import CONFIRMATIONS
from ..command_policy import Risk
from ..command_policy import classify
from ..command_policy import validate_command_text
from ..localization import translate
from ..metrics import METRICS
from ..parse_mode import MessageBuilder
from ..parse_mode import format_code_block
from ..ssh_manager import HostKeyMismatch
import asyncio
import os
import tempfile
import time

from . import state
from .state import (
    AWAIT_COMMAND, ssh_manager, user_connections,
)

from .support import _edit_message_safely, _host_key_mismatch_message, _send_message_safely

logger = logging.getLogger(__name__)


async def run_command_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Asks the user for a command to run."""
    query = update.callback_query
    await query.answer()

    context.user_data['alias'] = query.data.split('_', 2)[2]

    user_id = update.effective_user.id
    alias = context.user_data['alias']
    parse_mode = state._get_user_parse_mode(user_id)
    builder = MessageBuilder(parse_mode)
    builder.add_text("Ok, please send the command you want to run on ")
    builder.add_bold(alias)
    builder.add_text(".")
    await _edit_message_safely(query.message, builder.build(), user_id, preformatted=True)
    return AWAIT_COMMAND

async def _stream_command_result(
    chat,
    user_id: int,
    alias: str,
    command: str,
    result_message,
) -> None:
    """Streams remote output into ``result_message`` with rate-limited edits.

    Shared by the direct run path and the confirmed (dangerous command) path so
    both behave identically.
    """
    parse_mode = state._get_user_parse_mode(user_id)
    output_buffer: list[str] = []
    last_sent_text = ""
    edit_interval = 1.5  # seconds between UI refreshes
    last_edit_time = time.monotonic()
    started_at = time.monotonic()

    try:
        async for item, stream in state.ssh_manager.run_command(user_id, alias, command):
            if stream == "pid":
                continue
            if stream in ("stdout", "stderr"):
                output_buffer.append(item)

                now = time.monotonic()
                if now - last_edit_time >= edit_interval:
                    partial_output = "".join(output_buffer)[-3800:]
                    if partial_output != last_sent_text:
                        try:
                            code_block = format_code_block(partial_output, "", parse_mode)
                            await _edit_message_safely(result_message, code_block, user_id, preformatted=True)
                            last_sent_text = partial_output
                        except BadRequest as exc:
                            if "Message is not modified" not in str(exc):
                                logger.warning("Telegram update error: %s", exc)
                        last_edit_time = now

        final_output = "".join(output_buffer).strip()
        if not final_output:
            final_output = "[No output returned]"

        audit.record(
            "command.completed",
            user_id=user_id,
            alias=alias,
            command=command,
            duration_ms=round((time.monotonic() - started_at) * 1000),
            output_bytes=len(final_output.encode("utf-8", "ignore")),
        )

        builder = MessageBuilder(parse_mode)
        builder.add_text("✅ ")
        builder.add_bold("Command completed on ")
        builder.add_code(alias)
        builder.add_line()
        builder.add_code_block(final_output)
        final_text = builder.build()

        if len(final_text) > 4096:
            with tempfile.NamedTemporaryFile(mode="w+", delete=False, suffix=".txt") as handle:
                handle.write(final_output)
                handle.flush()
                document_path = handle.name
            await result_message.delete()
            caption_builder = MessageBuilder(parse_mode)
            caption_builder.add_text("Output for ")
            caption_builder.add_code(command)
            with open(document_path, "rb") as document:
                await _send_message_safely(
                    chat,
                    caption_builder.build(),
                    user_id,
                    preformatted=True,
                    document=document,
                )
            os.remove(document_path)
        else:
            await _edit_message_safely(result_message, final_text, user_id, preformatted=True)

    except asyncio.TimeoutError:
        audit.record("command.timeout", user_id=user_id, alias=alias, command=command, outcome="timeout")
        await _edit_message_safely(
            result_message,
            f"⏰ **Timeout:** Command `{command}` took too long and was terminated.",
            user_id,
            preformatted=True,
        )
    except HostKeyMismatch as exc:
        METRICS.inc("ssh_host_key_mismatch_total")
        await _edit_message_safely(
            result_message,
            _host_key_mismatch_message(alias, exc, state._get_user_language(user_id)),
            user_id,
            preformatted=True,
        )
    except Exception as exc:
        audit.record(
            "command.failed",
            user_id=user_id,
            alias=alias,
            command=command,
            outcome="error",
            error=str(exc)[:200],
        )
        logger.error("Error executing '%s' on '%s': %s", command, alias, exc, exc_info=True)
        await _edit_message_safely(
            result_message,
            f"❌ **Error while executing command:**\n`{exc}`",
            user_id,
            preformatted=True,
        )

async def _run_command_for_user(
    chat,
    user_id: int,
    alias: str,
    command: str,
    *,
    target_message=None,
) -> None:
    """Announces a command, then streams its result into the chat."""
    parse_mode = state._get_user_parse_mode(user_id)
    builder = MessageBuilder(parse_mode)
    builder.add_text("🛰️ Running ")
    builder.add_code(command)
    builder.add_text(" on ")
    builder.add_code(alias)
    builder.add_text("...")

    if target_message is not None:
        await _edit_message_safely(target_message, builder.build(), user_id, preformatted=True)
        result_message = target_message
    else:
        result_message = await _send_message_safely(
            chat, builder.build(), user_id, preformatted=True
        )

    await _stream_command_result(chat, user_id, alias, command, result_message)

async def execute_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Executes a command after validation, destination and risk checks."""
    user_id = update.effective_user.id
    alias = state.user_connections.get(user_id)
    command = (update.message.text or "").strip()
    language = state._get_user_language(user_id)

    if not alias:
        await _send_message_safely(
            update.message.chat,
            translate("no_active_connection", language),
            user_id,
            preformatted=True,
        )
        return ConversationHandler.END

    try:
        validate_command_text(command)
    except ValueError as exc:
        audit.record("command.rejected", user_id=user_id, alias=alias, outcome="invalid", error=str(exc))
        await _send_message_safely(
            update.message.chat,
            translate("command_too_long", language, error=str(exc)),
            user_id,
            preformatted=True,
        )
        return ConversationHandler.END

    assessment = classify(command)
    if assessment.risk is Risk.BLOCKED:
        METRICS.inc("commands_blocked_total", rule=assessment.rule)
        audit.record(
            "command.blocked",
            user_id=user_id,
            alias=alias,
            outcome="denied",
            command=command,
            rule=assessment.rule,
            reason=assessment.reason,
        )
        await _send_message_safely(
            update.message.chat,
            translate(
                "command_blocked",
                language,
                command=command,
                reason=assessment.reason,
            ),
            user_id,
            preformatted=True,
        )
        return ConversationHandler.END

    if assessment.risk is Risk.CONFIRM:
        pending = CONFIRMATIONS.create(user_id, alias, command, assessment)
        METRICS.inc("commands_needing_confirmation_total", rule=assessment.rule)
        audit.record(
            "command.confirmation_requested",
            user_id=user_id,
            alias=alias,
            command=command,
            rule=assessment.rule,
            reason=assessment.reason,
        )
        keyboard = [
            [
                InlineKeyboardButton(
                    "✅ Confirm", callback_data=f"confirm_cmd_{pending.token}"
                ),
                InlineKeyboardButton(
                    "❌ Cancel", callback_data=f"cancel_cmd_{pending.token}"
                ),
            ]
        ]
        await _send_message_safely(
            update.message.chat,
            translate(
                "command_confirm_required",
                language,
                command=command,
                reason=assessment.reason,
            ),
            user_id,
            reply_markup=InlineKeyboardMarkup(keyboard),
            preformatted=True,
        )
        return ConversationHandler.END

    METRICS.inc("commands_executed_total")
    audit.record("command.executed", user_id=user_id, alias=alias, command=command)
    await _run_command_for_user(update.message.chat, user_id, alias, command)
    return ConversationHandler.END

async def confirm_command_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs a CONFIRM-band command after an explicit, single-use confirmation."""
    query = update.callback_query
    user_id = update.effective_user.id
    language = state._get_user_language(user_id)
    try:
        _, _, token = query.data.split("_", 2)
    except ValueError:
        await query.answer("Invalid confirmation request.", show_alert=True)
        return

    pending = CONFIRMATIONS.pop(user_id, token)
    if pending is None:
        message = translate("command_confirmation_expired", language)
        await query.answer(message, show_alert=True)
        await _edit_message_safely(query.message, message, user_id, preformatted=True)
        return

    await query.answer(translate("command_confirmed", language))
    METRICS.inc("commands_confirmed_total", rule=pending.assessment.rule)
    audit.record(
        "command.confirmed",
        user_id=user_id,
        alias=pending.alias,
        command=pending.command,
        rule=pending.assessment.rule,
    )
    await _run_command_for_user(
        query.message.chat,
        user_id,
        pending.alias,
        pending.command,
        target_message=query.message,
    )

async def cancel_command_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Drops a pending confirmation without executing anything."""
    query = update.callback_query
    user_id = update.effective_user.id
    language = state._get_user_language(user_id)
    cancelled = CONFIRMATIONS.cancel(user_id)
    await query.answer()
    audit.record(
        "command.confirmation_cancelled",
        user_id=user_id,
        outcome="cancelled",
    )
    await _edit_message_safely(
        query.message,
        translate("command_cancelled" if cancelled else "command_confirmation_expired", language),
        user_id,
        preformatted=True,
    )

async def cancel_command_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancels a running command."""
    query = update.callback_query
    await query.answer()
    _, alias, pid = query.data.split('_', 2)

    try:
        user_id = update.effective_user.id
        await state.ssh_manager.kill_process(user_id, alias, int(pid))
        user_id = update.effective_user.id
        parse_mode = state._get_user_parse_mode(user_id)
        builder = MessageBuilder(parse_mode)
        builder.add_text("✅ ")
        builder.add_bold("Command (PID: ")
        builder.add_code(str(pid))
        builder.add_bold(") cancelled on ")
        builder.add_code(alias)
        builder.add_text(".")
        await _edit_message_safely(query.message, builder.build(), user_id, preformatted=True)
    except Exception as e:
        user_id = update.effective_user.id
        parse_mode = state._get_user_parse_mode(user_id)
        builder = MessageBuilder(parse_mode)
        builder.add_text("❌ ")
        builder.add_bold("Error:")
        builder.add_text(" Could not cancel command.")
        builder.add_line()
        builder.add_code(str(e))
        await _edit_message_safely(query.message, builder.build(), user_id, preformatted=True)
