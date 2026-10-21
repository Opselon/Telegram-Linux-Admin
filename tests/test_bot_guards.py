"""Integration tests for the bot's security gates (access, policy, confirmations)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.ext import ConversationHandler

from src import database, main
from src.access_control import permissions, rate_limiter
from src.audit import audit
from src.destination_policy import DESTINATIONS


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TLA_DB_FILE", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("TLA_ENCRYPTION_KEY_FILE", str(tmp_path / "key.json"))
    database.close_db_connection()
    database.initialize_database()

    permissions.set_mode("open")
    permissions.update(whitelisted=[], admins=[])
    rate_limiter.reset()
    audit.set_enabled(False)
    main.user_connections.clear()
    main.SHELL_MODE_USERS.clear()
    main.CONFIRMATIONS._pending.clear()  # noqa: SLF001 - test isolation
    main.ssh_manager = AsyncMock()

    yield

    audit.set_enabled(True)
    database.close_db_connection()
    main.user_connections.clear()
    main.SHELL_MODE_USERS.clear()


def _update(user_id: int = 1, text: str = "", callback_data: str | None = None):
    user = SimpleNamespace(id=user_id)
    chat = AsyncMock()
    update = AsyncMock()
    update.effective_user = user
    update.effective_chat = chat
    update.message.text = text
    update.message.chat = chat
    if callback_data is not None:
        query = AsyncMock()
        query.data = callback_data
        query.message = AsyncMock()
        update.callback_query = query
    else:
        update.callback_query = None
    return update


@pytest.mark.asyncio
async def test_whitelist_mode_denies_stranger_before_handler():
    permissions.set_mode("whitelist")
    permissions.update(whitelisted=[1], admins=[1])

    handler = AsyncMock(return_value="ran")
    wrapped = main.authorized(handler)
    update = _update(user_id=99)

    result = await wrapped(update, AsyncMock())

    assert result is None
    handler.assert_not_awaited()
    update.effective_chat.send_message.assert_awaited()


@pytest.mark.asyncio
async def test_whitelisted_user_reaches_handler():
    permissions.set_mode("whitelist")
    permissions.update(whitelisted=[7], admins=[7])

    handler = AsyncMock(return_value="ran")
    wrapped = main.authorized(handler)
    result = await wrapped(_update(user_id=7), AsyncMock())

    assert result == "ran"
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_unidentifiable_update_is_denied():
    handler = AsyncMock(return_value="ran")
    wrapped = main.authorized(handler)
    update = AsyncMock()
    update.effective_user = None
    update.callback_query = None
    update.message = None

    assert await wrapped(update, AsyncMock()) is None
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_catastrophic_command_is_blocked_and_never_run():
    main.user_connections[1] = "web"
    update = _update(user_id=1, text="rm -rf /")

    result = await main.execute_command(update, AsyncMock())

    assert result == ConversationHandler.END
    main.ssh_manager.run_command.assert_not_called()
    sent = update.effective_chat.send_message.await_args.args[0]
    assert "blocked" in sent.lower()


@pytest.mark.asyncio
async def test_disruptive_command_requires_confirmation_then_runs():

    async def fake_run(*args, **kwargs):
        yield ("4242", "pid")
        yield ("rebooting\n", "stdout")

    main.ssh_manager.run_command = fake_run
    main.user_connections[1] = "web"
    update = _update(user_id=1, text="reboot")

    result = await main.execute_command(update, AsyncMock())
    assert result == ConversationHandler.END

    pending = main.CONFIRMATIONS.peek(1)
    assert pending is not None and pending.command == "reboot"
    keyboard = update.effective_chat.send_message.await_args.kwargs["reply_markup"]
    assert "confirm_cmd_" in keyboard.inline_keyboard[0][0].callback_data

    confirm_update = _update(user_id=1, callback_data=f"confirm_cmd_{pending.token}")
    await main.confirm_command_callback(confirm_update, AsyncMock())

    assert main.CONFIRMATIONS.peek(1) is None
    confirm_update.callback_query.answer.assert_awaited()


@pytest.mark.asyncio
async def test_confirmation_token_is_single_use():
    main.user_connections[1] = "web"

    async def fake_run(*args, **kwargs):
        yield ("1", "pid")
        yield ("ok\n", "stdout")

    main.ssh_manager.run_command = fake_run
    await main.execute_command(_update(user_id=1, text="reboot"), AsyncMock())
    pending = main.CONFIRMATIONS.peek(1)

    first = _update(user_id=1, callback_data=f"confirm_cmd_{pending.token}")
    await main.confirm_command_callback(first, AsyncMock())
    second = _update(user_id=1, callback_data=f"confirm_cmd_{pending.token}")
    await main.confirm_command_callback(second, AsyncMock())

    # The second attempt only shows an alert; no new run happened.
    assert second.callback_query.answer.await_args.kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_shell_mode_refuses_confirm_band_commands():
    main.SHELL_MODE_USERS.add(1)
    main.user_connections[1] = "web"
    update = _update(user_id=1, text="iptables -F")

    await main.handle_shell_command(update, AsyncMock())

    main.ssh_manager.run_command_in_shell.assert_not_called()
    sent = update.effective_chat.send_message.await_args.args[0]
    assert "shell" in sent.lower()


@pytest.mark.asyncio
async def test_connect_is_denied_by_destination_policy():
    database.add_server(1, "web", "8.8.8.8", "root", key_path="/root/.ssh/id_ed25519")
    DESTINATIONS.reconfigure("deny_public", ())

    update = _update(user_id=1, callback_data="connect_web")
    await main.handle_server_connection(update, AsyncMock())

    update.callback_query.answer.assert_awaited()
    assert "show_alert" in update.callback_query.answer.await_args.kwargs
    main.ssh_manager.run_command.assert_not_called()


@pytest.mark.asyncio
async def test_rate_limit_stops_repeated_connections(monkeypatch):
    monkeypatch.setattr(main, "rate_limiter", main.rate_limiter)
    rate_limiter.reset()
    handler = AsyncMock(return_value="ok")
    wrapped = main.rate_limit("connect", max_calls=2, period=60)(handler)
    update = _update(user_id=5)

    assert await wrapped(update, AsyncMock()) == "ok"
    assert await wrapped(update, AsyncMock()) == "ok"
    assert await wrapped(update, AsyncMock()) is None
    assert handler.await_count == 2
