# PURPOSE: Cover the scheduled-backup runner end-to-end with fakes: success,
#          retrieval failure, overwrite failure, rotation pruning, and the
#          metric trail — plus the same flows through a real tmp_path tree.
# OWNER: lane `backup` of wave 3.4 (branch feat/3.4-backup).
# CONSUMES: src.sched.runner, src.sched.models, src.metrics.
#           asyncio.run over a fake run_cmd; no network, no Telegram, no
#           sleeps — everything is deterministic.
# PROVIDES: tests/test_sched_runner.py.
# INVARIANTS: the runner never raises for any of the failure scenarios; every
#             outcome leaves a sched_backup_total{status=...} counter; details
#             are secret-free (no exception text, no paths outside the tree).
# EXTEND: new outcomes belong next to the scenario that pins them.

"""Runner tests for src.sched (fake run_cmd + tmp_path filesystem)."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import pytest

from src.metrics import METRICS
from src.sched.models import BackupPlan
from src.sched.runner import archive_dir_for, rotate_archive_dir, run_backup

NOW = datetime(2026, 10, 22, 3, 0, 0)
PLAN = BackupPlan(alias="web", hour=3, keep=2, owner_id=1, id=11)


@pytest.fixture(autouse=True)
def _reset_metrics():
    """Isolates the shared registry around each test (same pattern as test_jobs_ui)."""
    before_counters = dict(METRICS._counters)
    before_gauges = dict(METRICS._gauges)
    yield
    METRICS._counters.clear()
    METRICS._counters.update(before_counters)
    METRICS._gauges.clear()
    METRICS._gauges.update(before_gauges)


def _ok_cmd_factory(tmp_path: Path, contents: str = "archive-bytes"):
    """Returns an async run_cmd that writes a fresh archive under tmp_path."""

    async def run_cmd(alias: str) -> Path:
        path = tmp_path / f"stage_{alias}.zip"
        path.write_text(contents)
        return path

    return run_cmd


def _run(plan: BackupPlan, cmd, tmp_path: Path):
    return asyncio.run(run_backup(plan, cmd, base=tmp_path / "data", now=NOW))


# -- success + rotation ---------------------------------------------------------


def test_success_moves_archive_into_per_plan_dir(tmp_path: Path) -> None:
    base = tmp_path / "data"
    run = _run(PLAN, _ok_cmd_factory(tmp_path), tmp_path)

    assert run.ok is True
    assert run.plan_id == PLAN.id and run.alias == "web"
    final = Path(run.detail)
    assert final.is_file()
    assert final.parent == base / "web", "archives live in a per-plan dir"
    assert final.name.startswith("web_20261022_030000_"), "deterministic stamp"
    # the staged original is gone (moved, not duplicated)
    assert not (tmp_path / "stage_web.zip").exists()
    assert METRICS.get("sched_backup_total", status="ok") == 1.0
    assert METRICS.get("sched_backup_total", status="started") == 0.0, (
        "no phantom started counter leaks into the rendered series"
    )


def test_rotation_keeps_only_the_newest_keep_files(tmp_path: Path) -> None:
    run_a = _run(PLAN, _ok_cmd_factory(tmp_path, "old-1"), tmp_path)
    run_b = _run(PLAN, _ok_cmd_factory(tmp_path, "old-2"), tmp_path)
    run_c = _run(PLAN, _ok_cmd_factory(tmp_path, "new"), tmp_path)

    plan_dir = archive_dir_for("web", tmp_path / "data")
    names = sorted(path.name for path in plan_dir.iterdir())
    assert len(names) == PLAN.keep, "keep=2 must leave exactly two archives"
    assert Path(run_a.detail).name not in names, "the oldest archive was pruned"
    assert Path(run_c.detail).name in names, "the newest archive always survives"
    assert Path(run_b.detail).name in names


def test_rotation_prunes_the_oldest_even_across_restarts(tmp_path: Path) -> None:
    import os

    plan_dir = archive_dir_for("web", tmp_path / "data")
    plan_dir.mkdir(parents=True)
    for index in range(5):
        entry = plan_dir / f"web_old_{index}.zip"
        entry.write_text(f"backup-{index}")
        # Distinct, ordered mtimes so pruning is deterministic.
        stamp = 1700000000 + index
        os.utime(entry, (stamp, stamp))

    deleted = rotate_archive_dir(plan_dir, keep=2)

    assert deleted == 3
    remaining = sorted(path.name for path in plan_dir.iterdir())
    assert remaining == ["web_old_3.zip", "web_old_4.zip"]


def test_rotate_on_missing_directory_is_a_noop(tmp_path: Path) -> None:
    assert rotate_archive_dir(tmp_path / "does_not_exist", keep=2) == 0


def test_rotate_rejects_zero_keep() -> None:
    with pytest.raises(ValueError):
        rotate_archive_dir(Path("anything"), keep=0)


# -- retrieval failure ----------------------------------------------------------


def test_run_cmd_returning_none_is_retrieval_failed(tmp_path: Path) -> None:
    async def run_cmd(alias: str) -> Path | None:
        return None

    run = _run(PLAN, run_cmd, tmp_path)
    assert run.ok is False
    assert run.status == "retrieval_failed"
    assert run.detail == "archive not produced"
    assert not (tmp_path / "data" / "web").exists()
    assert METRICS.get("sched_backup_total", status="retrieval_failed") == 1.0


def test_run_cmd_raise_never_propagates_and_detail_carries_no_text(tmp_path: Path) -> None:
    class ExplodingTarget(RuntimeError):
        pass

    async def run_cmd(alias: str) -> Path:
        # The message mimics a secret-bearing failure (e.g. a token echoed in
        # an exception) — the runner must NOT copy it into the detail.
        raise ExplodingTarget("ssh: password hunter2 failed for token=XYZ")

    run = _run(PLAN, run_cmd, tmp_path)

    assert run.ok is False
    assert run.status == "retrieval_failed"
    assert run.detail == "ExplodingTarget"
    assert "hunter2" not in run.detail and "XYZ" not in run.detail
    assert METRICS.get("sched_backup_errors_total", phase="retrieve") == 1.0


# -- overwrite failure ----------------------------------------------------------


def test_filesystem_failure_reports_overwrite_failed(tmp_path: Path, monkeypatch) -> None:
    """The archive lands but the per-plan dir cannot be created."""
    base = tmp_path / "data"
    blocker = base / "web"
    blocker.parent.mkdir(parents=True)
    blocker.write_text("", )  # a FILE where the per-plan dir must be

    killed = []

    real_mkdir = Path.mkdir

    def refusing_mkdir(self, *args, **kwargs):
        if self == blocker:
            killed.append(True)
            raise OSError("read-only file system")
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", refusing_mkdir)

    run = _run(PLAN, _ok_cmd_factory(tmp_path), tmp_path)

    assert killed, "the blocking path must have been attempted"
    assert run.ok is False
    assert run.status == "overwrite_failed"
    assert "read-only" in run.detail
    assert METRICS.get("sched_backup_total", status="overwrite_failed") == 1.0
    # the staged original is still intact: a failed store must not destroy
    # the producer's archive before the next run can try again
    assert (tmp_path / "stage_web.zip").exists()


def test_prune_failure_never_raises(tmp_path: Path, monkeypatch) -> None:
    plan_dir = archive_dir_for("web", tmp_path / "data")
    plan_dir.mkdir(parents=True)
    stale = plan_dir / "web_a.zip"
    fresh = plan_dir / "web_b.zip"
    stale.write_text("old")
    fresh.write_text("new")
    import os

    os.utime(stale, (1, 1))
    os.utime(fresh, (2, 2))

    real_unlink = Path.unlink

    def refusing_unlink(self, *args, **kwargs):
        if self == stale:
            raise OSError("in use")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refusing_unlink)

    assert rotate_archive_dir(plan_dir, keep=1) == 0
    assert plan_dir.iterdir() and METRICS.get("sched_backup_errors_total", phase="prune") == 1.0
