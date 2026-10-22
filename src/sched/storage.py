"""Purpose: SQLite persistence for backup plans (table ``scheduled_backups``).

OWNER: lane `backup` of wave 3.4 (branch feat/3.4-backup), package src/sched/.
CONSUMES: ``src.database.get_db_connection`` (the process-wide connection) and
  :class:`src.sched.models.BackupPlan`.
PROVIDES: :data:`SCHEMA_SQL` (the orchestrator wires it into db init),
  :func:`add_plan`, :func:`set_plan`, :func:`get_plan`, :func:`list_plans`,
  :func:`delete_plan`.
INVARIANTS:
  * every mutation is ownership-checked: a plan belonging to another owner is
    reported as "not found", never as "forbidden" (no existence oracle);
  * a missing table / unopenable DB degrades: the read paths return an empty
    list / ``None`` and count a metric; they never raise into a handler or the
    scheduler loop (CONTRACT hard law L5);
  * rows read back are validated through :class:`BackupPlan`, so a corrupt
    row is skipped with a log line instead of surfacing garbage upstream;
  * hours/keeps are validated by the model before they reach SQL.
EXTEND: add a column in ``SCHEMA_SQL`` *and* a backwards-compatible default in
  the model; the orchestrator owns the database-init wiring.
"""

from __future__ import annotations

import logging
import sqlite3

from ..metrics import METRICS
from .models import BackupPlan

logger = logging.getLogger(__name__)

__all__ = [
    "SCHEMA_SQL",
    "TABLE",
    "add_plan",
    "set_plan",
    "get_plan",
    "list_plans",
    "delete_plan",
]

#: Table owned by this lane.  ``owner_id`` carries the admin who created it;
#: ``hour``/``keep``/``enabled`` mirror :class:`BackupPlan`.
TABLE = "scheduled_backups"

#: Delivered as a constant so the orchestrator can run it from db init
#: (``executescript``-safe: one statement, idempotent).
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS scheduled_backups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL DEFAULT 0,
    alias TEXT NOT NULL,
    hour INTEGER NOT NULL,
    keep INTEGER NOT NULL DEFAULT 7,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""".strip()

_COLUMNS = "id, owner_id, alias, hour, keep, enabled"


def _connect() -> sqlite3.Connection:
    """The process-wide connection (imported lazily so tests can patch it)."""
    from ..database import get_db_connection

    return get_db_connection()


def _row_to_plan(row: sqlite3.Row | tuple) -> BackupPlan | None:
    """Builds a validated plan from a row; ``None`` when the row is corrupt."""
    try:
        return BackupPlan(
            id=int(row[0]),
            owner_id=int(row[1]),
            alias=str(row[2]),
            hour=int(row[3]),
            keep=int(row[4]),
            enabled=bool(row[5]),
        )
    except (IndexError, TypeError, ValueError):
        logger.warning("skipping unreadable scheduled_backups row: %r", tuple(row))
        METRICS.inc("sched_storage_corrupt_row_total")
        return None


def _degrade(op: str, exc: BaseException) -> None:
    """Records a storage failure without raising into the caller."""
    logger.warning("scheduled_backups %s failed: %s", op, exc)
    METRICS.inc("sched_storage_errors_total", op=op)


def add_plan(plan: BackupPlan) -> BackupPlan:
    """Inserts ``plan`` and returns it with the assigned id.

    Returns the plan unchanged with ``id=0`` when the table is unavailable: the
    caller (UI) shows a generic failure, nothing raises (law L5).
    """
    try:
        conn = _connect()
        cur = conn.execute(
            f"INSERT INTO {TABLE} (owner_id, alias, hour, keep, enabled) VALUES (?, ?, ?, ?, ?)",
            (plan.owner_id, plan.alias, plan.hour, plan.keep, int(plan.enabled)),
        )
        conn.commit()
        return BackupPlan(
            id=int(cur.lastrowid or 0),
            owner_id=plan.owner_id,
            alias=plan.alias,
            hour=plan.hour,
            keep=plan.keep,
            enabled=plan.enabled,
        )
    except (sqlite3.Error, OSError) as exc:
        _degrade("add", exc)
        return plan


def set_plan(plan: BackupPlan) -> bool:
    """Updates a plan owned by ``plan.owner_id``. False when nothing matched."""
    try:
        conn = _connect()
        cur = conn.execute(
            f"UPDATE {TABLE} SET alias = ?, hour = ?, keep = ?, enabled = ? "
            "WHERE id = ? AND owner_id = ?",
            (plan.alias, plan.hour, plan.keep, int(plan.enabled), plan.id, plan.owner_id),
        )
        conn.commit()
        return cur.rowcount > 0
    except (sqlite3.Error, OSError) as exc:
        _degrade("set", exc)
        return False


def get_plan(plan_id: int, owner_id: int) -> BackupPlan | None:
    """One plan by id, scoped to its owner. ``None`` when absent/foreign."""
    try:
        conn = _connect()
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM {TABLE} WHERE id = ? AND owner_id = ?",
            (plan_id, owner_id),
        ).fetchone()
    except (sqlite3.Error, OSError) as exc:
        _degrade("get", exc)
        return None
    if row is None:
        return None
    return _row_to_plan(row)


def list_plans(
    owner_id: int, alias: str | None = None, enabled: bool | None = None
) -> list[BackupPlan]:
    """Owner-scoped listing, optionally filtered; oldest plan first."""
    sql = f"SELECT {_COLUMNS} FROM {TABLE} WHERE owner_id = ?"
    params: list[object] = [owner_id]
    if alias is not None:
        sql += " AND alias = ?"
        params.append(alias)
    if enabled is not None:
        sql += " AND enabled = ?"
        params.append(int(enabled))
    sql += " ORDER BY id"
    try:
        conn = _connect()
        rows = conn.execute(sql, params).fetchall()
    except (sqlite3.Error, OSError) as exc:
        _degrade("list", exc)
        return []
    plans = (_row_to_plan(row) for row in rows)
    return [plan for plan in plans if plan is not None]


def delete_plan(plan_id: int, owner_id: int) -> bool:
    """Deletes one plan owned by ``owner_id``. False when nothing matched."""
    try:
        conn = _connect()
        cur = conn.execute(
            f"DELETE FROM {TABLE} WHERE id = ? AND owner_id = ?", (plan_id, owner_id)
        )
        conn.commit()
        return cur.rowcount > 0
    except (sqlite3.Error, OSError) as exc:
        _degrade("delete", exc)
        return False
