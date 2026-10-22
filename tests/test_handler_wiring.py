"""Every callback pattern in the wiring must still cover the menus' buttons.

A screen without a handler is invisible in production: pressing the button
shows a loading clock that never resolves. This test builds the real PTB
application (no network, isolated database, fake token) and runs the callbacks
emitted by the server dashboard through the registered patterns — the same grid
serves SSH targets and the administrator's self-host target.
"""

from __future__ import annotations

import re
import tempfile
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("telegram")

FAKE_TOKEN = "123456:AAHfake-token-for-wiring-tests"


@pytest.fixture(name="wiring")
def wiring_fixture(monkeypatch):
    """Build the real application with an isolated database and a fake token."""
    with tempfile.TemporaryDirectory() as tmp:
        for env_key, value in {
            "TLA_DB_FILE": str(Path(tmp) / "bot.db"),
            "TLA_ENCRYPTION_KEY_FILE": str(Path(tmp) / "key.bin"),
            "TLA_LOCAL_TARGET_ENABLED": "1",
            "TLA_NO_ENCRYPT_DB": "1",
        }.items():
            monkeypatch.setenv(env_key, value)

        from src import main

        stub = SimpleNamespace(**{**vars(main.config), "telegram_token": FAKE_TOKEN})
        with unittest.mock.patch.object(main, "config", stub):
            yield main.build_application()


def _callback_handlers(app):
    """CallbackQueryHandlers at top level plus every one inside conversations."""
    from telegram.ext import ConversationHandler

    found = []
    for group in app.handlers.values():
        for handler in group:
            if type(handler).__name__ == "CallbackQueryHandler":
                found.append(handler)
            elif isinstance(handler, ConversationHandler):
                for list_ in [handler.entry_points] + list(handler.states.values()):
                    for step in list_:
                        if type(step).__name__ == "CallbackQueryHandler":
                            found.append(step)
    return found


def _pattern_of(handler) -> str | None:
    pattern = handler.pattern
    if pattern is None:
        return None
    return pattern.pattern if hasattr(pattern, "pattern") else pattern


def test_the_application_registers_every_advertised_command(wiring):
    from telegram.ext import ConversationHandler

    registered = {
        command
        for group in wiring.handlers.values()
        for handler in group
        if type(handler).__name__ == "CommandHandler"
        for command in handler.commands
    } | {
        command
        for group in wiring.handlers.values()
        for outer in group
        if isinstance(outer, ConversationHandler)
        for list_ in [outer.entry_points] + list(outer.states.values())
        for step in list_
        if type(step).__name__ == "CommandHandler"
        for command in step.commands
    }
    for name in ("start", "menu", "add_server", "language", "update_bot", "exit_shell", "help"):
        assert name in registered, f"/{name} is advertised but not registered"


def test_every_callback_pattern_is_anchored_and_compiles(wiring):
    patterns = [p for p in (_pattern_of(h) for h in _callback_handlers(wiring)) if p]
    assert len(patterns) >= 40, patterns
    for pattern in patterns:
        assert pattern.startswith("^"), pattern
        re.compile(pattern)  # raises if the pattern is not valid


def test_every_dashboard_button_resolves_to_a_handler(wiring):
    from src.presentation import screens

    patterns = [re.compile(p) for p in (_pattern_of(h) for h in _callback_handlers(wiring)) if p]
    for is_local in (False, True):
        view = screens.dashboard(lang="en", alias="self-host" if is_local else "web01", is_local=is_local)
        callbacks = [callback for row in view.buttons for _, callback in row]
        for callback in callbacks:
            assert any(pattern.match(callback) for pattern in patterns), callback


def test_dashboard_callbacks_stay_kind_agnostic():
    from src.presentation import screens

    local = screens.dashboard(lang="en", alias="self-host", is_local=True)
    remote = screens.dashboard(lang="en", alias="web01", is_local=False)
    local_cbs = [cb.replace("self-host", "web01") for _, cb in (b for row in local.buttons for b in row)]
    remote_cbs = [cb for _, cb in (b for row in remote.buttons for b in row)]
    assert local_cbs == remote_cbs
    assert "main_menu" in remote_cbs
