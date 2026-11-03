"""Service-layer tests: migrations, accounts, roles, ownership isolation."""

from __future__ import annotations

import asyncio

import pytest

from src.services import build_services
from src.services import store
from src.services.command_service import CommandDenied, ConfirmationError
from src.services.migrations import current_schema_version, run_migrations
from src.services.permissions import PermissionDenied


@pytest.fixture()
def services(tmp_path, monkeypatch):
    monkeypatch.setenv("TLA_DB_FILE", str(tmp_path / "svc.db"))
    monkeypatch.setenv("TLA_ENCRYPTION_KEY_FILE", str(tmp_path / "keys.key"))
    monkeypatch.setenv("TLA_AUDIT_FILE", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("TLA_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("TLA_ACCESS_MODE", "open")
    from src import database

    database.close_db_connection()
    run_migrations(snapshot=False)
    yield build_services()
    database.close_db_connection()


def _owner(services):
    token, _info = services.auth.bootstrap_owner("root", "S3curePass!2026")
    account = store.resolve_session(token)
    return store.actor_from_account(account)


def _operator(services):
    account = store.ensure_account("web", "web:op", display_name="op", role="operator")
    return store.actor_from_account(account)


def _viewer(services):
    account = store.ensure_account("web", "web:view", display_name="view", role="read_only")
    return store.actor_from_account(account)


# --- migrations ---------------------------------------------------------------

def test_migrations_are_idempotent(services):
    applied = run_migrations(snapshot=False)
    assert applied == 0  # already applied by the fixture
    assert current_schema_version(store.__dict__ and __import__("src.database", fromlist=["get_db_connection"]).get_db_connection()) >= 3


def test_migration_backfills_telegram_accounts(services):
    from src import database

    database.add_user(4242)
    # The backfill step is idempotent; invoke it directly to map the new user.
    with database.transaction() as conn:
        from src.services.migrations import _backfill_accounts

        _backfill_accounts(conn)
    accounts = [a for a in store.list_accounts() if a.kind == "telegram"]
    assert any(a.external_id == "4242" for a in accounts)


# --- accounts + roles -----------------------------------------------------------

def test_web_owner_bootstrap_and_session_roundtrip(services):
    actor = _owner(services)
    assert actor.role == "owner"
    assert actor.has("servers.create")
    assert actor.has("users.manage")
    assert actor.owner_id < 0  # synthetic legacy owner id for platform accounts


def test_unknown_role_rejected(services):
    with pytest.raises(store.StoreError):
        store.ensure_account("web", "web:x", role="superuser")


def test_role_hierarchy_permissions(services):
    owner, op, view = _owner(services), _operator(services), _viewer(services)
    assert owner.has("servers.delete")
    assert op.has("commands.execute")
    assert not op.has("users.manage")
    assert view.has("monitoring.read")
    assert not view.has("commands.execute")


def test_telegram_actor_maps_to_legacy_owner(services):
    from src import database

    actor = store.actor_for_telegram(777, "TG")
    assert actor is not None
    assert actor.owner_id == 777
    services.servers.create_server(actor, alias="tgbox", hostname="h", user="u")
    # The legacy Telegram code path (database.get_server by telegram id) sees it.
    assert database.get_server(777, "tgbox")["alias"] == "tgbox"


# --- ownership isolation (IDOR) ---------------------------------------------------

def test_ownership_isolation_between_platform_accounts(services):
    owner = _owner(services)
    op = _operator(services)
    mine = services.servers.create_server(owner, alias="mine", hostname="h", user="u")
    # A low-privilege account gets 404 semantics for a foreign server.
    with pytest.raises(LookupError):
        services.servers.get_server(op, mine["id"])
    listed = {s["id"] for s in services.servers.list_servers(op)}
    assert mine["id"] not in listed


def test_admin_sees_all_servers(services):
    owner = _owner(services)
    op = _operator(services)
    services.servers.create_server(op, alias="opbox", hostname="h", user="u")
    seen = {s["alias"] for s in services.servers.list_servers(owner)}
    assert "opbox" in seen


def test_operator_cannot_delete(services):
    op = _operator(services)
    with pytest.raises(PermissionDenied):
        services.servers.delete_server(op, 99999)


# --- secrets never leave the service --------------------------------------------

def test_server_secrets_are_stripped_from_lists(services):
    owner = _owner(services)
    services.servers.create_server(owner, alias="sec", hostname="h", user="u", password="hunter2")
    for server in services.servers.list_servers(owner):
        assert "password" not in server
        assert "key_path" not in server


# --- command flow -----------------------------------------------------------------

def test_blocked_command_is_refused(services):
    owner = _owner(services)
    server = services.servers.create_server(owner, alias="b", hostname="h", user="u")
    plan = asyncio.run(services.commands.request(owner, server["id"], "rm -rf /"))
    assert plan.kind == "denied"
    assert plan.risk == "blocked"


def test_confirm_flow_single_use_and_expiry_bound(services):
    owner = _owner(services)
    server = services.servers.create_server(owner, alias="c", hostname="h", user="u")
    plan = asyncio.run(services.commands.request(owner, server["id"], "reboot now"))
    assert plan.kind == "confirm"
    token = plan.pending.token
    # Foreign actor cannot consume the token.
    op = _operator(services)
    with pytest.raises(ConfirmationError):
        asyncio.run(services.commands.confirm(op, token))
    # Owner confirms exactly once.
    job = asyncio.run(services.commands.confirm(owner, token))
    assert job.state.value in ("queued", "running", "done")
    with pytest.raises(ConfirmationError):
        asyncio.run(services.commands.confirm(owner, token))


def test_new_confirmation_supersedes_previous(services):
    owner = _owner(services)
    server = services.servers.create_server(owner, alias="d", hostname="h", user="u")
    plan1 = asyncio.run(services.commands.request(owner, server["id"], "reboot now"))
    plan2 = asyncio.run(services.commands.request(owner, server["id"], "reboot now"))
    with pytest.raises(ConfirmationError):
        asyncio.run(services.commands.confirm(owner, plan1.pending.token))
    job = asyncio.run(services.commands.confirm(owner, plan2.pending.token))
    assert job.id


def test_read_only_cannot_execute(services):
    owner = _owner(services)
    view = _viewer(services)
    server = services.servers.create_server(owner, alias="e", hostname="h", user="u")
    with pytest.raises(PermissionDenied):
        asyncio.run(services.commands.request(view, server["id"], "ls"))


def test_queue_full_maps_to_command_denied(services, monkeypatch):
    owner = _owner(services)
    server = services.servers.create_server(owner, alias="f", hostname="h", user="u")
    from src.jobs.errors import QueueFull

    async def _boom(spec, runner):
        raise QueueFull("full", user_id=1, limit=1)

    monkeypatch.setattr(services.commands.queue, "submit_nowait", _boom)
    with pytest.raises(CommandDenied):
        asyncio.run(services.commands.request(owner, server["id"], "ls"))


# --- link codes / bale bindings ---------------------------------------------------

def test_link_code_single_use(services):
    owner = _owner(services)
    code = services.security.create_link_code(owner, None)
    account = store.consume_link_code(code)
    assert account is not None and account.id == owner.account_id
    assert store.consume_link_code(code) is None  # burned


def test_bale_binding_roundtrip(services):
    owner = _owner(services)
    code = services.security.create_link_code(owner, None)
    result = services.security.link_bale(owner, code, "999", "Bale User")
    assert result["account_id"] == owner.account_id
    bound = store.get_bale_binding("999")
    assert bound is not None and bound.id == owner.account_id


# --- notifications ------------------------------------------------------------------

def test_notification_dedup_and_cooldown(services):
    async def _run():
        sent = []
        services.notifications.subscribe(lambda kind, payload: _async_collect(sent, kind, payload))
        await services.notifications.emit("cpu_high", scope="s1", cpu=95)
        await services.notifications.emit("cpu_high", scope="s1", cpu=96)  # deduped
        services.notifications.cooldown = 0.0
        await services.notifications.emit("cpu_high", scope="s1", cpu=97)  # passes after cooldown reset
        return sent

    async def _async_collect(sent, kind, payload):
        sent.append((kind, payload))

    sent = asyncio.run(_run())
    assert len(sent) == 2


def test_session_revocation(services):
    owner = _owner(services)
    token, _ = services.auth.login("root", "S3curePass!2026")
    assert store.resolve_session(token) is not None
    store.revoke_session(token)
    assert store.resolve_session(token) is None
