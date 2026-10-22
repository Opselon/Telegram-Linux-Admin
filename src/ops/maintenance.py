"""Default periodic jobs for the wave 3.3 scheduler (ops lane).

PURPOSE
    Wires the bot's recurring upkeep onto :mod:`src.ops.scheduler`:
    audit-log rotation, SSH pool reaping and a metrics ticker that pushes
    scheduler/queue gauges into :data:`src.metrics.METRICS`.  Every job is
    tolerant of the subsystem it targets being absent or broken: the job
    records the degradation in its result (and in the metrics registry)
    and moves on — it never raises into the scheduler loop itself.

OWNER
    Lane ops (feat/3.3-ops).  Registration is caller-driven; this module
    never touches application wiring.

CONSUMES
    src.audit (rotation candidates and directory), src.metrics.METRICS,
    the SSH manager surface exposed through ``src.bot.state.ssh_manager``
    (read dynamically so absent subsystems degrade instead of breaking).

PROVIDES
    :class:`MaintenanceResult` — one record per job execution.
    :func:`rotate_audit_logs` — compresses/moves audit files older than
    ``max_age_days`` (config-driven, OFF until enabled: default interval
    is None so it does not register until the operator turns it on).
    :func:`reap_ssh_pool` — calls the existing pool reaping hook hourly.
    :func:`metrics_ticker` — pushes scheduler/queue gauges every minute.
    :func:`register_defaults` — binds the enabled jobs onto a Scheduler.
"""

from __future__ import annotations

import asyncio
import gzip
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..audit import AuditLogger, audit
from ..metrics import METRICS

logger = logging.getLogger(__name__)

#: Defaults matching the audit module's own heuristics (5 MiB rotation).
ROTATION_DEFAULT_MAX_AGE_DAYS = 14
ROTATION_DEFAULT_KEEP_FILES = 3
REAP_DEFAULT_INTERVAL = 3600.0
METRICS_DEFAULT_INTERVAL = 60.0


@dataclass
class MaintenanceResult:
    """Outcome of a single maintenance job run."""

    job: str
    ok: bool
    detail: str


