"""Regression tests for the /cancel command.

Two verified defects:

1. ``run_command_handler`` was built with ``fallbacks=[]``. Its ``AWAIT_COMMAND``
   state only accepts plain text (``filters.TEXT & ~filters.COMMAND``), so a
   user sitting in that state typing ``/cancel`` was **silently ignored** —
   the command matched no fallback and no state handler. The user was stuck
   until they either ran a command or gave up.

2. ``/cancel`` was registered only as a ConversationHandler *fallback*. With
   no conversation active, the command the bot's own Telegram menu advertises
   ("Cancel the current multi-step operation") matched nothing at all and
   produced no feedback.

OWNER: tests/test_cancel_command.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _command_update(user_id: int = 12345, text: str = "/cancel"):
    """A command-message update, following the pattern the existing suite
    uses (``tests/test_bot_guards.py::_update``)."""
    from types import SimpleNamespace

    user = SimpleNamespace(id=user_id)
    chat = AsyncMock()
    message = AsyncMock()
    message.text = text
    message.chat = chat

    update = AsyncMock()
    update.effective_user = user
    update.effective_chat = chat
    update.effective_message = message
    update.message = message
    return update


@pytest.mark.asyncio
async def test_run_command_conversation_can_be_cancelled():
    """A user in AWAIT_COMMAND must be able to leave it with /cancel.

    The handler must return ConversationHandler.END *and* clear user_data, so
    the partially-staged alias cannot survive into the next flow.
    """
    from src.bot.commands import cancel_any_conversation
    from telegram.ext import ConversationHandler

    update = _command_update()
    context = AsyncMock()
    context.user_data = {"alias": "prod"}

    result = await cancel_any_conversation(update, context)

    assert result is ConversationHandler.END, "cancel did not end the conversation"
    assert context.user_data == {}, "cancel left stale user_data behind"
    update.effective_message.chat.send_message.assert_awaited()


@pytest.mark.asyncio
async def test_cancel_acknowledges_even_with_no_conversation_active():
    """The top-level /cancel must always answer, never silently do nothing."""
    from src.bot.commands import cancel_any_conversation

    update = _command_update()
    context = AsyncMock()
    context.user_data = {}

    result = await cancel_any_conversation(update, context)

    # It ends "no conversation" cleanly rather than erroring...
    assert result is not None
    # ...and it tells the user something instead of vanishing.
    update.effective_message.chat.send_message.assert_awaited()


def test_cancel_is_registered_as_a_top_level_command():
    """``/cancel`` must be a top-level CommandHandler, not only a fallback.

    This is the wiring test that fails on the pre-fix tree: the command was
    registered eight times as a ConversationHandler fallback and zero times
    where PTB could match it with no conversation active.
    """
    import re

    from src.bot.application import build_application
    from src.config import config

    original = config.telegram_token
    config.telegram_token = "000000000:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    try:
        application = build_application()
    finally:
        config.telegram_token = original

    # Find every CommandHandler bound to the 'cancel' command and determine
    # whether any of them is top-level (not nested in a ConversationHandler).
    import telegram
    from telegram.ext import CommandHandler

    top_level_cancel = []

    def _walk(handler, *, nested: bool) -> None:
        # A ConversationHandler's fallbacks/states are nested.
        if isinstance(handler, telegram.ext.ConversationHandler):
            for fallback in handler.fallbacks:
                _walk(fallback, nested=True)
            for group in handler.states.values():
                for state_handler in group:
                    _walk(state_handler, nested=True)
            for entry in handler.entry_points:
                _walk(entry, nested=True)
            return
        if isinstance(handler, CommandHandler):
            commands = getattr(handler, "commands", None) or set()
            if "cancel" in commands and not nested:
                top_level_cancel.append(handler)

    for group in application.handlers.values():
        for handler in group:
            _walk(handler, nested=False)

    assert top_level_cancel, (
        "'cancel' is registered only as a conversation fallback, so with no "
        "conversation active /cancel matches nothing and the user gets no "
        "feedback at all"
    )


def test_run_command_conversation_has_a_cancel_fallback():
    """``run_command_handler`` must accept /cancel from AWAIT_COMMAND."""
    import telegram

    from src.bot.application import build_application
    from src.config import config

    original = config.telegram_token
    config.telegram_token = "000000000:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    try:
        application = build_application()
    finally:
        config.telegram_token = original

    run_command_handler = None
    for group in application.handlers.values():
        for handler in group:
            if isinstance(handler, telegram.ext.ConversationHandler):
                entries = [
                    str(getattr(e, "pattern", "")) for e in handler.entry_points
                ]
                if any("run_command_" in p for p in entries):
                    run_command_handler = handler
                    break
        if run_command_handler is not None:
            break

    assert run_command_handler is not None, "run_command_handler is not registered"
    fallback_commands = [
        sorted(h.commands) for h in run_command_handler.fallbacks
        if isinstance(h, telegram.ext.CommandHandler)
    ]
    assert ["cancel"] in fallback_commands, (
        f"run_command_handler fallbacks are {fallback_commands}; a user in "
        "AWAIT_COMMAND typing /cancel is silently ignored because the state's "
        "MessageHandler filters out commands"
    )
