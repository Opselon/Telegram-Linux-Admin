"""PURPOSE: Rule CRUD for the alerts lane + the fired-event read model.

OWNER: lane `alerts` of wave 3.4 (branch feat/3.4-alerts).
CONSUMES: src.database (shared connection + transaction), src.alerts.models
  (validated construction of every object crossing the boundary),
  src.audit.audit (the only persistence for fired events), src.metrics.
PROVIDES: ``SCHEMA_SQL`` (the orchestrator wires it into db init),
  ``ensure_schema()``, and the CRUD surface the Telegram UI and the scheduled
  sweep call: add_rule / list_rules / get_rule / set_enabled / delete_rule /
  record_event / events.
INVARIANTS (contract: ownership checked, degrade never crash):
  * Ownership is enforced in SQL (``WHERE ... owner_id = ?``), not in a
    Python filter, so a foreign row can never be read, toggled or deleted —
    and the mutators return ``False`` rather than raising, so a stale inline
    keyboard degrades instead of erroring into the bot loop.
  * Every returned object is rebuilt through the validated ``AlertRule`` /
    ``AlertEvent`` constructors: a row that somehow became invalid is skipped
    (counted + debug-logged), never handed to the caller.
  * There is NO events table (contract: keep the schema small).  Fired events
    go to the audit log and ``events()`` reads them back from there, so the
    backlog survives a restart without a second store to migrate.
  * A missing table is an expected first-boot state, not a crash: reads
    return empty and count a failure.
  * ``payload`` is stored for forward compatibility but never interpreted;
    ``kind`` alone selects the probe.
EXTEND: a new column is an APPEND to ``SCHEMA_SQL``; never edit an existing
  column (the orchestrator's migration path is append-only).
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from ..audit import audit
from ..database import get_db_connection, transaction
from ..metrics import METRICS
from .models import AlertEvent, AlertRule

logger = logging.getLogger(__name__)

__all__ = [
    "DataStoreError",
    "DEFAULT_EVENT_LIMIT",
    "FIRED_EVENT",
    "SCHEMA_INDEX_SQL",
    "SCHEMA_SQL",
    "add_rule",
    "delete_rule",
    "ensure_schema",
    "events",
    "get_rule",
    "list_rules",
    "record_event",
    "set_enabled",
]

#: Audit event name that carries a fired alert (read back by ``events()``).
FIRED_EVENT = "alerts.event.fired"

#: Default page size of the events read model / UI summary.
DEFAULT_EVENT_LIMIT = 50

#: One CREATE TABLE.  UNIQUE(owner_id, alias, kind) makes a duplicate a
#: caller error (ValueError) instead of a silent second rule firing the same
#: alert twice.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS alert_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL,
    alias TEXT NOT NULL,
    kind TEXT NOT NULL,
    threshold INTEGER NOT NULL,
    payload TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(owner_id, alias, kind)
)
"""

#: Serves the sweep's "every enabled rule" query without a full scan.
SCHEMA_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_alert_rules_enabled
    ON alert_rules(enabled, owner_id, alias)
