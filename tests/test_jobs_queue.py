# PURPOSE: Prove JobQueue mechanics (lifecycle, task hygiene, API surface)
#          hold independently of the scheduling-semantics suite.
# OWNER: lane `jobs` of wave 3.2 (branch feat/3.2-jobs)
# CONSUMES: src.jobs (models, errors, queue).
# PROVIDES: tests/test_jobs_queue.py.

# PURPOSE: Prove JobQueue mechanics (start/stop hygiene, leaked-task audits,
#          public API surface, backpressure plumbing) independently of semantics.
# OWNER: lane `jobs` of wave 3.2 (branch feat/3.2-jobs)
# CONSUMES: src.jobs (models, errors, queue).
# PROVIDES: tests/test_jobs_queue.py.
# INVARIANTS: no real I/O, no threads, no third-party libs.
# EXTEND: lifecycle and API-surface checks go here; scheduling order rules go
#          to tests/test_jobs_semantics.py.

"""Lifecycle and mechanical tests for JobQueue.

Paired with ``test_jobs_semantics.py``: this file owns start/stop hygiene
(zero leaked tasks, idempotency, restart), the public API surface the
orchestrator wires, and backpressure/metric plumbing.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from src.jobs.errors import QueueFull
from src.jobs.models import JobResult, JobState, JobSpec
from src.jobs.queue import JobQueue

pytestmark = pytest.mark.asyncio


def spec(user_id: int = 1, command: str = "echo hi", **kw: object) -> JobSpec:
    """Builds a JobSpec with sane test defaults."""
    return JobSpec(user_id=user_id, alias="self", command=command, **kw)  # type: ignore[arg-type]


class FakeRunner:
    """A runner the tests gate with ``asyncio.Event``; no real I/O anywhere."""

    def __init__(self, result: str = "ok", error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.gate: asyncio.Event | None = None
        self.calls: list[JobSpec] = []

    async def __call__(self, job_spec: JobSpec) -> str:
        self.calls.append(job_spec)
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return self.result


async def _drain(loop_steps: int = 10) -> None:
    """Yields so dispatcher/runner tasks make progress (no fixed sleeps)."""
    for _ in range(loop_steps):
        await asyncio.sleep(0)


def _submit_bg(queue: JobQueue, job_spec: JobSpec, runner: object) -> "asyncio.Task[JobResult]":
    """Submits as a task so a gated runner cannot deadlock the test itself."""
    return asyncio.create_task(queue.submit(job_spec, runner))  # type: ignore[arg-type]


# -- start/stop lifecycle ---------------------------------------------------


async def test_start_creates_one_dispatcher_task() -> None:
    """``start`` schedules exactly one dispatcher; repeat calls are no-ops."""
    queue = JobQueue()
    await queue.start()
    workers = [task for task in asyncio.all_tasks() if "jobs-dispatcher" in task.get_name()]
    assert len(workers) == 1
    await queue.start()
    assert len([t for t in asyncio.all_tasks() if "jobs-dispatcher" in t.get_name()]) == 1
    await queue.stop()


async def test_start_is_idempotent_when_dispatcher_finished() -> None:
    """A fresh ``start`` after the loop drained re-arms the queue."""
    queue = JobQueue()
    await queue.start()
    await queue.stop()
    await queue.start()
    result = await queue.submit(spec(user_id=1), FakeRunner(result="again"))
    assert result.output == "again"
    await queue.stop()


async def test_stop_with_empty_queue_leaks_no_tasks() -> None:
    """Stopping an idle queue must leave only the test's own task behind."""
    queue = JobQueue()
    await queue.start()
    await _drain()
    before = set(asyncio.all_tasks())
    await queue.stop()
    leaked = [task for task in asyncio.all_tasks() - before if not task.done()]
    assert leaked == [], f"stop leaked tasks: {leaked!r}"
    assert queue.stats()["pending"] == 0
    assert queue.stats()["running"] == 0


async def test_stop_after_busy_queue_leaks_no_tasks() -> None:
    """Stopping after real work has run still leaves no dispatcher/runner tasks."""
    queue = JobQueue(max_concurrent=2)
    await queue.start()
    await asyncio.gather(*[queue.submit(spec(user_id=u), FakeRunner()) for u in range(4)])
    before = set(asyncio.all_tasks())
    await queue.stop()
    leaked = [task for task in asyncio.all_tasks() - before if not task.done()]
    assert leaked == []
    assert queue.stats()["done"] == 4


async def test_stop_leaves_no_pending_tasks_with_in_flight_work() -> None:
    """``stop`` with running plus queued work drops the queue and awaits the rest."""
    queue = JobQueue(max_concurrent=2)
    await queue.start()
    gated = FakeRunner()
    gated.gate = asyncio.Event()
    running = _submit_bg(queue, spec(user_id=1), gated)
    running2 = _submit_bg(queue, spec(user_id=2), gated)
    queued = _submit_bg(queue, spec(user_id=3), FakeRunner())
    await _drain()
    assert len(queue.active()) == 2
    before = set(asyncio.all_tasks())
    await queue.stop()
    leaked = [task for task in asyncio.all_tasks() - before if not task.done()]
    assert leaked == [], f"leaked: {leaked!r}"
    assert queued.done() and running.done() and running2.done()
    assert queued.result().job.state is JobState.CANCELLED
    assert queue.stats()["running"] == 0
    assert queue.list_pending() == []


