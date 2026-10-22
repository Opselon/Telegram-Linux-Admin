# PURPOSE: Cover the wave 3.4 alerts lane end to end: rule model validation,
#          the pure evaluator, the crash-proof checker (fires / skips on None
#          / skips on raise / counts metrics), storage CRUD with the
#          ownership boundary, the audit-backed event read model, and the
#          transport-free notifier contract.
# OWNER: lane `alerts` of wave 3.4 (branch feat/3.4-alerts).
# CONSUMES: src.alerts (models/evaluator/checker/storage/notifier),
#           src.metrics.METRICS, src.database (with mocked connection),
#           src.audit.audit (disabled; records are inspected via tail()).
# PROVIDES: tests/test_alerts_unit.py.
# INVARIANTS: every probe failure mode is exercised through a real
#             asyncio-to-sqlite path; METRICS counters are restored after
#             each test so the shared registry stays untouched for others;
#             timestamps are injected, never slept for (contract law L3).
# EXTEND: new alerts behavior goes here next to the scenario that pins it.

"""Unit tests for the wave 3.4 alerts package (models/evaluator/checker/
storage/notifier)."""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from unittest.mock import patch

import pytest

from src.alerts.checker import check_rules
from src.alerts.evaluator import evaluate
from src.alerts.models import ALERT_KINDS, AlertEvent, AlertRule
from src.alerts.notifier import format_ts, notify, render_event_text
from src.alerts.storage import (
    DataStoreError,
    add_rule,
    delete_rule,
    events,
    get_rule,
    list_rules,
    record_event,
    set_enabled,
)
from src.metrics import METRICS

OWNER_A = 4242
OWNER_B = 4343

#: a deterministic clock every test pins instead of sleeping (law L3).
T0 = 1_700_000_000.0

RULE = AlertRule(
    owner_id=OWNER_A, alias="web-1", kind="mem_high", threshold=80
)


@pytest.fixture(autouse=True)
def _restore_metrics_and_audit():
    """Snapshots the shared counters and disables the disk audit log."""
    before_counters = dict(METRICS._counters)
    before_gauges = dict(METRICS._gauges)
    yield
    METRICS._counters.clear()
    METRICS._counters.update(before_counters)
    METRICS._gauges.clear()
    METRICS._gauges.update(before_gauges)


# -- models ---------------------------------------------------------------------


def test_rule_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError):
        AlertRule(owner_id=OWNER_A, alias="x", kind="gpu_high", threshold=50)


def test_rule_rejects_out_of_range_threshold() -> None:
    for bad in (-1, 101):
        with pytest.raises(ValueError):
            AlertRule(owner_id=OWNER_A, alias="x", kind="mem_high", threshold=bad)


def test_rule_rejects_non_int_threshold_and_owner() -> None:
    with pytest.raises(ValueError):
        AlertRule(owner_id=OWNER_A, alias="x", kind="mem_high", threshold=80.5)
    with pytest.raises(ValueError):
        AlertRule(owner_id=None, alias="x", kind="mem_high", threshold=50)


def test_rule_rejects_empty_alias_and_non_bool_enabled() -> None:
    with pytest.raises(ValueError):
        AlertRule(owner_id=OWNER_A, alias="  ", kind="mem_high", threshold=50)
    with pytest.raises(ValueError):
        AlertRule(owner_id=OWNER_A, alias="x", kind="mem_high", threshold=50, enabled=1)


def test_rule_strips_alias_and_keeps_payload_none() -> None:
    rule = AlertRule(owner_id=OWNER_A, alias=" web-1 ", kind="cpu_high", threshold=90)
    assert rule.alias == "web-1"
    assert rule.payload is None


def test_event_rejects_bad_value_and_fired_at() -> None:
    with pytest.raises(ValueError):
        AlertEvent(
            owner_id=OWNER_A, alias="x", kind="mem_high", threshold=80,
            value="99", fired_at=T0,
        )
    with pytest.raises(ValueError):
        AlertEvent(
            owner_id=OWNER_A, alias="x", kind="mem_high", threshold=80,
            value=99.0, fired_at="now",
        )


def test_alert_kinds_fixture_matches_contract() -> None:
    assert ALERT_KINDS == ("mem_high", "disk_high", "cpu_high")


# -- evaluator -------------------------------------------------------------------


def test_evaluator_fires_at_threshold_and_above() -> None:
    assert evaluate(RULE, 80.0) is True, "== threshold must fire"
    assert evaluate(RULE, 80.1) is True
    assert evaluate(RULE, 100.0) is True


def test_evaluator_does_not_fire_below() -> None:
    assert evaluate(RULE, 79.9) is False
    assert evaluate(RULE, 0.0) is False


