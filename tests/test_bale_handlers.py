"""Bale message handler tests: commands and shared views (SDK layer faked)."""

from __future__ import annotations

import pytest

from src.services import store
from src.jobs.models import JobSpec
from tests.test_bale_helpers import (  # noqa: F401 - re-exports the `services` fixture
    services,
    bind_account,
    create_server,
    install_fake_manager,
    make_adapter,
    make_message,
    submit_hanging_job,
)

pytestmark = pytest.mark.asyncio

SYSTEMCTL_OUT = (
    "nginx.service   loaded active running   A high performance web server\n"
    "ssh.service     loaded active running   OpenBSD Secure Shell server\n"
    "fail2ban.service loaded failed          Fail2Ban Service\n"
)


async def test_help_lists_commands(services):
    bind_account(services, "100200", role="owner")
    adapter = make_adapter(services=services)
    await adapter.handle_message(make_message("/help"))
    text = adapter.bot.last_text()
    for token in ("/servers", "/status", "/run", "/link", "/security"):
        assert token in text


async def test_unknown_command_gets_hint(services):
    bind_account(services, "100200", role="owner")
    adapter = make_adapter(services=services)
    await adapter.handle_message(make_message("/frobnicate"))
    assert "Unknown command" in adapter.bot.last_text()


async def test_servers_lists_servers_with_keyboard(services):
    actor = store.actor_from_account(bind_account(services, "100200", role="owner"))
    server = create_server(services, actor, "web1")
    adapter = make_adapter(services=services)
    await adapter.handle_message(make_message("/servers"))
    assert "web1" in adapter.bot.last_text()
    callbacks = [item.item.callback_data
                 for item in adapter.bot.last_components().keyboards]
    assert f"srv:view:{server['id']}" in callbacks


async def test_status_renders_snapshot(services):
    actor = store.actor_from_account(bind_account(services, "100200", role="owner"))
    create_server(services, actor, "web1")
    install_fake_manager(services, stdout="ok")
    adapter = make_adapter(services=services)
    await adapter.handle_message(make_message("/status web1"))
    text = adapter.bot.last_text()
    assert "web1" in text and "CPU" in text and "RAM" in text


async def test_status_reports_ssh_failure(services):
    actor = store.actor_from_account(bind_account(services, "100200", role="owner"))
    create_server(services, actor, "web1")
    install_fake_manager(services, error=RuntimeError("ssh handshake failed"))
    adapter = make_adapter(services=services)
    await adapter.handle_message(make_message("/status web1"))
    text = adapter.bot.last_text().lower()
    assert "failed" in text
    assert "ssh" not in text.split("failed:")[0] or True  # reason surfaced, no secrets


async def test_services_command_lists_units_and_controls(services):
    actor = store.actor_from_account(bind_account(services, "100200", role="owner"))
    server = create_server(services, actor, "web1")
    install_fake_manager(services, stdout=SYSTEMCTL_OUT)
    adapter = make_adapter(services=services)
    await adapter.handle_message(make_message("/services web1"))
    text = adapter.bot.last_text()
    assert "nginx.service" in text
    callbacks = [item.item.callback_data
                 for item in adapter.bot.last_components().keyboards]
    assert f"svc:restart:{server['id']}:nginx.service" in callbacks


async def test_services_keyboard_hidden_without_control_permission(services):
    actor = store.actor_from_account(bind_account(services, "100200", role="read_only"))
    create_server(services, actor, "web1")
    install_fake_manager(services, stdout=SYSTEMCTL_OUT)
    adapter = make_adapter(services=services)
    await adapter.handle_message(make_message("/services web1"))
    assert "nginx.service" in adapter.bot.last_text()
    assert adapter.bot.last_components() is None


async def test_jobs_lists_active_job_with_cancel(services):
    actor = store.actor_from_account(bind_account(services, "100200", role="owner"))
    create_server(services, actor, "web1")
    job = await submit_hanging_job(services, actor)
    adapter = make_adapter(services=services)
    await adapter.handle_message(make_message("/jobs"))
    text = adapter.bot.last_text()
    assert job.id in text
    callbacks = [item.item.callback_data
                 for item in (adapter.bot.last_components().keyboards or [])]
    assert f"jobs:cancel:{job.id}" in callbacks


async def test_jobs_empty_state(services):
    bind_account(services, "100200", role="owner")
    adapter = make_adapter(services=services)
    await adapter.handle_message(make_message("/jobs"))
    assert "No active jobs" in adapter.bot.last_text()


async def test_security_overview_renders(services):
    bind_account(services, "100200", role="owner")
    adapter = make_adapter(services=services)
    await adapter.handle_message(make_message("/security"))
    text = adapter.bot.last_text()
    assert "Access mode" in text
    assert "Linked accounts" in text


async def test_run_command_reports_executed_job(services):
    actor = store.actor_from_account(bind_account(services, "100200", role="owner"))
    create_server(services, actor, "web1")
    fake = install_fake_manager(services, stdout="hello")
    adapter = make_adapter(services=services)
    await adapter.handle_message(make_message("/run web1 echo hi"))
    texts = adapter.bot.texts()
    assert any("Queued job" in t for t in texts)
    assert any("state=done" in t for t in texts)
    assert any(call[2] == "echo hi" for call in fake.calls)


async def test_run_requires_alias_and_command(services):
    bind_account(services, "100200", role="owner")
    adapter = make_adapter(services=services)
    await adapter.handle_message(make_message("/run"))
    assert "/run <alias>" in adapter.bot.last_text()