"""


class DataStoreError(RuntimeError):
    """The alert_rules table cannot be written on this host."""


# -- schema -------------------------------------------------------------------
def ensure_schema() -> None:
    """Creates the rules table + index if absent. Idempotent; never raises."""
    try:
        with transaction() as conn:
            conn.execute(SCHEMA_SQL)
            conn.execute(SCHEMA_INDEX_SQL)
    except sqlite3.Error as exc:  # pragma: no cover - defensive
        METRICS.inc("alerts_store_failures_total")
        logger.error("alert_rules schema init failed: %s", exc)


# -- row mapping --------------------------------------------------------------
def _row_to_rule(row: Any) -> AlertRule | None:
    """Builds an AlertRule from a row; None (counted) when the row is invalid."""
    record = dict(row)
    try:
        return AlertRule(
            id=record.get("id"),
            owner_id=record["owner_id"],
            alias=record["alias"],
            kind=record["kind"],
            threshold=int(record["threshold"]),
            payload=record.get("payload"),
            enabled=bool(record.get("enabled", 1)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        METRICS.inc("alerts_store_read_failures_total")
        logger.debug("skipping unreadable alert_rules row: %s", exc)
        return None


def _record_to_event(record: dict[str, Any]) -> AlertEvent | None:
    """Builds an AlertEvent from one audit record's flattened context."""
    context = record.get("context") or {}
    try:
        return AlertEvent(
            owner_id=int(record["user_id"]),
            alias=str(record["alias"]),
            kind=str(context["kind"]),
            threshold=int(context["threshold"]),
            value=float(context["value"]),
            fired_at=float(context["fired_at"]),
            message=str(context.get("message", "")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        METRICS.inc("alerts_store_read_failures_total")
        logger.debug("skipping unreadable alert audit line: %s", exc)
        return None


# -- rule CRUD ----------------------------------------------------------------
def add_rule(
    owner_id: int,
    alias: str,
    kind: str,
    threshold: int,
    payload: str | None = None,
    enabled: bool = True,
) -> AlertRule:
    """Validates and inserts one rule; returns it with its assigned id.

    Raises ``ValueError`` when the input fails model validation or the owner
    already has an identical (alias, kind) rule — a duplicate would fire the
    same alert twice, so it is refused at the boundary.
    """
    candidate = AlertRule(
        owner_id=owner_id,
        alias=alias,
        kind=kind,
        threshold=threshold,
        payload=payload,
        enabled=enabled,
    )
    try:
        with transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO alert_rules"
                " (owner_id, alias, kind, threshold, payload, enabled)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    candidate.owner_id,
                    candidate.alias,
                    candidate.kind,
                    candidate.threshold,
                    candidate.payload,
                    1 if candidate.enabled else 0,
                ),
            )
            rule_id = int(cursor.lastrowid or 0)
    except sqlite3.IntegrityError as exc:
        raise ValueError(
            f"an alert for {candidate.alias}/{candidate.kind} already exists"
        ) from exc
    except sqlite3.Error as exc:
        METRICS.inc("alerts_store_failures_total")
        logger.error("alert_rules insert failed: %s", exc)
        raise DataStoreError("alert rules are unavailable") from exc

    METRICS.inc("alerts_rules_added_total", kind=candidate.kind)
    audit.record(
        "alerts.rule.added",
        user_id=candidate.owner_id,
        alias=candidate.alias,
        outcome="success",
        kind=candidate.kind,
        threshold=candidate.threshold,
    )
    return AlertRule(
        id=rule_id,
        owner_id=candidate.owner_id,
        alias=candidate.alias,
        kind=candidate.kind,
        threshold=candidate.threshold,
        payload=candidate.payload,
        enabled=candidate.enabled,
    )


def list_rules(
    owner_id: int,
    alias: str | None = None,
    enabled: bool | None = None,
) -> list[AlertRule]:
    """Rules owned by ``owner_id``; empty (counted) when the table is absent."""
    sql = "SELECT * FROM alert_rules WHERE owner_id = ?"
    params: list[Any] = [owner_id]
    if alias is not None:
        sql += " AND alias = ?"
        params.append(alias)
    if enabled is not None:
        sql += " AND enabled = ?"
        params.append(1 if enabled else 0)
    sql += " ORDER BY id"

    rules: list[AlertRule] = []
    for row in _fetchall("alert_rules list", sql, params):
        rule = _row_to_rule(row)
        if rule is not None:
            rules.append(rule)
    return rules


def get_rule(rule_id: int, owner_id: int) -> AlertRule | None:
    """One rule by id, scoped to its owner (None when absent or foreign)."""
    rows = _fetchall(
        "alert_rules read",
        "SELECT * FROM alert_rules WHERE id = ? AND owner_id = ?",
        [rule_id, owner_id],
    )
    return _row_to_rule(rows[0]) if rows else None


