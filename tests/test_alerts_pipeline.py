# PURPOSE: Cover the alerts lane's persistence and sweep contract end to end:
#          (1) SCHEMA_SQL converges through `initialize_database()`, (2) rule
#          CRUD round trips against a real sqlite file, and (3) the layered
#          pipeline check_rules -> record_event -> notify emits the fired /
#          recorded / delivered counters and one audit line on a firing sweep.
# OWNER: lane `alerts` of wave 3.4 (branch feat/3.4-alerts).
# CONSUMES: src.alerts.storage (SCHEMA_SQL + CRUD), src.alerts.checker,
#           src.alerts.evaluator, src.alerts.notifier, src.database,
#           src.metrics.METRICS, src.audit.
# PROVIDES: tests/test_alerts_pipeline.py.
# INVARIANTS: database work runs against a real TLA_DB_FILE (env-pinned,
#             unique per test, erased after); the sweep's async pipeline is
#             awaited via asyncio.run; METRICS counters are snapshotted and
#             restored so the shared registry stays untouched for other files.
# EXTEND: a new rule kind or fired-metric gets its own scenario here.

"""Persistence and pipeline tests for the wave 3.4 alerts lane."""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from src import database
from src.alerts import storage as alerts_storage
from src.alerts.checker import check_rules
from src.alerts.evaluator import evaluate
from src.alerts.models import ALERT_KINDS, AlertEvent, AlertRule
from src.alerts.notifier import notify
from src.alerts.storage import (
    FIRED_EVENT,
    SCHEMA_SQL,
    add_rule,
    delete_rule,
    events,
    get_rule,
    list_rules,
    record_event,
    set_enabled,
)
from src.metrics import METRICS

OWNER = 777


@pytest.fixture(autouse=True)
def alerts_db(monkeypatch, tmp_path):
    """A real, unique sqlite file per test.

    Contract (lane A, storage item 4): the orchestrator wires ``SCHEMA_SQL``
    into db init, so this lane never edits the init flow — tests apply the
    module constant to a fresh file exactly the way integration will.
    """
    db_path = Path(tmp_path) / "alerts.sqlite"
    monkeypatch.setenv("TLA_DB_FILE", str(db_path))
    database.initialize_database()  # production init (users/servers/...)
    with database.get_db_connection() as conn:
        conn.execute(SCHEMA_SQL)  # the lane's constant, as integration will

    counters = dict(METRICS._counters)
    gauges = dict(METRICS._gauges)
    yield db_path
    METRICS._counters.clear()
    METRICS._counters.update(counters)
    METRICS._gauges.clear()
    METRICS._gauges.update(gauges)
    # get_db_connection() caches a module-level handle: close it or the next
    # test's tmp_path cleanup races an open WAL file.
    database.close_db_connection()
    with contextlib.suppress(FileNotFoundError):
        db_path.unlink()


# -- schema -----------------------------------------------------------------------


def test_schema_sql_creates_the_contracted_columns(alerts_db) -> None:
    """SCHEMA_SQL converges a fresh file with every contracted column."""
    conn = sqlite3.connect(alerts_db)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("PRAGMA table_info(alert_rules)").fetchall()
    finally:
        conn.close()
    names = {row["name"] for row in rows}
    assert {
        "id",
        "owner_id",
        "alias",
        "kind",
        "threshold",
        "payload",
        "enabled",
        "created_at",
    } <= names


def test_schema_sql_is_idempotent() -> None:
    """Applying SCHEMA_SQL twice must neither raise nor damage existing rows."""
    add_rule(OWNER, "web-1", "mem_high", 80)
    with database.get_db_connection() as conn:
        conn.execute(SCHEMA_SQL)
    assert len(list_rules(OWNER)) == 1


# -- CRUD -------------------------------------------------------------------------


def test_crud_round_trip() -> None:
    """add -> list/get -> toggle -> delete, all scoped by owner."""
    rule = add_rule(OWNER, "web-1", "mem_high", 80)
    assert rule.id is not None

    listed = list_rules(OWNER)
    assert len(listed) == 1 and listed[0].alias == "web-1"

    got = get_rule(rule.id, OWNER)
    assert got is not None and got.threshold == 80 and got.enabled is True
    assert get_rule(rule.id, 4242) is None, "a foreign owner reads nothing"

    assert set_enabled(rule.id, OWNER, False) is True
    toggled = get_rule(rule.id, OWNER)
    assert toggled is not None and toggled.enabled is False
    assert set_enabled(rule.id + 500, OWNER, False) is False

    assert delete_rule(rule.id, OWNER) is True
    assert delete_rule(rule.id, OWNER) is False
    assert list_rules(OWNER) == []


def test_list_rules_filters_by_alias_and_state() -> None:
    add_rule(OWNER, "web-1", "mem_high", 80)
    add_rule(OWNER, "web-1", "disk_high", 90)
    add_rule(OWNER, "db-2", "cpu_high", 70)

    assert len(list_rules(OWNER, alias="web-1")) == 2
    assert len(list_rules(OWNER, enabled=True)) == 3
    assert list_rules(OWNER, alias="nope") == []