def test_evaluator_none_and_nan_never_fire() -> None:
    assert evaluate(RULE, None) is False
    nan = float("nan")
    assert evaluate(RULE, nan) is False


def test_evaluator_is_pure_over_the_rule() -> None:
    before = (RULE.threshold, RULE.kind)
    evaluate(RULE, 99.0)
    assert (RULE.threshold, RULE.kind) == before


# -- checker ----------------------------------------------------------------------


def _now(clock_box: dict[str, float]):
    def clock() -> float:
        return clock_box["t"]

    return clock


def test_check_rules_fires_per_rule_and_orders_sweep() -> None:
    box = {"t": T0}

    async def probe(rule: AlertRule) -> float | None:
        return 99.0

    rules = [
        AlertRule(owner_id=OWNER_A, alias="web-1", kind="mem_high", threshold=50),
        AlertRule(owner_id=OWNER_A, alias="web-2", kind="disk_high", threshold=90),
    ]
    fired = asyncio.run(check_rules(rules, probe, now=_now(box)))
    assert [event.alias for event in fired] == ["web-1", "web-2"]
    assert all(event.value == 99.0 for event in fired)
    assert all(event.fired_at == T0 for event in fired)
    assert METRICS.get("alerts_fired_total", kind="mem_high") >= 1
    assert METRICS.get("alerts_fired_total", kind="disk_high") >= 1


def test_check_rules_skips_none_reading() -> None:
    async def probe(rule: AlertRule) -> float | None:
        return None

    fired = asyncio.run(check_rules([RULE], probe, now=_now({"t": T0})))
    assert fired == []
    assert METRICS.get("alerts_probe_skips_total", kind="mem_high") >= 1


def test_check_rules_skips_raising_probe_and_counts_metric() -> None:
    async def probe(rule: AlertRule) -> float | None:
        raise RuntimeError("ssh down")

    fired = asyncio.run(check_rules([RULE], probe, now=_now({"t": T0})))
    assert fired == []
    assert METRICS.get("alerts_probe_failures_total", kind="mem_high") >= 1


def test_check_rules_never_raises_into_the_loop() -> None:
    """A probe that raises for one rule must not stop the remaining rules."""
    cpu_rule = AlertRule(owner_id=OWNER_A, alias="web-3", kind="cpu_high", threshold=10)

    async def bad_then_good(rule: AlertRule) -> float | None:
        if rule.kind == "cpu_high":
            return float(rule.threshold)  # exact boundary -> fires
        raise RuntimeError("boom")

    fired = asyncio.run(
        check_rules([RULE, cpu_rule], bad_then_good, now=_now({"t": T0}))
    )
    assert [event.kind for event in fired] == ["cpu_high"], (
        "one broken probe must not stop the other rules in the sweep"
    )


def test_check_rules_ignores_disabled_rules_without_probing() -> None:
    calls: list[str] = []

    async def probe(rule: AlertRule) -> float | None:
        calls.append(rule.alias)
        return 100.0

    disabled = AlertRule(
        owner_id=OWNER_A, alias="web-4", kind="mem_high", threshold=10, enabled=False
    )
    fired = asyncio.run(check_rules([disabled], probe, now=_now({"t": T0})))
    assert fired == []
    assert calls == [], "a muted rule must not even be probed"


def test_check_rules_treats_nonnumeric_probe_return_as_failure() -> None:
    async def probe(rule: AlertRule) -> float | None:
        return "99"  # type: ignore[return-value]

    fired = asyncio.run(check_rules([RULE], probe, now=_now({"t": T0})))
    assert fired == []
    assert METRICS.get("alerts_probe_failures_total", kind="mem_high") >= 1


def test_event_message_is_the_rendered_template() -> None:
    box = {"t": T0}

    async def probe(rule: AlertRule) -> float | None:
        return 83.5

    fired = asyncio.run(check_rules([RULE], probe, now=_now(box)))
    line = fired[0].message
    assert "web-1" in line and "80" in line


# -- storage: a real sqlite connection with the schema applied ---------------------

@pytest.fixture()
def store(monkeypatch):
    """Isolated in-memory sqlite driver exercising the real storage SQL."""
    conn = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    from src.alerts.storage import SCHEMA_INDEX_SQL, SCHEMA_SQL

    conn.execute(SCHEMA_SQL)
    conn.execute(SCHEMA_INDEX_SQL)
    monkeypatch.setattr("src.alerts.storage.get_db_connection", lambda: conn)
    # transaction() also reads module state; point it at the same connection.
    import contextlib

    @contextlib.contextmanager
    def fake_transaction():
        yield conn

    monkeypatch.setattr("src.alerts.storage.transaction", fake_transaction)
    yield conn
    conn.close()