async def test_stop_releases_waiting_submitters() -> None:
    """No submitter may stay blocked on a future that stop leaves unsettled."""
    queue = JobQueue(max_concurrent=1)
    await queue.start()
    blocker = FakeRunner()
    blocker.gate = asyncio.Event()
    _submit_bg(queue, spec(user_id=1), blocker)
    await _drain()
    waiters = [_submit_bg(queue, spec(user_id=1), FakeRunner()) for _ in range(3)]
    await _drain()
    await queue.stop()
    outcomes = await asyncio.gather(*waiters)
    assert all(result.exit_ok is False for result in outcomes)
    assert all(result.job.state is JobState.CANCELLED for result in outcomes)


async def test_stop_cancels_unbounded_runner_and_jobs_settle() -> None:
    """A never-returning runner is cancelled within the grace window."""
    queue = JobQueue()
    queue._stop_grace = 0.05
    await queue.start()
    task = _submit_bg(queue, spec(user_id=1), _UnboundedRunner())
    await _drain()
    await queue.stop()
    result = await task
    assert result.job.state is JobState.CANCELLED
    assert result.exit_ok is False
    assert result.job.error


class _UnboundedRunner:
    """Sleeps forever but records that it observed cancellation."""

    def __init__(self) -> None:
        self.cancelled = False

    async def __call__(self, job_spec: JobSpec) -> str:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return "never"


# -- public API surface (contract names) ------------------------------------


async def test_contract_method_signatures() -> None:
    """The orchestrator wires these names; the signatures must match exactly."""
    expected = {
        "start": ["self"],
        "stop": ["self"],
        "submit": ["self", "spec", "runner"],
        "list_pending": ["self", "user_id"],
        "active": ["self", "user_id"],
        "cancel": ["self", "user_id", "job_id"],
        "stats": ["self"],
        "set_runner_concurrency": ["self", "value"],
    }
    for name, params in expected.items():
        method = getattr(JobQueue, name)
        assert inspect.iscoroutinefunction(method) is (name in {"start", "stop", "submit"}), name
        assert list(inspect.signature(method).parameters) == params, name
    init = list(inspect.signature(JobQueue.__init__).parameters)
    assert init == [
        "self",
        "max_concurrent",
        "max_pending_per_user",
        "max_pending_total",
        "on_metric",
    ], "JobQueue.__init__ keyword-only contract changed"


async def test_constructor_defaults_match_contract() -> None:
    """Default caps are exactly the contract's documented defaults."""
    defaults = inspect.signature(JobQueue.__init__).parameters
    assert defaults["max_concurrent"].default == 4
    assert defaults["max_pending_per_user"].default == 8
    assert defaults["max_pending_total"].default == 64
    assert defaults["on_metric"].default is None


async def test_submit_resolves_with_jobresult_and_duration() -> None:
    """A clean run returns DONE with a non-negative measured duration."""
    queue = JobQueue()
    await queue.start()
    result = await queue.submit(spec(user_id=1, timeout=0.0), FakeRunner(result="done"))
    assert isinstance(result, JobResult)
    assert result.exit_ok is True
    assert result.job.state is JobState.DONE
    assert result.job.exit_ok is True
    assert result.job.error is None
    assert result.job.output_bytes == len("done")
    assert result.duration >= 0.0
    assert result.job.started_at is not None and result.job.finished_at is not None
    await queue.stop()


async def test_timezone_of_job_timestamps_is_monotonic_for_ordering() -> None:
    """Started/finished timestamps are wall-clock and non-decreasing."""
    queue = JobQueue()
    await queue.start()
    first = await queue.submit(spec(user_id=1), FakeRunner())
    second = await queue.submit(spec(user_id=1), FakeRunner())
    assert second.job.started_at is not None and first.job.finished_at is not None
    assert second.job.started_at >= first.job.finished_at
    await queue.stop()


async def test_list_pending_and_active_empty_by_default() -> None:
    """A fresh queue exposes no phantom pending or active jobs."""
    queue = JobQueue()
    await queue.start()
    assert queue.list_pending() == []
    assert queue.active() == []
    assert queue.list_pending(1) == []
    assert queue.active(1) == []
    await queue.stop()


async def test_jobs_run_again_after_all_slots_drain() -> None:
    """Throughput is preserved: n jobs on cap-2 queue still finish all n."""
    queue = JobQueue(max_concurrent=2)
    await queue.start()
    results = await asyncio.gather(
        *[queue.submit(spec(user_id=u), FakeRunner(result=f"r{u}")) for u in range(6)]
    )
    assert [result.output for result in results] == [f"r{u}" for u in range(6)]
    assert queue.stats()["done"] == 6
    assert queue.stats()["outstanding"] == 0
    await queue.stop()


