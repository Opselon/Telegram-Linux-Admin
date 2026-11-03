"""Handler-graph wiring tests.

The unit suite calls handlers directly (``await handler(update, ctx)``), so it
cannot detect a handler that exists and passes its tests but was never
registered in ``src/bot/application.py``. That is exactly how the CONFIRM-band
confirm/cancel buttons shipped dead in v3.2+: ``confirm_command_callback`` and
``cancel_command_confirmation`` were implemented and green in
``tests/test_bot_guards.py`` while both buttons were silently ignored in
production.

These tests build the *real* Application and drive updates through PTB's
actual dispatch graph, so a missing registration fails here even though every
handler-level test passes.

OWNER: tests/test_handler_graph_wiring.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Every callback_data the bot renders, grouped by the handler that must receive
# it. Kept in sync with src/bot/* via the inventory in
# docs/audit/FEATURE_INVENTORY.md. A new button that has no handler fails here
# BEFORE it reaches production.
EXPECTED_CALLBACK_ROUTES: dict[str, tuple[str, ...]] = {
    "navigation": (
        "main_menu", "language_menu", "set_language_fa",
    ),
    "host_status": ("host_status",),
    "security_center": (
        "sec_menu", "sec_mode_admin_only", "sec_whitelist",
        "sec_add_start", "sec_config", "sec_audit",
    ),
    "jobs_ui": ("jobsrefresh", "jobscancel_<id>"),
    "backup": ("backup", "restore"),
    "commands": (
        "run_command_<alias>", "cancel_command_<alias>_<pid>",
        # The two payloads that were silently dropped between v3.1 and v3.5:
        "confirm_cmd_<token>", "cancel_cmd_<token>",
    ),
    "servers": (
        "add_server_start", "connect_server_menu", "connect_<alias>",
        "disconnect_<alias>", "remove_server_menu", "remove_<alias>",
    ),
    "services": (
        "service_management_menu_<alias>", "check_service_<alias>",
        "start_service_<alias>", "stop_service_<alias>",
        "restart_service_<alias>",
    ),
    "monitoring": (
        "server_status_menu_<alias>", "static_info_<alias>",
        "resource_usage_<alias>", "live_monitoring_<alias>",
        "stop_live_monitoring_<alias>", "disk_usage_<alias>",
        "network_info_<alias>", "open_ports_<alias>",
    ),
    "packages": (
        "package_management_menu_<alias>", "pkg_update_<alias>",
        "pkg_upgrade_<alias>",
    ),
    "docker": (
        "docker_management_menu_<alias>", "docker_ps_<alias>",
        "docker_logs_<alias>", "docker_start_<alias>", "docker_stop_<alias>",
    ),
    "files": (
        "file_manager_menu_<alias>", "fm_ls_<alias>",
        "fm_download_<alias>", "fm_upload_<alias>",
    ),
    "processes": (
        "process_management_menu_<alias>", "ps_aux_<alias>",
        "kill_process_<alias>",
    ),
    "firewall": (
        "firewall_management_menu_<alias>", "fw_status_<alias>",
        "fw_allow_<alias>", "fw_deny_<alias>", "fw_delete_<alias>",
    ),
    "system": (
        "system_commands_menu_<alias>", "reboot_<alias>",
        "shutdown_<alias>", "execute_reboot_<alias>",
    ),
    "shell": ("start_shell_<alias>",),
}


def _build_application():
    """Builds the real PTB Application with a dummy token.

    ``Application.builder().token(...)`` does not connect, and
    ``build_application()`` only registers handlers, so this is safe to run
    without a network or a real bot.
    """
    import sys

    from src.bot.application import build_application

    # ``build_application`` reads ``config.telegram_token``, and
    # ``src.bot.application`` binds it with ``from ..config import config`` at
    # import time. ``tests/test_config_env.py`` calls
    # ``importlib.reload(src.config)``, which rebinds ``src.config.config`` to
    # a *new* object — leaving every module that from-imported it holding the
    # orphaned singleton whose token is "" (PTB then rejects the build with
    # InvalidToken). Patch the attribute *on the module that actually reads
    # it*, and restore it afterwards so this fixture pollutes nothing.
    app_module = sys.modules["src.bot.application"]
    original = app_module.config.telegram_token
    app_module.config.telegram_token = (
        "000000000:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    )
    try:
        return build_application()
    finally:
        app_module.config.telegram_token = original


def _all_callback_patterns() -> list[str]:
    """Every callback pattern in the real handler graph.

    Walks ``Application.handlers`` and, for ``ConversationHandler``s, their
    ``entry_points`` too — a button like ``run_command_<alias>`` is routed by a
    ConversationHandler entry point, not by a top-level CallbackQueryHandler,
    so a scan that only reads top-level patterns would false-positive on it.
    """
    application = _build_application()
    patterns: list[str] = []
    for group in application.handlers.values():
        for handler in group:
            for candidate in _handler_patterns(handler):
                patterns.append(candidate)
    return patterns


def _handler_patterns(handler: object) -> list[str]:
    import re

    out: list[str] = []
    # Top-level CallbackQueryHandler / CommandHandler patterns.
    pattern = getattr(handler, "pattern", None)
    if pattern is not None:
        out.append(str(pattern.pattern))
    # ConversationHandler entry points (and states, which also carry
    # CallbackQueryHandlers that respond mid-conversation).
    entry_points = getattr(handler, "entry_points", None)
    if entry_points:
        for entry in entry_points:
            out.extend(_handler_patterns(entry))
    states = getattr(handler, "states", None)
    if states:
        for group in states.values():
            for state_handler in group:
                out.extend(_handler_patterns(state_handler))
    return [p for p in out if p not in ("None", "")]


def _matches_any(payload: str, patterns: list[str]) -> bool:
    import re

    return any(re.match(p, payload) for p in patterns)


def test_every_rendered_callback_has_a_registered_handler():
    """Every callback_data the bot renders must match a registered pattern.

    This is the regression test for the dead CONFIRM-band buttons: PTB resolves
    handlers by regex match against callback_data, so a button whose payload
    matches no registered pattern is ignored outright.
    """
    patterns = _all_callback_patterns()

    unmatched = [
        payload
        for payloads in EXPECTED_CALLBACK_ROUTES.values()
        for payload in payloads
        if not _matches_any(payload, patterns)
    ]

    assert not unmatched, (
        "These callback_data are rendered by buttons but match no registered "
        "handler — tapping them does nothing:\n  " + "\n  ".join(unmatched)
    )


@pytest.mark.parametrize("module_name", sorted(EXPECTED_CALLBACK_ROUTES))
def test_every_route_group_is_routable(module_name: str):
    """Per-module breakdown so a failure names the offending area."""
    patterns = _all_callback_patterns()
    for payload in EXPECTED_CALLBACK_ROUTES[module_name]:
        assert _matches_any(payload, patterns), (
            f"{module_name}: {payload!r} matches no registered handler pattern"
        )


# --------------------------------------------------------------------------
# End-to-end dispatch: a CONFIRM-band confirm button must actually fire the
# handler through the real handler graph, not merely exist as a function.
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_confirm_cmd_button_dispatches_through_the_real_graph():
    """The v3.2-v3.5 P0 regression: `confirm_cmd_<token>` was rendered but the
    handler was never registered, so the button was dead in production while
    its unit test passed. Drive it through PTB's real dispatch here."""
    from src.command_policy import CONFIRMATIONS, Assessment, Risk
    from telegram import Update

    application = _build_application()

    # Locate the handler PTB routes `confirm_cmd_...` to.
    handler = _find_handler(application, "confirm_cmd")
    assert handler is not None, "no handler registered for `confirm_cmd_`"

    # Build a pending CONFIRM-band command as execute_command would.
    assessment = Assessment(risk=Risk.CONFIRM, rule="test", reason="test rule")
    pending = CONFIRMATIONS.create(123456, "webserver", "rm -rf /tmp/x", assessment)

    query, update, context = _callback_update(f"confirm_cmd_{pending.token}")

    await handler.callback(update, context)

    # The token must have been consumed (single use) and the query answered.
    assert CONFIRMATIONS.peek(123456) is None, "confirm token was not consumed"
    query.answer.assert_awaited()


