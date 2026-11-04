"""Cross-interface end-to-end tests.

Proves the interfaces share one core: a server created through the Web API is
visible to the Telegram code path (legacy database functions), a Bale account
binds to the same account graph, job state is shared, and denial events land
in the audit trail. No real SSH or bot network access — the SSH manager is
stubbed at the boundary.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from fastapi.testclient import TestClient

from src import database
from src.api import create_app
from src.services import build_services, store
from src.services.migrations import run_migrations


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("TLA_DB_FILE", str(tmp_path / "e2e.db"))
    monkeypatch.setenv("TLA_ENCRYPTION_KEY_FILE", str(tmp_path / "keys.key"))
    monkeypatch.setenv("TLA_AUDIT_FILE", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("TLA_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("TLA_ACCESS_MODE", "open")
    database.close_db_connection()
    run_migrations(snapshot=False)
    yield tmp_path
    database.close_db_connection()


def _web_owner(env) -> tuple[TestClient, dict[str, str], int]:
    client = TestClient(create_app())
    client.post("/api/v1/auth/setup", json={
        "username": "chief", "password": "S3curePass!2026"})
    headers = {"X-CSRF-Token": client.cookies.get("tla_csrf")}
    server_id = client.post(
        "/api/v1/servers",
        json={"alias": "prod-01", "hostname": "prod.example.invalid", "user": "deploy",
              "password": "s3cret", "port": 2201},
        headers=headers,
    ).json()["id"]
    return client, headers, server_id


def test_web_created_server_visible_to_telegram_and_job_state_shared(env):
    client, headers, server_id = _web_owner(env)
    account = store.ensure_account("web", "web:chief", role="owner")
    actor = store.actor_from_account(account)

    # 1. The legacy Telegram code path (raw database functions) sees the same
    #    server record — same table, same encrypted credentials.
    legacy = database.get_server(actor.owner_id, "prod-01")
    assert legacy is not None
    assert legacy["hostname"] == "prod.example.invalid"
    assert legacy["port"] == 2201
    assert legacy["password"] == "s3cret"  # decrypted through the same Fernet store

    # 2. A Bale account links to the same account and can run the shared
    #    command flow against the web-created server.
    code = client.post("/api/v1/bale/link-code", json={}, headers=headers).json()["code"]
    bound = store.consume_link_code(code)
    assert bound is not None and bound.id == account.id
    store.bind_bale_user("bale-42", bound.id, "Bale Admin")
    bale_account = store.get_bale_binding("bale-42")
    assert bale_account is not None
    bale_actor = store.actor_from_account(bale_account)
    assert bale_actor.role == "owner"

    # 3. Shared job state: a command queued through the service layer (the
    #    same object the Bale adapter and web API use) is visible to the
    #    job query surface regardless of which interface submitted it.
    services = build_services()
    plan = asyncio.run(services.commands.request(actor, server_id, "echo shared-state"))
    assert plan.kind == "executed"
    jobs = services.commands.list_jobs(actor)
    assert any(job.id == plan.job.id for job in jobs)  # visible from web actor
    assert any(job.id == plan.job.id for job in services.commands.list_jobs(bale_actor))
    assert services.commands.get_job(bale_actor, plan.job.id) is not None

    # 4. The audit trail recorded the platform-side events.
    from src.audit import audit

    events = [e.get("event") for e in audit.tail(limit=200)]
    assert "command.queued" in events
    assert "server.created" in events


def test_denied_cross_tenant_access_records_security_event(env):
    client, headers, server_id = _web_owner(env)
    # Second account attempts to use the first account's server.
    stranger = store.ensure_account("web", "web:other", role="operator")
    stranger_actor = store.actor_from_account(stranger)
    services = build_services()
    with pytest.raises(LookupError):
        services.servers.get_server(stranger_actor, server_id)
    from src.audit import audit

    events = [e.get("event") for e in audit.tail(limit=100)]
    assert "security.denied" in events


def test_web_login_failure_is_audited(env):
    client, headers, _sid = _web_owner(env)
    client.post("/api/v1/auth/logout", headers=headers)
    client.post("/api/v1/auth/login", json={"username": "root", "password": "nope"})
    from src.audit import audit

    events = [e.get("event") for e in audit.tail(limit=50)]
    assert "auth.login.failed" in events


# --- platform runtime ----------------------------------------------------------

@pytest.mark.asyncio
async def test_platform_serves_legacy_health_endpoint(env, monkeypatch):
    """tla-platform must serve /healthz on TLA_HEALTH_PORT in production mode.

    Regression test for the post-merge CD failure: the Docker ENTRYPOINT moved
    from tla-bot to tla-platform, but run_platform() only started the health
    server in SMOKE_TEST mode, so a container healthcheck configured for the
    legacy port never went healthy.
    """
    import socket

    from src.runtime_app import run_platform

    # Production mode (no SMOKE_TEST): web adapter on an ephemeral port, no
    # bot tokens — the shape a container runs in when only the web UI is on.
    monkeypatch.delenv("SMOKE_TEST", raising=False)
    monkeypatch.setenv("TLA_WEB_ENABLED", "1")
    monkeypatch.setenv("TLA_WEB_HOST", "127.0.0.1")
    monkeypatch.delenv("BALE_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TLA_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        web_port = s.getsockname()[1]
    monkeypatch.setenv("TLA_WEB_PORT", str(web_port))

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setenv("TLA_HEALTH_PORT", str(port))

    task = asyncio.create_task(run_platform())
    try:
        # Wait for the health endpoint to answer (it starts before adapters).
        # The client must be an in-loop stream: urllib.request would block the
        # very loop the server runs on.
        deadline = asyncio.get_running_loop().time() + 30
        while asyncio.get_running_loop().time() < deadline:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", port), timeout=2
                )
                with contextlib.suppress(Exception):
                    writer.write(b"GET /healthz HTTP/1.1\r\nHost: localhost\r\n\r\n")
                    await writer.drain()
                    data = await asyncio.wait_for(reader.read(256), timeout=2)
                    writer.close()
                    if data.startswith(b"HTTP/1.1 200"):
                        break
            except (OSError, TimeoutError, asyncio.TimeoutError):
                await asyncio.sleep(0.2)
        else:  # pragma: no cover
            pytest.fail("platform never answered /healthz on TLA_HEALTH_PORT")
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
