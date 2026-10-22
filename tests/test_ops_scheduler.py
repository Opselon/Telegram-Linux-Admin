"""Unit tests for src/ops/scheduler.py (wave 3.3 lane ops).

Testing approach is fully deterministic: the engine under test is a
manual-tick scheduler.  Tests advance time themselves via ``Scheduler.
now(at=...)`` so no sleeping is involved and timing assumptions cannot
flake on a slow runner.  A separate smoke test exercises ``start()``
and ``stop()`` with a tiny interval to make sure the loop drains and
leaks no tasks.
"""

import asyncio

import pytest

from src.ops.scheduler import Scheduler, TaskInfo


# -- registration ----------------------------------------------------------
def test_add_rejects_zero_interval() -> None:
    s = Scheduler()
    with pytest.raises(ValueError):
        s.add("bad", 0, lambda: None)


def test_add_rejects_negative_interval() -> None:
    s = Scheduler()
    with pytest.raises(ValueError):
        s.add("bad", -1, lambda: None)


def test_start_stop_leaks_no_tasks() -> None:
    async def scenario() -> None:
        ran = {"n": 0}
        s = Scheduler()
        s.add("ticker", 0.01, lambda: ran.__setitem__("n", ran["n"] + 1))
        s.start()
        await asyncio.sleep(0.1)  # interval 0.01 => several due windows
        marker_before = ran["n"]
        assert marker_before >= 1, "battery loop should have ticked at least once"
        await s.stop()
        after = ran["n"]
        # A second stop is safe and a no-op.
        await s.stop()
        await asyncio.sleep(0.05)
        assert ran["n"] == after, "stopped loop kept ticking"
        # nothing left scheduled
        running = [
            t
            for t in asyncio.all_tasks()
            if t is not asyncio.current_task() and not t.done()
        ]
        assert not running, running

    asyncio.run(scenario())


# -- manual tick engine ----------------------------------------------------
def test_sync_job_runs_on_manual_tick() -> None:
    s = Scheduler()
    calls: list[float] = []
    s.add("job", 10, lambda: calls.append(1.0))
    s.now(at=0)  # fires immediately (next_run None)
    assert len(calls) == 1
    s.now(at=5)  # not due yet
    assert len(calls) == 1
    s.now(at=10)
    assert len(calls) == 2
    info = s.status()["job"]
    assert info["last_run"] == 10
    assert info["next_run"] == 20


def test_clock_advancing_backwards_is_handled() -> None:
    """Time going backwards must not loop a task forever."""
    s = Scheduler()
    calls: list[float] = []
    s.add("job", 5, lambda: calls.append(1.0))
    s.now(at=100)
    s.now(at=50)  # clock went back; task must not fire again on stale clock
    assert len(calls) == 1
    info = s.status()["job"]
    assert info["next_run"] == 105, f"expected shed, got {info['next_run']}"


def test_manual_tick_runs_async_job() -> None:
    """Coroutine jobs drive themselves on a fresh loop when ticked manually."""
    s = Scheduler()
    seen: list[str] = []

    async def async_job() -> None:
        seen.append("done")

    s.add("async_job", 1, async_job)
    s.now(at=0)  # no running loop here: fresh loop spins up internally
    s.now(at=1)
    assert seen == ["done", "done"]
    assert s.status()["async_job"]["run_count"] == 2


def test_manual_tick_runs_sync_callable_side_effects() -> None:
    ticked: list[str] = []
    s = Scheduler()
    s.add("a", 1, lambda: ticked.append("a"))
    s.add("b", 2, lambda: ticked.append("b"))
    # At t=0 both are due (next_run None). Registration order === dict
    # order === tick order, so the assertions below are deterministic.
    s.now(at=0)
    assert ticked == ["a", "b"]
    # at t=1 only "a" is due
    s.now(at=1)
    assert ticked == ["a", "b", "a"]
    # at t=2 both are due again (a first, then b - same inventory order)
    s.now(at=2)
    assert ticked == ["a", "b", "a", "a", "b"]


# -- error capture ---------------------------------------------------------
def test_raising_task_recorded_and_loop_survives() -> None:
    s = Scheduler()
    survived: list[str] = []

    def boom() -> None:
        raise RuntimeError("deliberate")

    s.add("bad", 1, boom)
    s.add("good", 1, lambda: survived.append("ok"))
    s.now(at=0)
    assert survived == ["ok"], "failing job must not take the loop/other tasks down"
    info = s.status()["bad"]
    assert info["last_error"] is not None
    assert "RuntimeError" in str(info["last_error"])
    assert "deliberate" in str(info["last_error"])
    assert info["run_count"] == 1
    assert info["next_run"] == 1, "next run stays due after error"
    assert s.status()["good"]["last_error"] is None


def test_repeated_failure_shows_latest_state_only() -> None:
    s = Scheduler()
    counter = {"n": 0}

    def flaky() -> None:
        counter["n"] += 1
        if counter["n"] >= 2:
            raise ValueError("second run dies")

    s.add("flaky", 1, flaky)
    s.now(at=0)
    assert s.status()["flaky"]["last_error"] is None  # first run ok
    s.now(at=1)
    assert "ValueError" in str(s.status()["flaky"]["last_error"])


def test_raising_async_task_loop_survives_in_loop_context() -> None:
    """The battery path must survive a raising async task (loop call)."""
    s = Scheduler()
    survived: list[str] = []

    async def bad_async() -> None:
        raise KeyError("async boom")

    async def drive() -> None:
        s.add("async_bad", 0.01, bad_async)
        s.add("good", 0.01, lambda: survived.append("ok"))
        s.start()
        await asyncio.sleep(0.06)
        await s.stop()

    asyncio.run(drive())
    info = s.status()["async_bad"]
    assert info["last_error"] is not None
    assert "KeyError" in str(info["last_error"])
    assert survived, "the healthy task must keep running after an async failure"


# -- status ---------------------------------------------------------------
def test_status_shape_is_plain_dict() -> None:
    s = Scheduler()
    s.add("job", 3, lambda: None)
    s.now(at=0)
    status = s.status()
    assert set(status) == {"job"}
    info = status["job"]
    assert set(info) == {"name", "interval", "last_run", "next_run", "last_error", "run_count"}
    assert info["name"] == "job"
    assert info["interval"] == 3
    assert info["last_run"] == 0
    assert info["next_run"] == 3
    assert info["run_count"] == 1
    assert info["last_error"] is None
    # JSON-renderable (no exotic types)
    import json

    assert json.loads(json.dumps(status)) == status


def test_start_then_stop_clears_next_run_tracking() -> None:
    """The loop fires real run counts via the same tick engine."""
    s = Scheduler()
    counter = {"n": 0}
    s.add("job", 0.01, lambda: counter.__setitem__("n", counter["n"] + 1))

    async def scenario() -> None:
        s.start()
        await asyncio.sleep(0.1)
        first = s.status()["job"]["run_count"]
        assert int(first) >= 1
        await s.stop()
        second = s.status()["job"]["run_count"]
        assert second >= first

    asyncio.run(scenario())
    assert counter["n"] == s.status()["job"]["run_count"]


def test_taskinfo_projection_is_frozen_shape() -> None:
    info = TaskInfo("x", 1)
    assert info.as_dict() == {
        "name": "x",
        "interval": 1,
        "last_run": None,
        "next_run": None,
        "last_error": None,
        "run_count": 0,
    }