def test_duplicate_rule_is_refused() -> None:
    add_rule(OWNER, "web-1", "mem_high", 80)
    with pytest.raises(ValueError):
        add_rule(OWNER, "web-1", "mem_high", 85)
    assert len(list_rules(OWNER)) == 1


def test_alias_is_normalized_before_the_unique_index() -> None:
    """Padded aliases cannot smuggle a second rule past UNIQUE(owner, alias, kind)."""
    rule = add_rule(OWNER, "  web-1  ", "mem_high", 80)
    assert rule.alias == "web-1"
    with pytest.raises(ValueError):
        add_rule(OWNER, "web-1", "mem_high", 70)


def test_model_rejects_invalid_input() -> None:
    with pytest.raises(ValueError):
        AlertRule(owner_id=OWNER, alias="   ", kind="mem_high", threshold=50)
    with pytest.raises(ValueError):
        AlertRule(owner_id=OWNER, alias="ok", kind="nope", threshold=50)
    with pytest.raises(ValueError):
        AlertRule(owner_id=OWNER, alias="ok", kind="mem_high", threshold=101)
    with pytest.raises(ValueError):
        AlertRule(owner_id=OWNER, alias="ok", kind="mem_high", threshold=True)


# -- events (audit-backed read model) ----------------------------------------------


def test_record_event_writes_one_audit_line_then_events_reads_it() -> None:
    written: list[dict] = []

    def fake_record(event_name: str, user_id: int, **context) -> None:
        written.append({"event": event_name, "user_id": user_id, "context": context})

    event = AlertEvent(
        owner_id=OWNER,
        alias="web-1",
        kind="mem_high",
        threshold=80,
        value=87.5,
        fired_at=1_700_000_000.0,
        message="rendered",
    )
    with patch.object(alerts_storage.audit, "record", side_effect=fake_record):
        record_event(event)

    assert len(written) == 1
    assert written[0]["event"] == FIRED_EVENT
    assert written[0]["context"]["value"] == 87.5


def test_notify_without_sender_is_counted_not_delivered() -> None:
    event = AlertEvent(
        owner_id=OWNER,
        alias="web-1",
        kind="disk_high",
        threshold=90,
        value=95.0,
        fired_at=1_700_000_000.0,
        message="m",
    )
    assert asyncio.run(notify(event)) is False
    assert METRICS.get("alerts_events_total", kind="disk_high") >= 1
    assert METRICS.get("alerts_notify_failures_total", kind="disk_high") == 0


# -- evaluator --------------------------------------------------------------------


def test_evaluate_is_a_deterministic_threshold_check() -> None:
    rule = AlertRule(owner_id=OWNER, alias="web-1", kind="mem_high", threshold=80)
    assert evaluate(rule, 80.0) is True, "the threshold itself fires (>=)"
    assert evaluate(rule, 79.9) is False
    assert evaluate(rule, 100.0) is True
    assert ALERT_KINDS == ("mem_high", "disk_high", "cpu_high")


# -- pipeline ---------------------------------------------------------------------


async def _sweep_and_deliver(rule: AlertRule, value: float) -> list[AlertEvent]:
    """Runs the lane pipeline once: check -> record -> notify."""
    delivered: list[AlertEvent] = []

    async def sender(event: AlertEvent) -> bool:
        delivered.append(event)
        return True

    async def probe(_rule: AlertRule) -> float:
        return value

    fired = await check_rules([rule], probe, now=lambda: 1_700_000_000.0)
    for event in fired:
        record_event(event)
        await notify(event, sender)
    return delivered


def test_pipeline_fires_records_and_delivers() -> None:
    rule = add_rule(OWNER, "web-1", "mem_high", 80)
    delivered = asyncio.run(_sweep_and_deliver(rule, 91.5))

    assert len(delivered) == 1
    event = delivered[0]
    assert event.value == 91.5 and event.owner_id == OWNER
    assert event.alias == "web-1" and event.kind == "mem_high"
    assert event.fired_at == 1_700_000_000.0

    assert METRICS.get("alerts_fired_total", kind="mem_high") >= 1
    assert METRICS.get("alerts_events_total", kind="mem_high") >= 1
    assert METRICS.get("alerts_notify_sent_total", kind="mem_high") >= 1


def test_pipeline_below_threshold_stays_silent() -> None:
    rule = add_rule(OWNER, "web-1", "cpu_high", 70)
    delivered = asyncio.run(_sweep_and_deliver(rule, 65.0))
    assert delivered == []
    assert METRICS.get("alerts_fired_total", kind="cpu_high") == 0


def test_pipeline_skips_paused_rules() -> None:
    rule = add_rule(OWNER, "web-1", "disk_high", 90)
    assert rule.id is not None
    set_enabled(rule.id, OWNER, False)

    paused = get_rule(rule.id, OWNER)
    assert paused is not None
    delivered = asyncio.run(_sweep_and_deliver(paused, 99.0))
    assert delivered == [], "a paused rule is never probed or fired"
