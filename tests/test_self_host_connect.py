"""End-to-end: the installer/admin manages the bot's own host.

The connect handler is exercised against a real local transport (it really
runs ``printf ready``) with only Telegram mocked, so this covers the flagship
4.0 feature rather than a stand-in for it.
"""

from __future__ import annotations

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src import database, main
from src.access_control import permissions, rate_limiter
from src.audit import audit
from src.domain.targets import LOCAL_ALIAS
from src.infrastructure.routing_manager import RoutingCommandManager
from src.ssh_manager import SSHManager

ADMIN_ID = 1
REGULAR_ID = 2


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("TLA_DB_FILE", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("TLA_ENCRYPTION_KEY_FILE", str(tmp_path / "key.json"))
    monkeypatch.setenv("TLA_LOCAL_TARGET_ENABLED", "1")
    database.close_db_connection()
    database.initialize_database()

    permissions.set_mode("whitelist")
    permissions.update(whitelisted=[ADMIN_ID, REGULAR_ID], admins=[ADMIN_ID])
    rate_limiter.reset()
    audit.set_enabled(True)

    main.user_connections.clear()
    main.user_language_cache.clear()
    main._get_user_language.cache_clear()
    main.container = None

    monkeypatch.setattr(main.config, "local_target_enabled", True)
    main.ssh_manager = RoutingCommandManager(
        SSHManager(),
        policy=main.TargetAccessPolicy(local_enabled=True),
        is_admin=permissions.is_admin,
    )

    yield

    main.container = None
    main.ssh_manager = None
    database.close_db_connection()


def update_for(user_id: int, data: str):
    user = SimpleNamespace(id=user_id, full_name="Cap", username="cap", is_bot=False)
    chat = AsyncMock()
    query = AsyncMock()
    query.data = data
    query.from_user = user
    query.message = AsyncMock()
    update = AsyncMock()
    update.effective_user = user
    update.effective_chat = chat
    update.callback_query = query
    return update


def answered_text(update) -> str:
    call = update.callback_query.answer.await_args
    return (call.args[0] if call.args else call.kwargs.get("text")) or ""


def edited_text(update) -> str:
    calls = update.callback_query.message.edit_message_text.call_args_list
    assert calls, "the handler never rendered anything"
    return calls[-1].args[0]


def edited_keyboard(update) -> list[str]:
    calls = update.callback_query.message.edit_message_text.call_args_list
    markup = calls[-1].kwargs.get("reply_markup")
    assert markup is not None
    return [button.callback_data for row in markup.inline_keyboard for button in row]


@pytest.mark.asyncio
async def test_admin_opens_self_host_and_gets_the_full_panel():
    update = update_for(ADMIN_ID, f"connect_{LOCAL_ALIAS}")

    await main.handle_server_connection(update, AsyncMock())

    assert main.user_connections[ADMIN_ID] == LOCAL_ALIAS
    assert "Connected" in edited_text(update)
    assert "self-host" not in edited_text(update), "internal alias must never be shown"
    callbacks = edited_keyboard(update)
    assert f"run_command_{LOCAL_ALIAS}" in callbacks
    assert f"start_shell_{LOCAL_ALIAS}" in callbacks
    assert f"disconnect_{LOCAL_ALIAS}" in callbacks

    events = [record["event"] for record in audit.tail(15)]
    assert "connect.local" in events


@pytest.mark.asyncio
async def test_regular_user_is_denied_self_host():
    update = update_for(REGULAR_ID, f"connect_{LOCAL_ALIAS}")

    await main.handle_server_connection(update, AsyncMock())

    assert REGULAR_ID not in main.user_connections
    call = update.callback_query.answer.await_args
    assert call.kwargs.get("show_alert") is True
    # The user is allowed to use the bot but not to reach the bot's own host.
    assert "admin" in answered_text(update).lower()
    update.callback_query.message.edit_message_text.assert_not_called()


@pytest.mark.asyncio
async def test_disabled_self_host_target_is_refused(monkeypatch):
    monkeypatch.setattr(main.config, "local_target_enabled", False)
    monkeypatch.setenv("TLA_LOCAL_TARGET_ENABLED", "0")
    main.container = None
    update = update_for(ADMIN_ID, f"connect_{LOCAL_ALIAS}")

    await main.handle_server_connection(update, AsyncMock())

    assert ADMIN_ID not in main.user_connections
    call = update.callback_query.answer.await_args
    assert call.kwargs.get("show_alert") is True
    assert answered_text(update)


@pytest.mark.asyncio
async def test_an_unlisted_stranger_is_stopped_by_the_access_gate():
    stranger = update_for(999, f"connect_{LOCAL_ALIAS}")

    await main.handle_server_connection(stranger, AsyncMock())

    assert 999 not in main.user_connections
    assert "whitelist" in answered_text(stranger).lower()
    stranger.callback_query.message.edit_message_text.assert_not_called()


@pytest.mark.asyncio
async def test_self_host_commands_really_execute_on_this_host():
    update = update_for(ADMIN_ID, f"connect_{LOCAL_ALIAS}")
    await main.handle_server_connection(update, AsyncMock())

    out, err, code = await main.ssh_manager.execute(ADMIN_ID, LOCAL_ALIAS, "printf self-host-ok")
    assert out == "self-host-ok"
    assert code == 0


@pytest.mark.asyncio
async def test_unknown_alias_is_reported_not_connected():
    update = update_for(ADMIN_ID, "connect_ghost")

    await main.handle_server_connection(update, AsyncMock())

    assert ADMIN_ID not in main.user_connections
    call = update.callback_query.answer.await_args
    assert call.kwargs.get("show_alert") is True
    assert "ghost" in answered_text(update)


@pytest.mark.asyncio
async def test_malicious_alias_is_rejected_before_any_lookup():
    update = update_for(ADMIN_ID, "connect_../etc/passwd")

    await main.handle_server_connection(update, AsyncMock())

    assert ADMIN_ID not in main.user_connections
    update.callback_query.message.edit_message_text.assert_not_called()
    assert any(event["event"] == "connect.invalid_alias" for event in audit.tail(20))
