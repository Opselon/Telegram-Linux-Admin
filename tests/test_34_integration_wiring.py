"""Integration tests for the 3.4 wiring (alerts + scheduled backups).

These hit the seams the lane tests cannot: schema registration through the
database init hook, handler registration on the real Application builder,
and the ops scheduler gaining the two feature cadences. They deliberately
avoid Telegram I/O — the fakes here only record calls.
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any

import pytest

from src.alerts import storage as alert_storage
from src.sched import storage as sched_storage


# -- schema -----------------------------------------------------------------
def test_feature_schema_is_registered_and_idempotent(monkeypatch, tmp_path):
    """Importing lifecycle registers alert + backup tables exactly once."""
    from src import database

    monkeypatch.setattr(database, "_conn", None, raising=False)
    monkeypatch.setattr(database, "_conn_path", None, raising=False)
    monkeypatch.setenv("TLA_DB_FILE", str(tmp_path / "feature-schema.db"))

    # importing lifecycle performs the initial registration
    import src.bot.lifecycle as lifecycle

    before = len(database._extra_schema_stmts)
    assert before >= 3  # alerts + scheduled_backups + index
    # re-running the registrar must not duplicate statements
    lifecycle._register_feature_schema()
    assert len(database._extra_schema_stmts) == before

    database.initialize_database()
    tables = {
        row[0]
        for row in database.get_db_connection()
        .execute("SELECT name FROM sqlite_master WHERE type='table'")
        .fetchall()
    }
    assert "alert_rules" in tables
    assert "scheduled_backups" in tables


def test_alert_schema_matches_storage_expectations():
    """The registered SQL is the storage module's own constant."""
    from src import database

    assert alert_storage.SCHEMA_SQL in database._extra_schema_stmts
    assert sched_storage.SCHEMA_SQL in database._extra_schema_stmts


# -- handler registration ---------------------------------------------------
def test_new_commands_and_callbacks_are_registered(monkeypatch):
    from src.bot import application as app_module
    from src.bot.application import build_application

    monkeypatch.setattr(
        app_module.config, "telegram_token", "123456:TESTTOKEN", raising=False
    )
    app = build_application()
    commands = set()
    patterns: list[str] = []
    for group in app.handlers.values():
        for handler in group:
            command = getattr(handler, "commands", None)
            if command:
                commands.update(command)
            pattern = getattr(handler, "pattern", None)
            if pattern is not None:
                patterns.append(pattern.pattern)

    assert {"alerts", "backups"} <= commands
    for expected in (
        r"^alertsmenu_",
        r"^alertsadd_",
        r"^alertstoggle_",
        r"^alertsdel_",
        r"^schedtoggle_",
        r"^scheddel_",
        r"^schedrun_",
        r"^schedadd_alias_",
        r"^schedadd_hour_",
        r"^schedadd_keep_",
    ):
        assert expected in patterns, expected


def test_backup_confirm_pattern_does_not_shadow_stage_prefixes():
    """schedadd_alias_/hour_/keep_ must not be swallowed by the confirm regex."""
    import re

    confirm = re.compile(r"^schedadd_[^_]+_[0-9]+_[0-9]+$")
    for stage in ("schedadd_alias_web1", "schedadd_hour_web1_04", "schedadd_keep_web1_4_7"):
        assert not confirm.match(stage), stage
    assert confirm.match("schedadd_web1_4_7")


# -- scheduler cadence ------------------------------------------------------
def test_ops_scheduler_registers_feature_cadences(monkeypatch):
    """post_init gives the scheduler the alerts + backups tasks."""
    from src.bot import lifecycle, state

    added: list[str] = []

    class FakeScheduler:
        def add(self, name: str, interval: float, fn: Any) -> None:
            added.append(name)

        def start(self) -> None:
            pass

        def status(self) -> dict[str, Any]:
            return {name: None for name in added}

        async def stop(self) -> None:
            pass

    monkeypatch.setattr(lifecycle, "Scheduler", FakeScheduler)
    monkeypatch.setattr(state, "ops_scheduler", None, raising=False)

    async def fake_register_defaults(scheduler, **kwargs):
        scheduler.add("reaper", 3600.0, lambda: None)

    monkeypatch.setattr(lifecycle, "register_defaults", fake_register_defaults)
    monkeypatch.setattr(lifecycle, "_wire_backup_run_cmd", lambda: None)

    asyncio.run(lifecycle._start_ops_scheduler(application=None))

    assert "alerts" in added
    assert "backups" in added


# -- backup run-cmd port ----------------------------------------------------
def test_run_cmd_port_is_wired_to_the_archive_builder(monkeypatch, tmp_path):
    """_wire_backup_run_cmd points the sched UI at create_backup_archive."""
    from src.bot import lifecycle
    from src.sched import ui as sched_ui

    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.json").write_text("{}")
    (tmp_path / "database.db").write_text("x")

    called: dict[str, Any] = {}

    async def fake_archive(*, destination_dir=None):
        called["destination_dir"] = destination_dir
        path = tmp_path / "archive.zip"
        path.write_bytes(b"PK")
        return str(path)

    monkeypatch.setattr("src.bot.backup.create_backup_archive", fake_archive)

    lifecycle._wire_backup_run_cmd()
    run_cmd = sched_ui.get_run_cmd()
    assert run_cmd is not None

    result = asyncio.run(run_cmd("web1"))
    assert str(result).endswith("archive.zip")
    assert "scheduled" in str(called["destination_dir"])


# -- alert sweep ------------------------------------------------------------
def test_alerts_sweep_skips_remote_aliases_and_reads_local_health(monkeypatch):
    """Remote rules are skipped (counted); local rules read real metrics."""
    from src.bot import lifecycle
    from src.metrics import METRICS

    def fake_health():
        return {"memory_percent": 91.0, "disk_percent": 10.0, "cpu_percent": 5.0}

    monkeypatch.setattr("src.database.get_system_health", fake_health)
    monkeypatch.setattr(lifecycle.state, "SELF_ALIAS", "self", raising=False)

    remote = type("R", (), {"alias": "web1", "kind": "mem_high"})()
    assert asyncio.run(lifecycle._alerts_probe(remote)) is None

    local = type("R", (), {"alias": "self", "kind": "mem_high"})()
    assert asyncio.run(lifecycle._alerts_probe(local)) == 91.0
