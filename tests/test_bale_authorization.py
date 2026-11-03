"""Bale authorization tests: /start linking flow, link codes, stranger denial."""

from __future__ import annotations

import re

import pytest

from src.adapters.bale import authorization
from src.services import store
from tests.test_bale_helpers import (  # noqa: F401 - re-exports the `services` fixture
    services,
    bind_account,
    make_adapter,
    make_callback,
    make_message,
)

pytestmark = pytest.mark.asyncio

CODE_IN_TEXT = re.compile(r"\b([2-9A-HJ-NP-Z]{6})\b")


async def test_start_unbound_opens_linking_session(services):
    bot = services  # noqa: F841 (fixture keeps the tmp DB alive)
    adapter = make_adapter(services=services)
    await adapter.handle_message(make_message("/start", user_id=555001))
    assert "link code" in adapter.bot.last_text().lower()
    assert adapter.links.active("555001")


async def test_unbound_stranger_rejected_on_every_command(services):
    adapter = make_adapter(services=services)
    # /start and /help are deliberately public (handlers.py dispatch); every
    # operational command is gated behind the linking prompt.
    for command in ("/servers", "/status web1", "/services", "/jobs",
                    "/security", "/run web1 echo hi", "/link"):
        await adapter.handle_message(make_message(command, user_id=555002))
    assert all("account linking" in text.lower() for text in adapter.bot.texts())
    # /help stays reachable for strangers (it only advertises /start + linking).
    await adapter.handle_message(make_message("/help", user_id=555002))
    assert "commands" in adapter.bot.last_text().lower()


async def test_unbound_stranger_rejected_on_callbacks(services):
    adapter = make_adapter(services=services)
    await adapter.handle_callback(make_callback("srv:list", user_id=555003))
    assert "not authorized" in adapter.bot.last_text().lower()


async def test_valid_code_binds_and_persists(services):
    account = bind_account(services, "0", role="owner")  # code issuer only
    actor = store.actor_from_account(account)
    code = services.security.create_link_code(actor, None)

    adapter = make_adapter(services=services)
    await adapter.handle_message(make_message("/start", user_id=555004))
    await adapter.handle_message(make_message(code, user_id=555004))
    assert any("Linked" in text for text in adapter.bot.texts())

    binding = store.get_bale_binding("555004")
    assert binding is not None and binding.id == account.id
    assert not adapter.links.active("555004")

    # The freshly bound user now gets the real surface.
    await adapter.handle_message(make_message("/servers", user_id=555004))
    assert "web1" not in adapter.bot.last_text()  # no servers yet for owner
    assert "No servers" in adapter.bot.last_text()


async def test_invalid_and_malformed_codes_rejected(services):
    account = bind_account(services, "0", role="owner")
    actor = store.actor_from_account(account)
    services.security.create_link_code(actor, None)  # a code exists, but...

    adapter = make_adapter(services=services)
    adapter.links.begin("555005")
    await adapter.handle_message(make_message("000000", user_id=555005))
    await adapter.handle_message(make_message("abc", user_id=555005))
    assert adapter.bot.texts()[-1] == authorization.INVALID_CODE
    assert store.get_bale_binding("555005") is None
    # Session survives a wrong code so the user can retry.
    assert adapter.links.active("555005")


async def test_expired_code_rejected(services):
    account = bind_account(services, "0", role="owner")
    actor = store.actor_from_account(account)
    code = store.create_link_code(account.id, ttl=-1.0)

    adapter = make_adapter(services=services)
    adapter.links.begin("555006")
    await adapter.handle_message(make_message(code, user_id=555006))
    assert adapter.bot.last_text() == authorization.INVALID_CODE
    assert store.get_bale_binding("555006") is None


async def test_link_sessions_expire(services):
    sessions = authorization.LinkSessions(ttl=-1.0)
    sessions.begin("777")
    assert not sessions.active("777")
    # sweep() removes expired sessions and reports how many it dropped.
    assert sessions.sweep() == 1
    assert sessions.sweep() == 0


async def test_link_command_creates_single_use_code(services):
    account = bind_account(services, "555007", role="owner")
    adapter = make_adapter(services=services)
    await adapter.handle_message(make_message("/link", user_id=555007))
    match = CODE_IN_TEXT.search(adapter.bot.last_text())
    assert match, adapter.bot.last_text()
    code = match.group(1)

    # A second Bale user binds to the same account with the code.
    ok, reply = authorization.try_bind_code(
        services, adapter.links, "555008", "Other", code)
    assert ok and "Linked" in reply
    assert store.get_bale_binding("555008").id == account.id

    # Replay of the consumed code fails.
    ok2, _reply2 = authorization.try_bind_code(
        services, adapter.links, "555009", "Third", code)
    assert not ok2


async def test_unlink_removes_binding(services):
    bind_account(services, "555010", role="operator")
    adapter = make_adapter(services=services)
    await adapter.handle_message(make_message("/unlink", user_id=555010))
    assert "unlinked" in adapter.bot.last_text().lower()
    assert store.get_bale_binding("555010") is None

    await adapter.handle_message(make_message("/servers", user_id=555010))
    # After unlinking, the user is gated back to the linking conversation.
    assert "account linking" in adapter.bot.last_text().lower()


async def test_resolve_actor_maps_role_and_none_for_strangers(services):
    assert authorization.resolve_actor("424242", services=services) is None
    bind_account(services, "555011", role="admin")
    actor = authorization.resolve_actor("555011", services=services)
    assert actor is not None and actor.role == "admin"
    assert actor.kind in {"web", "bale", "telegram"}
