"""Regression tests for admin command paths that previously raised NameError.

Both handlers referenced variables that were never defined (``parse_mode`` in
``/dashboard`` and ``user_id`` in the update launcher), so the admin-only
features crashed on first use.  These tests execute the handlers end to end
with the database and Telegram mocked out.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src import database, main
from src.access_control import permissions
from src.audit import audit


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TLA_DB_FILE", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("TLA_ENCRYPTION_KEY_FILE", str(tmp_path / "key.json"))
    database.close_db_connection()
    database.initialize_database()
    permissions.set_mode("whitelist")
    permissions.update(whitelisted=[1], admins=[1])
    audit.set_enabled(False)
    yield
    audit.set_enabled(True)
    database.close_db_connection()


def _update(user_id: int = 1):
    chat = AsyncMock()
    update = AsyncMock()
    update.effective_user = SimpleNamespace(id=user_id)
    update.effective_chat = chat
    update.message.chat = chat
    update.callback_query = None
    return update


@pytest.mark.asyncio
async def test_dashboard_renders_without_name_error():
    update = _update()
    await main.dashboard_command(update, AsyncMock())

    update.message.chat.send_message.assert_awaited()
    rendered = update.message.chat.send_message.await_args.args[0]
    assert "Dashboard" in rendered or "Loading" in rendered


@pytest.mark.asyncio
async def test_update_launcher_uses_real_user_id(monkeypatch):
    update = _update()
    with patch("src.main.subprocess.Popen") as popen:
        await main.update_bot_command(update, AsyncMock())

    popen.assert_called_once()
    sent_message = update.effective_message.chat.send_message
    sent_message.assert_awaited()
    assert "Update process started" in sent_message.await_args.args[0]


@pytest.mark.asyncio
async def test_admin_denial_path_sends_message_instead_of_crashing():
    handler = AsyncMock()
    wrapped = main.admin_authorized(handler)
    update = _update(user_id=2)  # whitelisted nowhere, not the admin

    await wrapped(update, AsyncMock())

    handler.assert_not_awaited()
    update.effective_chat.send_message.assert_awaited()
    assert "Access Denied" in update.effective_chat.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_audit_command_shows_recent_events(tmp_path, monkeypatch):
    audit.set_enabled(True)
    monkeypatch.setattr(main.audit, "_path", tmp_path / "audit.log")
    monkeypatch.setattr(main.audit, "_directory", tmp_path)
    main.audit.record("command.executed", user_id=1, alias="web", command="uptime")

    update = _update()
    context = SimpleNamespace(args=["5"])
    await main.audit_command(update, context)

    sent = update.effective_chat.send_message.await_args.args[0]
    assert "command.executed" in sent
    assert "web" in sent
