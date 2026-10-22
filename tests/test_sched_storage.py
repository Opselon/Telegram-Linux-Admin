# PURPOSE: Cover the scheduled_backups storage layer: CRUD round-trip,
#          ownership boundaries, corrupt-row tolerance and degrade-not-raise
#          behavior when the table/DB is unavailable.
# OWNER: lane `backup` of wave 3.4 (branch feat/3.4-backup).
# CONSUMES: src.sched.storage, src.sched.models, src.metrics.
#           Each test gets its own real sqlite3 file (tmp_path), drives it
#           directly with SCHEMA_SQL (no src.database state is touched).
# PROVIDES: tests/test_sched_storage.py.
# INVARIANTS: every mutation rejects a foreign owner with a plain "not found"
#             result (False/None, never an exception); a missing table
#             degrades to empty results + a counted error metric.
# EXTEND: new plan columns go here next to the scenario that pins them.

"""Storage tests for src.sched (real sqlite file per test)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.metrics import METRICS
from src.sched import storage
from src.sched.models import BackupPlan

OWNER_A = 101
OWNER_B = 202


@pytest.fixture(autouse=True)
def _reset_metrics():
    """Isolates the shared registry around each test."""
    before_counters = dict(METRICS._counters)
    before_gauges = dict(METRICS._gauges)
    yield
    METRICS._counters.clear()
    METRICS._counters.update(before_counters)
    METRICS._gauges.clear()
    METRICS._gauges.update(before_gauges)


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "schedules.db"


@pytest.fixture()
def conn(db_path: Path):
    """A scoped sqlite connection whose schema is only what this lane ships."""
    sqlite_conn = sqlite3.connect(db_path)
    sqlite_conn.execute(storage.SCHEMA_SQL)
    yield sqlite_conn
    sqlite_conn.close()


@pytest.fixture()
def wired(conn, monkeypatch):
    """Points the storage module's lazy db accessor at the test connection."""
    monkeypatch.setattr(storage, "_connect", lambda: conn)
    return conn


def _plan(**overrides) -> BackupPlan:
    base = dict(alias="web", hour=3, keep=7, owner_id=OWNER_A)
    base.update(overrides)
    return BackupPlan(**base)


# -- CRUD round-trip -------------------------------------------------------------


def test_add_then_list_round_trips_every_field(wired, conn) -> None:
    saved = storage.add_plan(_plan())

    assert saved.id > 0
    plans = storage.list_plans(OWNER_A)
    assert len(plans) == 1
    assert plans[0] == saved
    assert plans[0].alias == "web"
    assert plans[0].hour == 3
    assert plans[0].keep == 7
    assert plans[0].enabled is True


def test_list_scopes_to_the_owner(wired) -> None:
    storage.add_plan(_plan(alias="mine", hour=1))
    storage.add_plan(_plan(alias="theirs", hour=2, owner_id=OWNER_B))

    mine = storage.list_plans(OWNER_A)
    theirs = storage.list_plans(OWNER_B)

    assert [plan.alias for plan in mine] == ["mine"]
    assert [plan.alias for plan in theirs] == ["theirs"]


def test_list_filters_by_alias_and_enabled(wired) -> None:
    storage.add_plan(_plan(alias="web", hour=1, enabled=True))
    storage.add_plan(_plan(alias="web", hour=4, enabled=False))
    storage.add_plan(_plan(alias="db", hour=5, enabled=True))

    assert [plan.hour for plan in storage.list_plans(OWNER_A, alias="web")] == [1, 4]
    assert [plan.hour for plan in storage.list_plans(OWNER_A, enabled=True)] == [1, 5]
    assert [plan.hour for plan in storage.list_plans(OWNER_A, alias="web", enabled=False)] == [4]
    assert storage.list_plans(OWNER_A, alias="nope") == []


def test_set_plan_updates_fields_only_for_the_owner(wired) -> None:
    saved = storage.add_plan(_plan())

    updated = BackupPlan(
        alias="web", hour=10, keep=14, enabled=False, owner_id=OWNER_A, id=saved.id
    )
    assert storage.set_plan(updated) is True

    fetched = storage.get_plan(saved.id, OWNER_A)
    assert fetched is not None and fetched.hour == 10
    assert fetched.enabled is False and fetched.keep == 14

    foreign = BackupPlan(
        alias="web", hour=11, keep=1, enabled=True, owner_id=OWNER_B, id=saved.id
    )
    assert storage.set_plan(foreign) is False, "a foreign owner cannot mutate a plan"
    assert storage.get_plan(saved.id, OWNER_A).hour == 10  # type: ignore[union-attr]


def test_get_and_delete_enforce_ownership(wired) -> None:
    saved = storage.add_plan(_plan())

    assert storage.get_plan(saved.id, OWNER_B) is None
    assert storage.delete_plan(saved.id, OWNER_B) is False
    assert storage.get_plan(saved.id, OWNER_A) is not None
    assert storage.delete_plan(saved.id, OWNER_A) is True
    assert storage.get_plan(saved.id, OWNER_A) is None
    assert storage.list_plans(OWNER_A) == []


def test_get_unknown_id_is_none_without_raising(wired) -> None:
    assert storage.get_plan(9999, OWNER_A) is None


def test_schema_sql_is_idempotent(wired, conn) -> None:
    storage.add_plan(_plan())
    conn.execute(storage.SCHEMA_SQL)  # re-running must not error or wipe
    assert len(storage.list_plans(OWNER_A)) == 1


# -- corrupt rows --------------------------------------------------------------


def test_a_corrupt_row_is_skipped_and_counted(wired, conn) -> None:
    storage.add_plan(_plan(alias="good", hour=1))
    # Slip in a row with a non-numeric hour that violates the model.
    conn.execute(
        "INSERT INTO scheduled_backups (owner_id, alias, hour, keep, enabled)"
        " VALUES (?, ?, ?, ?, ?)",
        (OWNER_A, "corrupt", "not-a-number", 7, 1),
    )

    plans = storage.list_plans(OWNER_A)

    assert [plan.alias for plan in plans] == ["good"]
    assert METRICS.get("sched_storage_corrupt_row_total") == 1.0


# -- degradation (law L5): missing table / missing DB -----------------------------


def test_missing_table_returns_empty_and_counts_and_does_not_raise(
    db_path: Path, monkeypatch
) -> None:
    """The DB file exists but the table was never created (fresh legacy DB)."""
    bare = sqlite3.connect(db_path)
    monkeypatch.setattr(storage, "_connect", lambda: bare)

    assert storage.list_plans(OWNER_A) == []
    assert storage.get_plan(1, OWNER_A) is None
    assert storage.delete_plan(1, OWNER_A) is False
    assert storage.set_plan(_plan(id=1)) is False
    assert METRICS.get("sched_storage_errors_total", op="list") == 1.0
    bare.close()


def test_unopenable_db_degrades_to_empty(wired, monkeypatch, tmp_path) -> None:
    """get_db_connection blows up entirely (disk gone): callers see empty data."""
    def broken():
        raise OSError("disk unavailable")

    monkeypatch.setattr(storage, "_connect", broken)

    assert storage.list_plans(OWNER_A) == []
    assert storage.add_plan(_plan()) == _plan(), "add returns the input plan, id 0"
    assert storage.add_plan(_plan()).id == 0


def test_schema_sql_matches_the_models_contract(wired) -> None:
    """The SCHEMA_SQL text the orchestrator wires must carry the columns."""
    assert "scheduled_backups" in storage.SCHEMA_SQL
    for column in ("owner_id", "alias", "hour", "keep", "enabled"):
        assert column in storage.SCHEMA_SQL
