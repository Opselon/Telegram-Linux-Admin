"""Watchdog tests: offline detection, recovery alerts, owner resolution."""

from __future__ import annotations

import asyncio

import pytest

from src import database
from src.services import build_services, store
from src.services.migrations import run_migrations
from src.services.watchdog import ServerWatchdog


@pytest.fixture()
def services(tmp_path, monkeypatch):
    monkeypatch.setenv("TLA_DB_FILE", str(tmp_path / "wd.db"))
    monkeypatch.setenv("TLA_ENCRYPTION_KEY_FILE", str(tmp_path / "keys.key"))
    monkeypatch.setenv("TLA_AUDIT_FILE", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("TLA_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("TLA_ACCESS_MODE", "open")
    database.close_db_connection()
    run_migrations(snapshot=False)
    yield build_services()
    database.close_db_connection()


def _owner(services):
    token, _ = services.auth.bootstrap_owner("root", "S3curePass!2026")
    return store.actor_from_account(store.resolve_session(token))


def test_watchdog_emits_offline_then_recovery(services, monkeypatch):
    actor = _owner(services)
    server = services.servers.create_server(actor, alias="wd1", hostname="h", user="u")
    watchdog = ServerWatchdog(services, interval=3600)

    calls: list[str] = []

    async def fake_execute(owner_id, alias, command, timeout=60):
        if command == "true":
            if not calls or calls[-1] == "fail":
                raise ConnectionError("down")
            return ("", "", 0)
        return ("", "", 0)

    monkeypatch.setattr(services.manager, "execute", fake_execute)

    # First check: probe fails (first call raises) → offline alert.
    stats = asyncio.run(watchdog.check_all())
    assert stats["offline"] == 1
    # Second check: still failing → deduped, no new alert.
    stats = asyncio.run(watchdog.check_all())
    assert stats["offline"] == 1
    # Probe passes now → recovery.
    calls.append("ok")
    stats = asyncio.run(watchdog.check_all())
    assert stats["online"] == 1 and stats["offline"] == 0
    assert server["id"] not in watchdog._offline


def test_watchdog_skips_unknown_owners(services):
    # A server row whose owner account is gone must not crash the loop.
    database.add_user(31337)
    database.add_server(31337, "orphan", "h", "u")
    watchdog = ServerWatchdog(services, interval=3600)
    stats = asyncio.run(watchdog.check_all())
    assert stats["online"] + stats["offline"] == 1  # it probed via the telegram account


def test_watchdog_start_stop_cycle(services):
    async def _run():
        watchdog = ServerWatchdog(services, interval=3600)
        await watchdog.start()
        assert watchdog._task is not None and not watchdog._task.done()
        await watchdog.stop()
        assert watchdog._task is None

    asyncio.run(_run())
