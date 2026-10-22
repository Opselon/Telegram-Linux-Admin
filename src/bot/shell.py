"""Purpose: Persistent interactive shell sessions.

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

from telegram import Update
from telegram.ext import ContextTypes

from ..access_control import permissions
from ..access_control import rate_limiter
from ..audit import audit
from ..command_policy import Risk
from ..command_policy import classify
from ..command_policy import validate_command_text
from ..localization import translate
from ..metrics import METRICS
import os
import tempfile

from . import state
from .state import (
    SHELL_MODE_USERS, ssh_manager, user_connections,
)

from .navigation import main_menu
from .support import _edit_message_safely, _send_message_safely

logger = logging.getLogger(__name__)


async def start_shell_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Starts an interactive shell session for the user."""
    query = update.callback_query
    await query.answer()
    alias = query.data.split('_', 2)[2]
    user_id = update.effective_user.id

    try:
        await state.ssh_manager.start_shell_session(user_id, alias)
        SHELL_MODE_USERS.add(user_id)
        state.user_connections[user_id] = alias  # Ensure connection is tracked
        await _edit_message_safely(
            query.message,
            f"🖥️ **Interactive shell started on `{alias}`.**\n\n"
            "Send any message to execute it as a command. Send `/exit_shell` to end the session.",
            user_id,
            preformatted=True
        )
    except Exception as e:
        logger.error(f"Error starting shell on {alias} for user {user_id}: {e}", exc_info=True)
        await _edit_message_safely(query.message, f"❌ **Error:** Could not start shell.\n`{e}`", user_id, preformatted=True)

async def handle_shell_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles commands sent by a user in shell mode (policy-checked)."""
    user_id = update.effective_user.id
    if user_id not in state.SHELL_MODE_USERS:
        # This message is not a shell command, process as normal (or ignore)
        return

    decision = permissions.check(user_id)
    if not decision.allowed:
        METRICS.inc("access_denied_total")
        audit.record("access_denied", user_id=user_id, outcome="denied", reason=decision.reason,
                     handler="handle_shell_command")
        return

    allowed, retry_after = state.rate_limiter.check(user_id, "shell", max_calls=20, period=60.0)
    if not allowed:
        METRICS.inc("rate_limited_total", scope="shell")
        audit.record("rate_limited", user_id=user_id, outcome="denied", scope="shell")
        await _send_message_safely(
            update.message.chat,
            translate("rate_limited", state._get_user_language(user_id), seconds=max(1, int(retry_after) + 1)),
            user_id,
            preformatted=True,
        )
        return

    alias = state.user_connections.get(user_id)
    command = update.message.text or ""
    language = state._get_user_language(user_id)

    if command.strip() == 'exit':
        await exit_shell(update, context)
        return

    try:
        validate_command_text(command)
    except ValueError as exc:
        await _send_message_safely(
            update.message.chat,
            translate("command_too_long", language, error=str(exc)),
            user_id,
            preformatted=True,
        )
        return

    assessment = classify(command)
    if assessment.risk is not Risk.SAFE:
        # Shell mode has no confirmation UI: refuse anything above SAFE and
        # point the user at the confirmed Run Command flow.
        METRICS.inc("shell_commands_blocked_total", risk=assessment.risk.value)
        audit.record(
            "shell.command_blocked",
            user_id=user_id,
            alias=alias,
            outcome="denied",
            command=command,
            rule=assessment.rule,
            risk=assessment.risk.value,
        )
        await _send_message_safely(
            update.message.chat,
            translate(
                "command_blocked_shell",
                language,
                command=command,
                reason=assessment.reason or "requires confirmation",
            ),
            user_id,
            preformatted=True,
        )
        return

    audit.record("shell.command", user_id=user_id, alias=alias, command=command)
    try:
        output = await state.ssh_manager.run_command_in_shell(user_id, alias, command)
        if not output:
            output = "[No output]"

        if len(output) > 4000:
            with tempfile.NamedTemporaryFile(mode="w+", delete=False, suffix=".txt") as f:
                f.write(output)
                f.flush()
            await update.message.reply_document(document=open(f.name, "rb"), caption=f"Shell output for `{command}`")
            os.remove(f.name)
        else:
            await _send_message_safely(update.message.chat, f"```\n{output}\n```", user_id, preformatted=True)
    except Exception as e:
        logger.error(f"Error in shell on {alias} for user {user_id}: {e}", exc_info=True)
        await _send_message_safely(update.message.chat, f"❌ **Error:**\n`{e}`", user_id, preformatted=True)

async def exit_shell(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Exits the interactive shell mode."""
    user_id = update.effective_user.id
    if user_id in state.SHELL_MODE_USERS:
        SHELL_MODE_USERS.remove(user_id)
        alias = state.user_connections.get(user_id)
        if alias:
            await state.ssh_manager.disconnect(user_id, alias)
            del state.user_connections[user_id]

        await _send_message_safely(update.message.chat, "🔌 **Shell session terminated.**", user_id, preformatted=True)
        await main_menu(update, context)
