# PURPOSE: Integration tests for the in-chat Security Center: admin gating,
#          mode switching (with the open-mode double confirm), whitelist
#          add/remove flows and the last-admin guard.
# OWNER: security lane, 3.5.
# CONSUMES: src.bot.security_center, src.bot.state, src.config.Config.
# PROVIDES: tests/test_security_center.py.
# INVARIANTS: non-admins never mutate anything; mutations persist through
#          the real Config (tmp file) and take effect on ``permissions``.
# EXTEND: new sec_ callbacks get handler tests here.

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.access_control import permissions, rate_limiter
from src.audit import audit
from src.bot import security_center, state
from src.config import Config


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    # Drop any TLA_* leftover from other test files (dotenv tests, reloads).
    for var in [k for k in os.environ if k.startswith("TLA_")]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TLA_DB_FILE", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("TLA_ENCRYPTION_KEY_FILE", str(tmp_path / "key.json"))
    permissions.set_mode("admin_only")
    permissions.update(whitelisted=[1], admins=[1])
    rate_limiter.reset()
    audit.set_enabled(False)
    state.config = Config(str(tmp_path / "config.json"))
    state.config.add_whitelisted_user(1)
    permissions.update(
        whitelisted=state.config.whitelisted_users,
        admins=list(state.config.whitelisted_users[:1]),
    )
    from src.access_control import SetupEnrollment

    state.setup_enrollment = SetupEnrollment("")
    yield
    audit.set_enabled(True)
    permissions.set_mode("admin_only")
    permissions.update(whitelisted=[], admins=[])


def _callback_update(user_id: int = 1, callback_data: str = "sec_menu"):
    query = AsyncMock()
    query.data = callback_data
    query.message = AsyncMock()
    update = AsyncMock()
    update.effective_user = SimpleNamespace(id=user_id)
    update.callback_query = query
    update.message = None
    update.effective_chat = AsyncMock()
    return update


def _context(user_id: int = 1):
    context = AsyncMock()
    context.user_data = {}
    return context


class TestSecurityMenu:
    @pytest.mark.asyncio
    async def test_admin_sees_overview(self):
        update = _callback_update(1, "sec_menu")
        await security_center.security_menu(update, _context())
        # _edit_message_safely prefers ``edit_message_text`` when present.
        text = update.callback_query.message.edit_message_text.call_args[0][0]
        assert "Security Center" in text

    @pytest.mark.asyncio
    async def test_non_admin_is_denied(self):
        update = _callback_update(99, "sec_menu")
        await security_center.security_menu(update, _context())
        update.callback_query.message.edit_message_text.assert_not_called()


class TestAccessModeSwitch:
    @pytest.mark.asyncio
    async def test_switch_to_whitelist_persists(self):
        update = _callback_update(1, "sec_mode_whitelist")
        await security_center.set_access_mode(update, _context())
        assert state.config.access_mode == "whitelist"
        assert permissions.mode == "whitelist"

    @pytest.mark.asyncio
    async def test_open_requires_second_tap(self):
        context = _context()
        update = _callback_update(1, "sec_mode_open")
        await security_center.set_access_mode(update, context)
        # First tap only warns; mode unchanged.
        assert state.config.access_mode == "admin_only"
        assert context.user_data.get("sec_open_confirmed") is True
        # Second tap commits.
        await security_center.set_access_mode(update, context)
        assert state.config.access_mode == "open"
        assert permissions.mode == "open"

    @pytest.mark.asyncio
    async def test_non_admin_cannot_switch(self):
        update = _callback_update(99, "sec_mode_whitelist")
        await security_center.set_access_mode(update, _context())
        assert state.config.access_mode == "admin_only"


class TestWhitelistFlows:
    @pytest.mark.asyncio
    async def test_add_start_arms_capture(self):
        context = _context()
        update = _callback_update(1, "sec_add_start")
        await security_center.whitelist_add_start(update, context)
        assert context.user_data[security_center._AWAIT_ADD] is True

    @pytest.mark.asyncio
    async def test_capture_adds_user_and_updates_permissions(self):
        context = _context()
        context.user_data[security_center._AWAIT_ADD] = True
        message = AsyncMock()
        message.text = "100200300"
        update = AsyncMock()
        update.effective_user = SimpleNamespace(id=1)
        update.message = message
        await security_center.whitelist_add_capture(update, context)
        assert 100200300 in state.config.whitelisted_users
        assert 100200300 in permissions.whitelisted

    @pytest.mark.asyncio
    async def test_capture_ignores_unarmed_users(self):
        message = AsyncMock()
        message.text = "100200300"
        update = AsyncMock()
        update.effective_user = SimpleNamespace(id=1)
        update.message = message
        await security_center.whitelist_add_capture(update, _context())
        assert 100200300 not in state.config.whitelisted_users

    @pytest.mark.asyncio
    async def test_capture_rejects_non_numeric(self):
        context = _context()
        context.user_data[security_center._AWAIT_ADD] = True
        message = AsyncMock()
        message.text = "not-a-number"
        update = AsyncMock()
        update.effective_user = SimpleNamespace(id=1)
        update.message = message
        await security_center.whitelist_add_capture(update, context)
        assert "not-a-number" not in str(state.config.whitelisted_users)

    @pytest.mark.asyncio
    async def test_cannot_remove_last_admin(self):
        update = _callback_update(1, "sec_remove_1")
        await security_center.whitelist_remove(update, _context())
        assert state.config.whitelisted_users == [1]

    @pytest.mark.asyncio
    async def test_remove_second_user_works(self):
        state.config.add_whitelisted_user(100200300)
        permissions.update(
            whitelisted=state.config.whitelisted_users,
            admins=list(state.config.whitelisted_users[:1]),
        )
        update = _callback_update(1, "sec_remove_100200300")
        await security_center.whitelist_remove(update, _context())
        assert 100200300 not in state.config.whitelisted_users
        assert 100200300 not in permissions.whitelisted