def test_add_and_list_round_trip(store) -> None:
    rule = add_rule(OWNER_A, "web-1", "mem_high", 80)
    assert rule.id is not None and rule.enabled is True
    listed = list_rules(OWNER_A)
    assert len(listed) == 1 and listed[0].alias == "web-1"
    assert listed[0].threshold == 80


def test_add_validates_before_writing(store) -> None:
    with pytest.raises(ValueError):
        add_rule(OWNER_A, "web-1", "gpu_high", 80)
    with pytest.raises(ValueError):
        add_rule(OWNER_A, "web-1", "mem_high", 101)
    assert list_rules(OWNER_A) == [], "an invalid rule must never reach the table"


def test_add_duplicate_alias_kind_refused(store) -> None:
    add_rule(OWNER_A, "web-1", "mem_high", 80)
    with pytest.raises(ValueError):
        add_rule(OWNER_A, "web-1", "mem_high", 70)


def test_list_rules_filters_by_alias_and_enabled(store) -> None:
    add_rule(OWNER_A, "web-1", "mem_high", 80)
    add_rule(OWNER_A, "web-1", "cpu_high", 70)
    db_rule = add_rule(OWNER_A, "web-2", "disk_high", 60)
    set_enabled(db_rule.id, OWNER_A, False)

    assert len(list_rules(OWNER_A, alias="web-1")) == 2
    enabled_only = list_rules(OWNER_A, enabled=True)
    assert all(rule.enabled for rule in enabled_only)
    assert len(enabled_only) == 2


def test_list_rules_never_returns_foreign_rows(store) -> None:
    add_rule(OWNER_A, "web-1", "mem_high", 80)
    assert list_rules(OWNER_B) == []


def test_toggle_ownership_boundary(store) -> None:
    rule = add_rule(OWNER_A, "web-1", "mem_high", 80)
    assert set_enabled(rule.id, OWNER_B, False) is False, (
        "another user's toggle must not touch the row"
    )
    assert store.execute(
        "SELECT enabled FROM alert_rules WHERE id = ?", (rule.id,)
    ).fetchone()["enabled"] == 1
    assert set_enabled(rule.id, OWNER_A, False) is True
    assert get_rule(rule.id, OWNER_A).enabled is False


def test_delete_ownership_boundary(store) -> None:
    rule = add_rule(OWNER_A, "web-1", "mem_high", 80)
    assert delete_rule(rule.id, OWNER_B) is False
    assert get_rule(rule.id, OWNER_B) is None, "a foreign read is invisible"
    assert delete_rule(rule.id, OWNER_A) is True
    assert get_rule(rule.id, OWNER_A) is None


def test_get_rule_scopes_by_owner(store) -> None:
    rule = add_rule(OWNER_A, "web-1", "mem_high", 80)
    assert get_rule(rule.id, OWNER_B) is None
    assert get_rule(99999, OWNER_A) is None
    fetched = get_rule(rule.id, OWNER_A)
    assert fetched == rule


def test_storage_degrades_when_table_is_missing(monkeypatch) -> None:
    """A first boot without the table reads as empty and refuses writes."""
    conn = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row

    @contextlib.contextmanager
    def fake_transaction():
        yield conn

    monkeypatch.setattr("src.alerts.storage.get_db_connection", lambda: conn)
    monkeypatch.setattr("src.alerts.storage.transaction", fake_transaction)
    assert list_rules(OWNER_A) == []
    assert get_rule(1, OWNER_A) is None
    assert set_enabled(1, OWNER_A, False) is False
    assert delete_rule(1, OWNER_A) is False
    conn.close()


def test_add_rule_raises_typed_error_when_store_is_down(monkeypatch) -> None:
    """An unusable store is a typed, catchable failure — not a raw sqlite error."""

    @contextlib.contextmanager
    def broken_transaction():
        raise sqlite3.OperationalError("disk full")
        yield None  # pragma: no cover - unreachable, keeps the CM shape

    monkeypatch.setattr("src.alerts.storage.transaction", broken_transaction)
    with pytest.raises(DataStoreError):
        add_rule(OWNER_A, "web-1", "mem_high", 80)


# -- events read model -------------------------------------------------------------


def test_record_event_then_events_round_trip(tmp_path, monkeypatch) -> None:
    from src.audit import AuditLogger

    audit_log = AuditLogger(tmp_path / "audit", filename="audit.log")
    monkeypatch.setattr("src.alerts.storage.audit", audit_log)

    event = AlertEvent(
        owner_id=OWNER_A, alias="web-1", kind="mem_high", threshold=80,
        value=87.3, fired_at=T0, message="rendered",
    )
    record_event(event)

    back = events(OWNER_A, limit=10)
    assert len(back) == 1
    got = back[0]
    assert got.owner_id == OWNER_A and got.alias == "web-1"
    assert got.value == pytest.approx(87.3) and got.fired_at == T0

    assert events(OWNER_B) == [], "events never leak across owners"


