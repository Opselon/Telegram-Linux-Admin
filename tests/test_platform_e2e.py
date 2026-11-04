"""Cross-interface end-to-end tests.

Proves the interfaces share one core: a server created through the Web API is
visible to the Telegram code path (legacy database functions), a Bale account
binds to the same account graph, job state is shared, and denial events land
in the audit trail. No real SSH or bot network access — the SSH manager is
stubbed at the boundary.
"""

from __future__ import annotations

import asyncio

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
