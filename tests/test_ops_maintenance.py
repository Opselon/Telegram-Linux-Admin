"""Unit tests for src/ops/maintenance.py (wave 3.3 lane ops).

Covers the contract's three default periodic jobs without touching the
real bot state: audit rotation against a tmp_path directory, the ssh
pool reaper's tolerance of an absent/present/broken manager surface, and
the metrics ticker pushing gauges into METRICS.  Degradation is proven
the same way the runtime would see it: never an exception, always a
MaintenanceResult with a truthful detail.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import pytest

import src.bot.state as bot_state
from src.config import Config
from src.metrics import METRICS
from src.ops.maintenance import (
    MaintenanceResult,
    metrics_ticker,
    register_defaults,
    reap_ssh_pool,
    rotate_audit_logs,
)
from src.ops.scheduler import Scheduler


class _FakeAuditLike:
    """Duck-typed audit logger whose path points into a (maybe missing) dir."""

    def __init__(self, directory: Any) -> None:
        self._dir = directory
        self._log = directory / "audit.log"

    @property
    def path(self) -> Any:
        return self._log


@pytest.fixture(autouse=True)
def _clean_metrics() -> Any:
    METRICS.reset()
    yield
    METRICS.reset()


# -- audit rotation --------------------------------------------------------
def test_rotation_moves_old_log_files(tmp_path: Any) -> None:
    fake = _FakeAuditLike(tmp_path)
    stale = tmp_path / "audit.log"
    stale.write_text("old", encoding="utf-8")
    old = time.time() - 30 * 86400
    os.utime(stale, (old, old))
    fresh = tmp_path / "audit.1.log"
    fresh.write_text("fresh", encoding="utf-8")

    result = rotate_audit_logs(fake, max_age_days=7, keep_files=3)

    assert isinstance(result, MaintenanceResult)
    assert result.ok
    assert result.detail == "rotated=1 dropped=0"
    assert (tmp_path / "audit.log.gz").is_file()
    assert (tmp_path / "audit.log.gz").read_bytes().startswith(b"\x1f\x8b")
    assert not stale.exists(), "original must be removed after compressing"
    assert fresh.exists(), "recent files must not be rotated"


def test_rotation_keeps_newest_archives_up_to_keep_files(tmp_path: Any) -> None:
    fake = _FakeAuditLike(tmp_path)
    for i in range(5):
        p = tmp_path / f"old-{i}.log"
        p.write_text("x", encoding="utf-8")
        old = time.time() - (30 + i) * 86400
        os.utime(p, (old, old))
    result = rotate_audit_logs(fake, max_age_days=1, keep_files=2)
    assert result.ok
    archives = sorted(tmp_path.glob("*.gz"))
    assert len(archives) == 2, f"keep_files=2 => 2 archives, got {archives}"


def test_rotation_tolerates_missing_directory(tmp_path: Any) -> None:
    fake = _FakeAuditLike(tmp_path / "nonexistent" / "deeper")
    result = rotate_audit_logs(fake)
    assert result.ok
    assert "no audit dir" in result.detail


def test_rotation_registers_only_when_config_enables() -> None:
    """Config-driven, OFF by default: no 'rotation' job until opted in."""
    s = Scheduler()
    asyncio.run(register_defaults(s))
    assert set(s.status()) == {"reaper", "ticker"}

    enabled = object.__new__(Config)  # no disk load: pure shape test
    enabled.extra = {
        "maintenance": {"audit_rotation": {"enabled": True, "max_age_days": 30}}
    }
    s2 = Scheduler()
    asyncio.run(register_defaults(s2, config=enabled))
    assert set(s2.status()) == {"reaper", "ticker", "rotation"}
    # Cadence is clamped: at least hourly, at most daily.
    assert s2.status()["rotation"]["interval"] == 86400.0

    quick = object.__new__(Config)
    quick.extra = {
        "maintenance": {"audit_rotation": {"enabled": True, "max_age_days": 0.5}}
    }
    s4 = Scheduler()
    asyncio.run(register_defaults(s4, config=quick))
    # spread over keep=3 slots: 0.5 day / 3 = 4 hours per slot (>= the 1h floor)
    assert s4.status()["rotation"]["interval"] == 14400.0

    disabled = object.__new__(Config)
    disabled.extra = {
        "maintenance": {"audit_rotation": {"enabled": False, "max_age_days": 30}}
    }
    s3 = Scheduler()
    asyncio.run(register_defaults(s3, config=disabled))
    assert "rotation" not in s3.status()


def test_rotation_defaults_registered_jobs_and_cadence() -> None:
    s = Scheduler()
    asyncio.run(register_defaults(s))
    assert s.status()["reaper"]["interval"] == 3600.0
    assert s.status()["ticker"]["interval"] == 60.0


# -- ssh pool reaper -------------------------------------------------------
def test_reaper_tolerates_absent_manager(monkeypatch: Any) -> None:
    monkeypatch.setattr(bot_state, "ssh_manager", None)
    result = reap_ssh_pool()
    assert result.ok
    assert "not present" in result.detail


def test_reaper_calls_existing_hook(monkeypatch: Any) -> None:
    calls: list[int] = []

    class _Mgr:
        def _reap_idle(self) -> None:
            calls.append(1)

    monkeypatch.setattr(bot_state, "ssh_manager", _Mgr())
    result = reap_ssh_pool()
    assert result.ok
    assert result.detail == "hook invoked"
    assert calls == [1]


def test_reaper_records_hook_failure_not_raise(monkeypatch: Any) -> None:
    class _BrokenMgr:
        def _reap_idle(self) -> None:
            raise RuntimeError("pool exploded")

    monkeypatch.setattr(bot_state, "ssh_manager", _BrokenMgr())
    result = reap_ssh_pool()
    assert not result.ok
    assert "hook raised" in result.detail
    assert "pool exploded" in result.detail
    # degradation is visible in METRICS too
    assert METRICS.get("ops_maintenance_degraded_total", job="ssh_pool_reaper") >= 1


# -- metrics ticker --------------------------------------------------------
def test_ticker_pushes_scheduler_and_queue_gauges() -> None:
    s = Scheduler()
    s.add("reaper", 3600.0, reap_ssh_pool)

    class _FakeQueue:
        def stats(self) -> dict[str, int]:
            return {"running": 2, "pending": 3}

    result = metrics_ticker(scheduler=s, queue=_FakeQueue())

    assert result.ok
    assert METRICS.get("ops_scheduler_tasks") == 1.0
    assert METRICS.get("ops_scheduler_tasks_errored") == 0.0
    assert METRICS.get("ops_queue_running") == 2.0
    assert METRICS.get("ops_queue_pending") == 3.0


def test_ticker_counts_errored_scheduler_tasks() -> None:
    s = Scheduler()

    def bad() -> None:
        raise RuntimeError("deliberate")

    s.add("bad", 1, bad)
    s.now(at=0)
    metrics_ticker(scheduler=s, queue=None)
    assert METRICS.get("ops_scheduler_tasks_errored") == 1.0


def test_ticker_tolerates_absent_subsystems() -> None:
    result = metrics_ticker(scheduler=None, queue=None)
    assert result.ok
    assert result.detail == "gauges pushed"


def test_ticker_job_never_raises_through_scheduler() -> None:
    """A broken queue/stats surface degrades; the scheduler loop holds."""

    class _ExplodingQueue:
        def stats(self) -> dict[str, int]:
            raise RuntimeError("queue unreadable")

    def job() -> MaintenanceResult:
        return metrics_ticker(scheduler=None, queue=_ExplodingQueue())

    result = job()
    assert not result.ok
    assert "queue unreadable" in result.detail
    assert METRICS.get("ops_maintenance_degraded_total", job="metrics_ticker") >= 1