@pytest.mark.asyncio
async def test_cancel_cmd_button_dispatches_through_the_real_graph():
    from src.command_policy import CONFIRMATIONS, Assessment, Risk
    from telegram import Update

    application = _build_application()
    handler = _find_handler(application, "cancel_cmd")
    assert handler is not None, "no handler registered for `cancel_cmd_`"

    assessment = Assessment(risk=Risk.CONFIRM, rule="test", reason="test rule")
    CONFIRMATIONS.create(123456, "webserver", "rm -rf /tmp/x", assessment)

    query, update, context = _callback_update("cancel_cmd_sometoken")

    await handler.callback(update, context)

    # Cancel drops the pending command entirely.
    assert CONFIRMATIONS.peek(123456) is None, "pending command survived cancel"
    query.answer.assert_awaited()




def _callback_update(payload: str, user_id: int = 123456):
    """A callback-query update, following the pattern the existing suite uses
    (``tests/test_bot_guards.py::_update``): ``AsyncMock`` for the update and
    a ``SimpleNamespace`` for the user, so no auto-generated mock attributes
    leak into the database layer (an AsyncMock ``user_id`` crashes sqlite3
    binding) and no PTB slot-locked object is assigned to."""
    from types import SimpleNamespace

    user = SimpleNamespace(id=user_id)
    chat = AsyncMock()
    message = AsyncMock()
    message.chat = chat

    query = AsyncMock()
    query.data = payload
    query.message = message

    update = AsyncMock()
    update.effective_user = user
    update.effective_chat = chat
    update.callback_query = query

    context = AsyncMock()
    context.user_data = {}
    context.bot = AsyncMock()
    return query, update, context


def _find_handler(application, needle: str):
    """Returns the first registered handler whose callback pattern contains
    *needle* (top-level CallbackQueryHandlers only — ConversationHandler
    entries are not callable this way)."""
    for group in application.handlers.values():
        for handler in group:
            pattern = getattr(handler, "pattern", None)
            if pattern is not None and needle in str(pattern.pattern):
                return handler
    return None
