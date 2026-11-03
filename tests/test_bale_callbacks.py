"""Bale callback tests: every press re-resolves the Actor and re-checks perms."""

from __future__ import annotations

import pytest

from src.services import store
from tests.test_bale_helpers import (  # noqa: F401 - re-exports the `services` fixture
    services,
    bind_account,
    callback_data_of,
    create_server,
    install_fake_manager,
    make_adapter,
    make_callback,
    make_message,
)

pytestmark = pytest.mark.asyncio


async def test_callback_from_unlinked_stranger_rejected(services):
    bind_account(services, "100200", role="owner")
    adapter = make_adapter(services=services)
    await adapter.handle_callback(make_callback("srv:list", user_id=888001))
    assert "not authorized" in adapter.bot.last_text().lower()


async def test_server_menu_callback_edits_message(services):
    actor = store.actor_from_account(bind_account(services, "100200", role="owner"))
    server = create_server(services, actor, "web1")
    adapter = make_adapter(services=services)
    await adapter.handle_callback(
        make_callback(f"srv:view:{server['id']}", message=make_message("old")))
    assert adapter.bot.edits, "callback should edit the pressed message"
    assert "web1" in adapter.bot.edits[-1].text
    callbacks = callback_data_of(adapter.bot.edits[-1].components)
    assert f"srv:status:{server['id']}" in callbacks
    assert f"svc:start:{server['id']}:x" not in callbacks  # menu has no raw actions


async def test_status_callback_allowed_for_read_only(services):
    actor = store.actor_from_account(bind_account(services, "100200", role="read_only"))
    server = create_server(services, actor, "web1")
    install_fake_manager(services, stdout="ok")
    adapter = make_adapter(services=services)
    await adapter.handle_callback(make_callback(f"srv:status:{server['id']}"))
    text = adapter.bot.last_text()
    assert "CPU" in text  # read_only keeps monitoring.read


async def test_read_only_denied_service_restart(services):
    actor = store.actor_from_account(bind_account(services, "100200", role="read_only"))
    server = create_server(services, actor, "web1")
    install_fake_manager(services)
    adapter = make_adapter(services=services)
    await adapter.handle_callback(
        make_callback(f"svc:restart:{server['id']}:nginx"))
    assert "do not have permission" in adapter.bot.last_text().lower()
    assert services.manager.calls == []  # nothing reached the server


async def test_owner_service_action_executes(services):
    actor = store.actor_from_account(bind_account(services, "100200", role="owner"))
    server = create_server(services, actor, "web1")
    fake = install_fake_manager(services, stdout="")
    adapter = make_adapter(services=services)
    await adapter.handle_callback(
        make_callback(f"svc:stop:{server['id']}:nginx"))
    assert "nginx.service" in adapter.bot.last_text()
    assert fake.calls and "systemctl stop" in fake.calls[0][2]


async def test_foreign_server_lookup_rejected(services):
    owner = store.actor_from_account(bind_account(services, "100200", role="owner"))
    server = create_server(services, owner, "web1")
    bind_account(services, "888002", role="operator", username="web:op2")
    adapter = make_adapter(services=services)
    await adapter.handle_callback(
        make_callback(f"srv:view:{server['id']}", user_id=888002))
    assert "Not found" in adapter.bot.last_text()


async def test_unknown_route_rejected(services):
    bind_account(services, "100200", role="owner")
    adapter = make_adapter(services=services)
    await adapter.handle_callback(make_callback("zzz:boom"))
    assert "Unknown or expired" in adapter.bot.last_text()


async def test_docker_and_firewall_views_render(services):
    actor = store.actor_from_account(bind_account(services, "100200", role="owner"))
    server = create_server(services, actor, "web1")
    install_fake_manager(
        services,
        stdout="abc123|web|nginx:latest|running|Up 2 hours\n",
    )
    adapter = make_adapter(services=services)
    await adapter.handle_callback(make_callback(f"srv:docker:{server['id']}"))
    assert "web" in adapter.bot.last_text()
    await adapter.handle_callback(make_callback(f"srv:firewall:{server['id']}"))
    assert "Firewall" in adapter.bot.last_text()


async def test_processes_view_renders(services):
    actor = store.actor_from_account(bind_account(services, "100200", role="owner"))
    server = create_server(services, actor, "web1")
    install_fake_manager(
        services,
        stdout="1 0 root 1.0 0.5 1:00 /sbin/init\n",
    )
    adapter = make_adapter(services=services)
    await adapter.handle_callback(make_callback(f"srv:procs:{server['id']}"))
    assert "/sbin/init" in adapter.bot.last_text()


async def test_run_prompt_shows_usage(services):
    actor = store.actor_from_account(bind_account(services, "100200", role="owner"))
    server = create_server(services, actor, "web1")
    adapter = make_adapter(services=services)
    await adapter.handle_callback(make_callback(f"srv:run:{server['id']}"))
    assert f"/run web1" in adapter.bot.last_text()


async def test_jobs_cancel_callback(services):
    from tests.test_bale_helpers import submit_hanging_job

    actor = store.actor_from_account(bind_account(services, "100200", role="owner"))
    create_server(services, actor, "web1")
    job = await submit_hanging_job(services, actor)
    adapter = make_adapter(services=services)
    await adapter.handle_callback(make_callback(f"jobs:cancel:{job.id}"))
    assert "cancelled" in adapter.bot.last_text().lower()


async def test_message_edit_falls_back_to_send_on_error(services):
    bind_account(services, "100200", role="owner")
    adapter = make_adapter(services=services)

    async def boom(*args, **kwargs):
        raise RuntimeError("stale message")

    adapter.bot.edit_message = boom
    await adapter.handle_callback(make_callback("zzz:x", message=make_message("old")))
    assert "Unknown or expired" in adapter.bot.last_text()