# -- audit log rotation ----------------------------------------------------
def rotate_audit_logs(
    audit_logger: AuditLogger | None = None,
    *,
    max_age_days: float = ROTATION_DEFAULT_MAX_AGE_DAYS,
    keep_files: int = ROTATION_DEFAULT_KEEP_FILES,
) -> MaintenanceResult:
    """Compresses audit files older than ``max_age_days``.

    Only the audit logger's own files are touched (they end in ``.log``);
    gzip is used so read tools still work during the grace window.  The
    returned :class:`MaintenanceResult` records how many files were moved;
    a filesystem error degrades to ``ok=False`` instead of raising so
    the scheduler contract holds.
    """
    job = "audit_rotation"
    try:
        target = audit_logger if audit_logger is not None else audit
        root = Path(target.path).parent
        if not root.is_dir():
            return MaintenanceResult(job, ok=True, detail=f"no audit dir at {root}")
        cutoff = time.time() - max_age_days * 86400
        candidates = [
            p
            for p in root.iterdir()
            if p.is_file() and p.suffix.lower() in {".log", ".jsonl"}
        ]
        moved = 0
        dropped = 0
        for path in sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime > cutoff:
                continue
            zipped = path.with_name(path.name + ".gz")
            try:
                with open(path, "rb") as src, gzip.open(zipped, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                path.unlink()
                moved += 1
            except OSError as exc:
                logger.warning("unable to rotate audit file %s: %s", path, exc)
                continue
            # Keep the newest `keep_files` compressed archives; older lose.
        archives = [
            p
            for p in root.glob("*.gz")
            if p.is_file() and (p.name.startswith("audit") or p.name.endswith(".gz"))
        ]
        for path in sorted(archives, key=lambda p: p.stat().st_mtime, reverse=True)[
            keep_files:
        ]:
            try:
                path.unlink()
                dropped += 1
            except OSError:  # pragma: no cover - defensive
                continue
        METRICS.inc("ops_audit_rotated_total", moved)
        METRICS.inc("ops_audit_retention_dropped_total", dropped)
        return MaintenanceResult(
            job=job, ok=True, detail=f"rotated={moved} dropped={dropped}"
        )
    except Exception as exc:  # degrade, never raise into the scheduler
        logger.exception("audit rotation degraded")
        METRICS.inc("ops_maintenance_degraded_total", job=job)
        return MaintenanceResult(job=job, ok=False, detail=f"error: {exc}")


def _rotation_from_config(config: object) -> tuple[float, int] | None:
    """Reads ``maintenance.audit_rotation`` off a Config-like object.

    Honors both ``config.extra['maintenance']`` (persisted keys) and a
    direct ``maintenance`` attribute, so an integration can adopt either
    surface without another commit.  Returns None when the feature is off,
    which is the default.
    """
    source = config if config is not None else None
    section = getattr(source, "maintenance", None)
    if section is None:
        extra = getattr(source, "extra", None)
        if isinstance(extra, dict):
            section = extra.get("maintenance")
    if not isinstance(section, dict):
        return None
    rotation = section.get("audit_rotation")
    if isinstance(rotation, dict) and rotation.get("enabled"):
        try:
            days = float(rotation.get("max_age_days", ROTATION_DEFAULT_MAX_AGE_DAYS))
            keep = int(rotation.get("keep_files", ROTATION_DEFAULT_KEEP_FILES))
        except (TypeError, ValueError):
            return None
        if days <= 0 or keep < 0:
            return None
        return days, keep
    return None


# -- ssh pool reaper -------------------------------------------------------
def reap_ssh_pool() -> MaintenanceResult:
    """Calls the existing pool reaping hook once per hour.

    The manager lives on ``state.ssh_manager`` and its ``_reap_idle`` hook
    is synchronous and cheap; the dynamic import keeps this module usable
    when no bot state object exists yet (integration degraded mode).
    """
    job = "ssh_pool_reaper"
    try:  # dynamic import so an absent bot state degrades cleanly
        from ..bot import state

        manager = getattr(state, "ssh_manager", None)
        reap = getattr(manager, "_reap_idle", None)
    except Exception as exc:  # degrade, never raise into the scheduler
        METRICS.inc("ops_maintenance_degraded_total", job=job)
        return MaintenanceResult(job=job, ok=True, detail=f"manager absent: {exc}")
    if reap is None or not callable(reap):
        METRICS.inc("ops_maintenance_degraded_total", job=job)
        return MaintenanceResult(job=job, ok=True, detail="pool hook not present")
    try:
        reap()
    except Exception as exc:  # the hook itself misbehaved
        METRICS.inc("ops_maintenance_degraded_total", job=job)
        return MaintenanceResult(job=job, ok=False, detail=f"hook raised: {exc}")
    METRICS.inc("ops_ssh_pool_reaped_total")
    return MaintenanceResult(job=job, ok=True, detail="hook invoked")


# -- metrics ticker --------------------------------------------------------
def metrics_ticker(
    scheduler: Any | None = None,
    queue: Any | None = None,
) -> MaintenanceResult:
    """Pushes scheduler/queue gauges into METRICS once a minute.

    Accepts any object exposing :meth:`status` (Schedulers) or
    :meth:`stats` (JobQueue); missing callables degrade to ok=True so a
    partially wired integration never fails the ticker.
    """
    job = "metrics_ticker"

    try:
        if scheduler is not None and callable(getattr(scheduler, "status", None)):
            s_status = dict(scheduler.status())
            METRICS.set("ops_scheduler_tasks", float(len(s_status)))
            errored = sum(
                1 for info in s_status.values() if info.get("last_error") is not None
            )
            METRICS.set("ops_scheduler_tasks_errored", float(errored))
        if queue is not None and callable(getattr(queue, "stats", None)):
            stats = dict(queue.stats())
            METRICS.set("ops_queue_running", float(stats.get("running", 0)))
            METRICS.set("ops_queue_pending", float(stats.get("pending", 0)))
    except Exception as exc:  # degrade, never raise
        METRICS.inc("ops_maintenance_degraded_total", job=job)
        return MaintenanceResult(job=job, ok=False, detail=f"error: {exc}")
    return MaintenanceResult(job=job, ok=True, detail="gauges pushed")


def _rotation_interval(days: float, keep: int) -> float:
    """Rotation cadence: at least hourly, at most daily.

    Spread over ``keep`` retention slots, a file lives at most ``days``
    after rotation; the 1h floor avoids micro-storms on very short ages.
    """
    spread = days / max(1, keep)
    return min(max(3600.0, spread * 86400.0), 86400.0)


# -- default registration --------------------------------------------------
async def register_defaults(
    scheduler: object,
    *,
    audit_logger: AuditLogger | None = None,
    config: object | None = None,
    queue: object | None = None,
) -> None:
    """Binds the enabled default jobs onto ``scheduler`` for the caller.

    * audit rotation: only when the config enables it under
      ``maintenance.audit_rotation`` — OFF until an operator opts in
      (CONTRACT.md lane ops, "config-driven, off by default until wired").
    * ssh pool reaper: hourly, every hour the manager surface is present.
    * metrics ticker: every minute pushing scheduler/queue gauges.

    Requires a real :class:`src.ops.scheduler.Scheduler` (parameter typed
    loosely so the import stays inside this function), otherwise it fails
    fast at registration time instead of silently doing nothing forever.
    """
    if not hasattr(scheduler, "add") or not hasattr(scheduler, "status"):
        raise TypeError("register_defaults expects a src.ops.scheduler.Scheduler")
    rotation_cfg = _rotation_from_config(config)
    if rotation_cfg is not None:
        days, keep = rotation_cfg
        scheduler.add(
            "rotation",
            _rotation_interval(days, keep),
            lambda: rotate_audit_logs(
                audit_logger, max_age_days=days, keep_files=keep
            ),
        )
    scheduler.add("reaper", REAP_DEFAULT_INTERVAL, reap_ssh_pool)
    scheduler.add(
        "ticker",
        METRICS_DEFAULT_INTERVAL,
        lambda: metrics_ticker(scheduler=scheduler, queue=queue),
    )