# -- backpressure ------------------------------------------------------------


async def test_pending_total_cap_counts_running_jobs() -> None:
    """A running job consumes one of ``max_pending_total``; backlog stays bounded."""
    queue = JobQueue(max_concurrent=1, max_pending_per_user=64, max_pending_total=3)
    await queue.start()
    gated = FakeRunner()
    gated.gate = asyncio.Event()
    _submit_bg(queue, spec(user_id=1), gated)
    await _drain()
    fillers = [_submit_bg(queue, spec(user_id=u), FakeRunner()) for u in (2, 3)]
    await _drain()
    with pytest.raises(QueueFull):
        await queue.submit(spec(user_id=4), FakeRunner())
    gated.gate.set()
    await asyncio.gather(*fillers)
    assert queue.stats()["done"] == 3
    await queue.stop()


async def test_starved_submission_runs_after_a_slot_frees() -> None:
    """A submission rejected moments ago is NOT cached: retrying works."""
    queue = JobQueue(max_concurrent=1, max_pending_per_user=1)
    await queue.start()
    gated = FakeRunner()
    gated.gate = asyncio.Event()
    gated_task = _submit_bg(queue, spec(user_id=1), gated)
    await _drain()
    with pytest.raises(QueueFull):
        await queue.submit(spec(user_id=1), FakeRunner())
    gated.gate.set()
    await gated_task
    result = await queue.submit(spec(user_id=1), FakeRunner(result="later"))
    assert result.output == "later"
    await queue.stop()


async def test_reject_counter_reflected_in_stats() -> None:
    """Rejections surface in stats so operators can see backpressure."""
    queue = JobQueue(max_concurrent=1, max_pending_per_user=1)
    await queue.start()
    gated = FakeRunner()
    gated.gate = asyncio.Event()
    _submit_bg(queue, spec(user_id=1), gated)
    await _drain()
    with pytest.raises(QueueFull):
        await queue.submit(spec(user_id=1), FakeRunner())
    assert queue.stats()["rejected"] == 1
    gated.gate.set()
    await _drain()
    await queue.stop()


# -- metrics integration -----------------------------------------------------


async def test_on_metric_receives_dict_labels() -> None:
    """The metric hook gets (name, labels-dict) and covers a full run."""
    seen: list[tuple[str, dict[str, object]]] = []
    queue = JobQueue(on_metric=lambda name, labels: seen.append((name, dict(labels))))
    await queue.start()
    await queue.submit(spec(user_id=1, priority=2), FakeRunner())
    names = [name for name, _ in seen]
    assert names[0] == "jobs_submitted"
    assert "jobs_started" in names
    assert names[-1] == "jobs_finished"
    assert all(isinstance(labels, dict) for _, labels in seen)
    await queue.stop()


async def test_no_metric_hook_and_none_output_still_works() -> None:
    """The queue runs fine without a hook and with an empty runner result."""
    queue = JobQueue()
    await queue.start()

    class _Empty:
        async def __call__(self, job_spec: JobSpec) -> str:
            return ""

    result = await queue.submit(spec(user_id=1), _Empty())
    assert result.exit_ok is True and result.output == "" and result.job.output_bytes == 0
    await queue.stop()


# -- concurrency change at runtime -------------------------------------------


async def test_set_runner_concurrency_rejects_zero() -> None:
    """A zero cap would stall the queue forever: refuse it."""
    queue = JobQueue()
    await queue.start()
    with pytest.raises(ValueError):
        queue.set_runner_concurrency(0)
    await queue.stop()


async def test_raising_concurrency_unblocks_waiting_jobs() -> None:
    """Raising the live cap lets already-queued jobs start immediately."""
    queue = JobQueue(max_concurrent=1)
    await queue.start()
    gated = FakeRunner()
    gated.gate = asyncio.Event()
    first = _submit_bg(queue, spec(user_id=1), gated)
    second = _submit_bg(queue, spec(user_id=2), FakeRunner())
    await _drain()
    assert len(queue.active()) == 1
    queue.set_runner_concurrency(2)
    for _ in range(100):
        await asyncio.sleep(0)
        if len(queue.active()) == 2:
            break
    assert len(queue.active()) == 2, "raised cap must start the waiting job"
    gated.gate.set()
    await asyncio.gather(first, second)
    await queue.stop()


# -- package export surface ---------------------------------------------------


async def test_package_exports_contract_names() -> None:
    """``src.jobs`` re-exports exactly the names the orchestrator imports."""
    import src.jobs

    assert set(src.jobs.__all__) == {
        "Job",
        "JobQueue",
        "JobResult",
        "JobState",
        "JobSpec",
        "QueueFull",
    }
    for name in src.jobs.__all__:
        assert hasattr(src.jobs, name), name
    assert issubclass(src.jobs.QueueFull, Exception)