def test_events_limit_is_honored(tmp_path, monkeypatch) -> None:
    from src.audit import AuditLogger

    audit_log = AuditLogger(tmp_path / "audit", filename="audit.log")
    monkeypatch.setattr("src.alerts.storage.audit", audit_log)
    for i in range(4):
        record_event(
            AlertEvent(
                owner_id=OWNER_A, alias="web-1", kind="cpu_high", threshold=50,
                value=60.0 + i, fired_at=T0 + i, message="m",
            )
        )
    back = events(OWNER_A, limit=2)
    assert len(back) == 2
    assert back[0].value == 63.0, "newest first"
    assert back[1].value == 62.0


def test_events_survives_bad_audit_lines(tmp_path, monkeypatch) -> None:
    from src.audit import AuditLogger

    audit_log = AuditLogger(tmp_path / "audit", filename="audit.log")
    monkeypatch.setattr("src.alerts.storage.audit", audit_log)
    record_event(
        AlertEvent(
            owner_id=OWNER_A, alias="web-1", kind="disk_high", threshold=90,
            value=95.0, fired_at=T0, message="m",
        )
    )
    # Corrupt the log: one truncated line appended after the good one.
    path = audit_log.path
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("{corrupted\n")
    back = events(OWNER_A)
    assert len(back) == 1, "a corrupt line is skipped, not fatal"


def test_record_event_never_raises(monkeypatch) -> None:
    class Exploding:
        def record(self, *args, **kwargs):
            raise OSError("disk full")

        def tail(self, *args, **kwargs):
            return []

    monkeypatch.setattr("src.alerts.storage.audit", Exploding())
    record_event(
        AlertEvent(
            owner_id=OWNER_A, alias="web-1", kind="cpu_high", threshold=50,
            value=55.0, fired_at=T0, message="m",
        )
    )
    assert METRICS.get("alerts_store_failures_total") >= 1


# -- notifier -----------------------------------------------------------------------


def test_notify_without_sender_records_and_reports_not_delivered() -> None:
    """The default deployment has no sender: counted, not delivered, no error.

    "No sender wired" is a normal configuration, not a delivery failure, so it
    must NOT increment ``alerts_notify_failures_total`` — otherwise every
    uneventful sweep would look like a broken notifier.
    """
    event = AlertEvent(
        owner_id=OWNER_A, alias="web-1", kind="mem_high", threshold=80,
        value=90.0, fired_at=T0, message="m",
    )
    delivered = asyncio.run(notify(event))
    assert delivered is False
    assert METRICS.get("alerts_events_total", kind="mem_high") >= 1
    assert METRICS.get("alerts_notify_failures_total", kind="mem_high") == 0


def test_notify_true_and_false_paths() -> None:
    event = AlertEvent(
        owner_id=OWNER_A, alias="web-1", kind="disk_high", threshold=90,
        value=95.0, fired_at=T0, message="m",
    )

    async def ok(sender_event: AlertEvent) -> bool:
        return True

    async def refusing(sender_event: AlertEvent) -> bool:
        return False

    assert asyncio.run(notify(event, ok)) is True
    assert METRICS.get("alerts_notify_sent_total", kind="disk_high") >= 1
    assert asyncio.run(notify(event, refusing)) is False


def test_notify_sender_raise_never_escapes() -> None:
    event = AlertEvent(
        owner_id=OWNER_A, alias="web-1", kind="cpu_high", threshold=70,
        value=80.0, fired_at=T0, message="m",
    )

    async def broken(sender_event: AlertEvent) -> bool:
        raise RuntimeError("telegram 500")

    assert asyncio.run(notify(event, broken)) is False
    assert METRICS.get("alerts_notify_failures_total", kind="cpu_high") >= 1


def test_render_event_text_from_event_and_from_rule() -> None:
    event = AlertEvent(
        owner_id=OWNER_A, alias="web-1", kind="mem_high", threshold=80,
        value=87.5, fired_at=T0, message="",
    )
    from_event = render_event_text(event)
    from_rule = render_event_text(rule=RULE, value=87.5, fired_at=T0)
    assert from_event == from_rule
    assert "web-1" in from_event
    assert "87.5" in from_event and "80" in from_event
    assert format_ts(T0) == "22:13", "UTC stamp is deterministic (tests pin it)"


def test_render_event_text_requires_complete_input() -> None:
    with pytest.raises(ValueError):
        render_event_text()
    with pytest.raises(ValueError):
        render_event_text(rule=RULE, value=1.0)