def set_enabled(rule_id: int, owner_id: int, enabled: bool) -> bool:
    """Toggles one of the owner's rules; False when no row of theirs matched."""

    def _on_change() -> None:
        METRICS.inc("alerts_rules_toggled_total", state="on" if enabled else "off")
        audit.record(
            "alerts.rule.toggled",
            user_id=owner_id,
            outcome="success",
            rule_id=rule_id,
            enabled=bool(enabled),
        )

    return _mutate(
        "alert_rules toggle",
        "UPDATE alert_rules SET enabled = ? WHERE id = ? AND owner_id = ?",
        [1 if enabled else 0, rule_id, owner_id],
        on_change=_on_change,
    )


def delete_rule(rule_id: int, owner_id: int) -> bool:
    """Deletes one of the owner's rules; False when no row of theirs matched."""

    def _on_change() -> None:
        METRICS.inc("alerts_rules_deleted_total")
        audit.record(
            "alerts.rule.deleted",
            user_id=owner_id,
            outcome="success",
            rule_id=rule_id,
        )

    return _mutate(
        "alert_rules delete",
        "DELETE FROM alert_rules WHERE id = ? AND owner_id = ?",
        [rule_id, owner_id],
        on_change=_on_change,
    )


# -- fired events --------------------------------------------------------------
def record_event(event: AlertEvent) -> None:
    """Appends one fired event to the audit log (the only event store).

    Never raises: a full disk must not lose the notification that already
    went out, and must not reach the bot loop (contract hard law L5).
    """
    METRICS.inc("alerts_events_recorded_total", kind=event.kind)
    try:
        audit.record(
            FIRED_EVENT,
            user_id=event.owner_id,
            alias=event.alias,
            outcome="fired",
            kind=event.kind,
            value=event.value,
            threshold=event.threshold,
            fired_at=event.fired_at,
            message=event.message,
        )
    except Exception as exc:  # noqa: BLE001 - recording must never raise
        METRICS.inc("alerts_store_failures_total")
        logger.warning("alert event could not be recorded: %s", exc)


def events(owner_id: int, limit: int = DEFAULT_EVENT_LIMIT) -> list[AlertEvent]:
    """The owner's most recent fired alerts, newest first, read from audit.

    Older events age out of the audit log by its own size rotation, which is
    why no dedicated table exists.  Returns ``[]`` (counted) when the log is
    empty or unreadable.
    """
    if limit <= 0:
        return []
    try:
        records = audit.tail(limit * 4, event=FIRED_EVENT)
    except Exception as exc:  # noqa: BLE001 - a read must never raise
        METRICS.inc("alerts_store_failures_total")
        logger.warning("alert events could not be read: %s", exc)
        return []

    collected: list[AlertEvent] = []
    for record in records:
        if record.get("user_id") != owner_id:
            continue
        event = _record_to_event(record)
        if event is not None:
            collected.append(event)
    collected.reverse()  # audit.tail is oldest-first; the UI wants newest
    return collected[:limit]


# -- sqlite plumbing ------------------------------------------------------------
def _fetchall(what: str, sql: str, params: list[Any]) -> list[Any]:
    """Runs a SELECT, degrading a missing table/unopenable DB to no rows."""
    try:
        conn = get_db_connection()
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        METRICS.inc("alerts_store_failures_total")
        logger.error("%s failed (schema not initialized?): %s", what, exc)
        return []
    except Exception as exc:  # noqa: BLE001 - e.g. DatabaseUnavailable
        METRICS.inc("alerts_store_failures_total")
        logger.error("%s could not open the database: %s", what, exc)
        return []


def _mutate(what: str, sql: str, params: list[Any], *, on_change: Any) -> bool:
    """Runs a write, returning True only when a row actually changed."""
    try:
        with transaction() as conn:
            cursor = conn.execute(sql, params)
            changed = cursor.rowcount > 0
    except sqlite3.Error as exc:
        METRICS.inc("alerts_store_failures_total")
        logger.error("%s failed: %s", what, exc)
        return False
    if changed:
        on_change()
    return changed
