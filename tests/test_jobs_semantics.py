# PURPOSE: Prove the JobQueue scheduling semantics contract holds (no fakes of
#          the queue itself: real asyncio.Event-driven fake runners only).
# OWNER: lane `jobs` of wave 3.2 (branch feat/3.2-jobs)
# CONSUMES: src.jobs (models, errors, queue).
# PROVIDES: tests/test_jobs_semantics.py.

# PURPOSE: Prove the JobQueue scheduling contract holds, using real asyncio
#          Event-gated fake runners (no mocks of the scheduler itself).
# OWNER: lane `jobs` of wave 3.2 (branch feat/3.2-jobs)
# CONSUMES: src.jobs (models, errors, queue).
# PROVIDES: tests/test_jobs_semantics.py.
# INVARIANTS: no real I/O, no sleeps beyond zero-step drains, no third-party libs.
# EXTEND: new scheduling rules belong here; mechanical/lifecycle checks go to
#          tests/test_jobs_queue.py.

"""Contract tests for JobQueue semantics: serialization, caps, priority, dedupe.

Every scenario drives a real ``JobQueue`` with fake runners gated on
``asyncio.Event`` objects, so assertions observe actual scheduling order rather
than mocks of the scheduler.
"""

from __future__ import annotations

import asyncio

import pytest

from src.jobs.errors import QueueFull
from src.jobs.models import Job, JobResult, JobState, JobSpec
from src.jobs.queue import JobQueue

pytestmark = pytest.mark.asyncio




def spec(user_id: int = 1, command: str = "echo hi", **kw: object) -> JobSpec:
    """Builds a JobSpec with sane test defaults (``123456`` is a fake user id)."""
    return JobSpec(user_id=user_id, alias="self", command=command, **kw)  # type: ignore[arg-type]


class FakeRunner:
    """A runner the tests fully control: gate it, inspect it, make it fail.

    Every active invocation registers itself in ``active_specs`` so tests can
    prove serialization and the global cap from observed concurrency, not from
    promises in the code.
    """

    def __init__(self, result: str = "ok", delay: float = 0.0, error: Exception | None = None) -> None:
        self.result = result
        self.delay = delay
        self.error = error
        self.gate: asyncio.Event | None = None
        self.calls: list[JobSpec] = []
        self.active_specs: list[JobSpec] = []
        self.completions: list[JobSpec] = []

    async def __call__(self, job_spec: JobSpec) -> str:
        self.calls.append(job_spec)
        self.active_specs.append(job_spec)
        try:
            if self.gate is not None:
                await self.gate.wait()
            elif self.delay:
                await asyncio.sleep(self.delay)
            if self.error is not None:
                raise self.error
            return self.result
        finally:
            self.completions.append(job_spec)
            self.active_specs.remove(job_spec)


async def _drain(queue: JobQueue) -> None:
    """Lets the dispatcher make progress without sleeping a fixed interval."""
    for _ in range(10):
        await asyncio.sleep(0)


def _bg(queue: JobQueue, job_spec: JobSpec, runner: object) -> "asyncio.Task[JobResult]":
    """Submits without awaiting: used while another job holds the user's slot.

    A gated runner plus ``await queue.submit`` would deadlock the test itself,
    so anything queued behind a blocker is submitted as a task.
    """
    return asyncio.create_task(queue.submit(job_spec, runner))  # type: ignore[arg-type]


# -- per-user serialization -------------------------------------------------


async def test_one_running_job_per_user_overlap_detector() -> None:
    """Two jobs from one user must never overlap: the detector must stay at 1."""
    queue = JobQueue(max_concurrent=4)
    await queue.start()
    runner = FakeRunner()
    runner.gate = asyncio.Event()
    results = [
        asyncio.create_task(queue.submit(spec(user_id=1, command=f"cmd{i}"), runner))
        for i in range(3)
    ]
    await _drain(queue)
    assert len(runner.active_specs) == 1, "only one job may run for a user"
    while not all(task.done() for task in results):
        assert len(runner.active_specs) <= 1, "jobs for one user overlapped"
        runner.gate.set()
        await asyncio.sleep(0)
        runner.gate = asyncio.Event()
        await _drain(queue)
    assert [job_spec.command for job_spec in runner.calls] == ["cmd0", "cmd1", "cmd2"]
    assert queue.stats()["running"] == 0
    await queue.stop()


async def test_per_user_serialization_with_distinct_users_in_parallel() -> None:
    """Different users run concurrently while each stays serialized."""
    queue = JobQueue(max_concurrent=4)
    await queue.start()
    runners = {user: FakeRunner() for user in (1, 2)}
    for runner in runners.values():
        runner.gate = asyncio.Event()
    tasks = [
        asyncio.create_task(queue.submit(spec(user_id=user, command=f"u{user}-c{i}"), runners[user]))
        for user in (1, 2)
        for i in range(2)
    ]
    await _drain(queue)
    for runner in runners.values():
        assert len(runner.active_specs) == 1, "each user must run exactly one job"
        runner.gate.set()
    await asyncio.gather(*tasks)
    for runner in runners.values():
        assert len(runner.calls) == 2
    await queue.stop()


async def test_user_with_running_job_is_skipped_for_lower_priority() -> None:
    """A busy user's queued job does not block another user's job."""
    queue = JobQueue(max_concurrent=2)
    await queue.start()
    busy = FakeRunner()
    busy.gate = asyncio.Event()
    other = FakeRunner()
    busy_task = asyncio.create_task(queue.submit(spec(user_id=1, command="busy"), busy))
    other_task = asyncio.create_task(queue.submit(spec(user_id=2, command="other"), other))
    queued = asyncio.create_task(
        queue.submit(spec(user_id=1, priority=1, command="queued"), FakeRunner())
    )
    await _drain(queue)
    assert len(other.calls) == 1, "other user must not wait behind the busy user's queue"
    busy.gate.set()
    await asyncio.gather(busy_task, other_task, queued)
    await queue.stop()


# -- global concurrency cap -------------------------------------------------


async def test_global_cap_never_exceeded_max_in_flight() -> None:
    """A max-in-flight counter proves the global cap is respected."""
    queue = JobQueue(max_concurrent=2, max_pending_total=64)
    await queue.start()
    runner = FakeRunner()
    runner.gate = asyncio.Event()
    tasks = [
        asyncio.create_task(queue.submit(spec(user_id=user, command=f"c{user}"), runner))
        for user in range(6)
    ]
    await _drain(queue)
    observed_max = 0
    while not all(task.done() for task in tasks):
        observed_max = max(observed_max, len(runner.active_specs))
        assert len(runner.active_specs) <= 2, "global concurrency cap exceeded"
        runner.gate.set()
        await asyncio.sleep(0)
        runner.gate = asyncio.Event()
        await _drain(queue)
    assert observed_max == 2, "the cap should actually be reached"
    await queue.stop()


async def test_set_runner_concurrency_lowers_live_cap() -> None:
    """``set_runner_concurrency`` takes effect for subsequently started jobs."""
    queue = JobQueue(max_concurrent=4)
    await queue.start()
    runner = FakeRunner()
    runner.gate = asyncio.Event()
    tasks = [asyncio.create_task(queue.submit(spec(user_id=u), runner)) for u in range(4)]
    await _drain(queue)
    assert len(runner.active_specs) == 4
    runner.gate.set()
    await asyncio.gather(*tasks)
    queue.set_runner_concurrency(1)
    runner.gate = asyncio.Event()
    tasks = [asyncio.create_task(queue.submit(spec(user_id=u), runner)) for u in range(4)]
    await _drain(queue)
    while not all(task.done() for task in tasks):
        assert len(runner.active_specs) <= 1, "lowered cap not honored"
        runner.gate.set()
        await asyncio.sleep(0)
        runner.gate = asyncio.Event()
        await _drain(queue)
    await queue.stop()


# -- priority and FIFO ------------------------------------------------------


async def test_priority_order_runs_lower_number_first() -> None:
    """Within one user the queue starts lower ``priority`` numbers first."""
    queue = JobQueue()
    await queue.start()
    runner = FakeRunner()
    priorities = (10, 1, 5)
    tasks = [
        asyncio.create_task(queue.submit(spec(user_id=1, priority=p), runner)) for p in priorities
    ]
    await asyncio.gather(*tasks)
    assert [job_spec.priority for job_spec in runner.calls] == [1, 5, 10]
    await queue.stop()


async def test_fifo_tiebreak_on_equal_priority() -> None:
    """Equal priority must preserve submission order (no reordering jitter)."""
    queue = JobQueue()
    await queue.start()
    runner = FakeRunner()
    tasks = [
        asyncio.create_task(queue.submit(spec(user_id=1, command=f"f{i}"), runner)) for i in range(5)
    ]
    await asyncio.gather(*tasks)
    assert [job_spec.command for job_spec in runner.calls] == ["f0", "f1", "f2", "f3", "f4"]
    await queue.stop()


async def test_list_pending_sorted_by_priority_then_fifo() -> None:
    """``list_pending`` exposes the dispatcher's exact run order."""
    queue = JobQueue()
    await queue.start()
    blocker = FakeRunner()
    blocker.gate = asyncio.Event()
    blocker_task = asyncio.create_task(queue.submit(spec(user_id=1, command="block"), blocker))
    await _drain(queue)
    queued = [
        _bg(queue, spec(user_id=1, command=command, priority=priority), FakeRunner())
        for command, priority in (("low", 10), ("high", 1), ("mid", 5), ("low2", 10))
    ]
    await _drain(queue)
    order = [job.spec.command for job in queue.list_pending()]
    assert order == ["high", "mid", "low", "low2"]
    blocker.gate.set()
    await blocker_task
    await asyncio.gather(*queued)
    await queue.stop()


# -- dedupe -----------------------------------------------------------------


async def test_dedupe_key_runs_once_and_shares_result() -> None:
    """Same ``dedupe_key`` for one user executes once and shares the result."""
    queue = JobQueue()
    await queue.start()
    runner = FakeRunner(result="shared-output")
    runner.gate = asyncio.Event()
    first = _bg(queue, spec(user_id=1, dedupe_key="k1"), runner)
    await _drain(queue)
    second = _bg(queue, spec(user_id=1, dedupe_key="k1"), runner)
    await _drain(queue)
    runner.gate.set()
    results = await asyncio.gather(first, second)
    assert len(runner.calls) == 1, "dedupe_key must not execute the command twice"
    assert results[0].output == results[1].output == "shared-output"
    assert results[0].job is results[1].job
    assert queue.stats()["deduped"] == 1
    assert queue.stats()["done"] == 1
    await queue.stop()


async def test_dedupe_key_scoped_per_user() -> None:
    """The same ``dedupe_key`` from different users is NOT coalesced."""
    queue = JobQueue(max_concurrent=2)
    await queue.start()
    runners = {1: FakeRunner(result="a"), 2: FakeRunner(result="b")}
    results = await asyncio.gather(
        queue.submit(spec(user_id=1, dedupe_key="same"), runners[1]),
        queue.submit(spec(user_id=2, dedupe_key="same"), runners[2]),
    )
    assert [r.output for r in results] == ["a", "b"]
    assert sum(len(r.calls) for r in runners.values()) == 2
    await queue.stop()


async def test_dedupe_key_freed_after_completion_for_resubmission() -> None:
    """Once terminal, a dedupe key no longer coalesces a fresh submission."""
    queue = JobQueue()
    await queue.start()
    runner = FakeRunner()
    await queue.submit(spec(user_id=1, dedupe_key="k"), runner)
    assert len(runner.calls) == 1
    await queue.submit(spec(user_id=1, dedupe_key="k"), runner)
    assert len(runner.calls) == 2, "a finished job's dedupe key must be released"
    await queue.stop()


async def test_dedupe_blocked_runner_exception_still_shares_failure() -> None:
    """A coalesced submission also sees the runner's failure, never an exception."""
    queue = JobQueue()
    await queue.start()
    runner = FakeRunner(error=RuntimeError("boom"))
    runner.gate = asyncio.Event()
    first = _bg(queue, spec(user_id=1, dedupe_key="k"), runner)
    await _drain(queue)
    second = _bg(queue, spec(user_id=1, dedupe_key="k"), runner)
    await _drain(queue)
    runner.gate.set()
    results = await asyncio.gather(first, second)
    assert all(r.exit_ok is False for r in results)
    assert all(r.job.error for r in results)
    assert len(runner.calls) == 1
    await queue.stop()


# -- caps / QueueFull -------------------------------------------------------


async def test_queue_full_per_user_cap() -> None:
    """Exceeding ``max_pending_per_user`` raises QueueFull, not a silent wait."""
    queue = JobQueue(max_concurrent=1, max_pending_per_user=2, max_pending_total=64)
    await queue.start()
    blocker = FakeRunner()
    blocker.gate = asyncio.Event()
    blocker_task = asyncio.create_task(queue.submit(spec(user_id=1), blocker))
    await _drain(queue)
    queued = _bg(queue, spec(user_id=1), FakeRunner())
    await _drain(queue)
    with pytest.raises(QueueFull):
        await queue.submit(spec(user_id=1), FakeRunner())
    blocker.gate.set()
    await blocker_task
    await queued
    await queue.stop()


async def test_queue_full_total_cap() -> None:
    """Exceeding ``max_pending_total`` raises QueueFull even across users."""
    queue = JobQueue(max_concurrent=1, max_pending_per_user=64, max_pending_total=2)
    await queue.start()
    blocker = FakeRunner()
    blocker.gate = asyncio.Event()
    blocker_task = asyncio.create_task(queue.submit(spec(user_id=1), blocker))
    await _drain(queue)
    queued = _bg(queue, spec(user_id=2), FakeRunner())
    await _drain(queue)
    with pytest.raises(QueueFull):
        await queue.submit(spec(user_id=3), FakeRunner())
    blocker.gate.set()
    await blocker_task
    await queued
    await queue.stop()


async def test_queue_full_carries_user_and_limit_context() -> None:
    """QueueFull attributes let handlers build a useful message."""
    queue = JobQueue(max_concurrent=1, max_pending_per_user=1, max_pending_total=64)
    await queue.start()
    blocker = FakeRunner()
    blocker.gate = asyncio.Event()
    blocker_task = asyncio.create_task(queue.submit(spec(user_id=7), blocker))
    await _drain(queue)
    with pytest.raises(QueueFull) as exc_info:
        await queue.submit(spec(user_id=7), FakeRunner())
    assert exc_info.value.user_id == 7
    assert exc_info.value.limit == 1
    blocker.gate.set()
    await blocker_task
    await queue.stop()


async def test_running_job_counts_against_user_pending_cap() -> None:
    """A running job still occupies its user's slot: caps bound total work."""
    queue = JobQueue(max_concurrent=1, max_pending_per_user=1, max_pending_total=64)
    await queue.start()
    blocker = FakeRunner()
    blocker.gate = asyncio.Event()
    blocker_task = asyncio.create_task(queue.submit(spec(user_id=1), blocker))
    await _drain(queue)
    with pytest.raises(QueueFull):
        await queue.submit(spec(user_id=1), FakeRunner())
    blocker.gate.set()
    await blocker_task
    await queue.stop()


# -- cancel -----------------------------------------------------------------


async def test_cancel_queued_job() -> None:
    """Cancelling a queued job resolves its submitter with exit_ok False."""
    queue = JobQueue(max_concurrent=1)
    await queue.start()
    blocker = FakeRunner()
    blocker.gate = asyncio.Event()
    blocker_task = asyncio.create_task(queue.submit(spec(user_id=1, command="block"), blocker))
    await _drain(queue)
    victim = _bg(queue, spec(user_id=1, command="victim"), FakeRunner())
    await _drain(queue)
    assert queue.cancel(1, queue.list_pending(1)[0].id) is True
    result = await victim
    assert result.exit_ok is False
    assert result.job.state is JobState.CANCELLED
    assert result.job.error
    assert queue.list_pending(1) == []
    blocker.gate.set()
    await blocker_task
    await queue.stop()


async def test_cancel_queue_full_after_cancel_frees_slot() -> None:
    """A cancelled queued job frees its user slot for a new submission."""
    queue = JobQueue(max_concurrent=1, max_pending_per_user=2)
    await queue.start()
    blocker = FakeRunner()
    blocker.gate = asyncio.Event()
    blocker_task = asyncio.create_task(queue.submit(spec(user_id=1), blocker))
    await _drain(queue)
    victim = _bg(queue, spec(user_id=1), FakeRunner())
    await _drain(queue)
    with pytest.raises(QueueFull):
        await queue.submit(spec(user_id=1), FakeRunner())
    assert queue.cancel(1, queue.list_pending(1)[0].id) is True
    await victim
    replacement = _bg(queue, spec(user_id=1), FakeRunner())
    await _drain(queue)
    assert queue.stats()["pending"] == 1, "released slot must be reusable"
    with pytest.raises(QueueFull):
        await queue.submit(spec(user_id=1), FakeRunner())
    blocker.gate.set()
    results = await asyncio.gather(blocker_task, replacement)
    assert [r.exit_ok for r in results] == [True, True]
    await queue.stop()


async def test_cancel_unknown_or_foreign_job_returns_false() -> None:
    """``cancel`` is scoped to the calling user and known, non-terminal jobs."""
    queue = JobQueue(max_concurrent=1)
    await queue.start()
    blocker = FakeRunner()
    blocker.gate = asyncio.Event()
    blocker_task = asyncio.create_task(queue.submit(spec(user_id=1), blocker))
    await _drain(queue)
    pending_id = queue.list_pending(1)[0].id if queue.list_pending(1) else "job_unknown"
    assert queue.cancel(999, pending_id) is False
    assert queue.cancel(1, "job_does_not_exist") is False
    blocker.gate.set()
    await blocker_task
    assert queue.cancel(1, blocker_task.result().job.id) is False, "terminal jobs are not cancellable"
    await queue.stop()


async def test_cancel_running_job_stops_it() -> None:
    """Cancelling a running job propagates CancelledError into the runner."""
    queue = JobQueue()
    await queue.start()
    runner = FakeRunner()
    runner.gate = asyncio.Event()
    task = asyncio.create_task(queue.submit(spec(user_id=1), runner))
    await _drain(queue)
    running = queue.active(1)[0]
    assert queue.cancel(1, running.id) is True
    result = await task
    assert result.job.state is JobState.CANCELLED
    assert result.exit_ok is False
    assert queue.stats()["running"] == 0
    await queue.stop()


# -- error containment ------------------------------------------------------


async def test_runner_exception_becomes_failed_result() -> None:
    """A raising runner yields JobResult(exit_ok=False) with Job.error set."""
    queue = JobQueue()
    await queue.start()
    result = await queue.submit(spec(user_id=1, command="bad"), FakeRunner(error=OSError("disk gone")))
    assert result.exit_ok is False
    assert result.job.state is JobState.FAILED
    assert result.job.error is not None
    assert "OSError" in result.job.error
    assert "disk gone" in result.job.error
    await queue.stop()


async def test_runner_timeout_becomes_failed_result() -> None:
    """A runner that overruns ``timeout`` fails instead of hanging the handler."""
    queue = JobQueue()
    await queue.start()
    result = await queue.submit(spec(user_id=1, timeout=0.05), FakeRunner(delay=5.0))
    assert result.exit_ok is False
    assert result.job.state is JobState.FAILED
    assert result.job.error is not None and "timed out" in result.job.error.lower()
    await queue.stop()


async def test_asyncio_timeouterror_shape_is_contained() -> None:
    """``asyncio.TimeoutError`` (a BaseException group in 3.12+) is contained."""
    queue = JobQueue()
    await queue.start()

    class _Boom:
        async def __call__(self, job_spec: JobSpec) -> str:
            raise asyncio.TimeoutError()

    result = await queue.submit(spec(user_id=1, timeout=0.0), _Boom())
    assert result.exit_ok is False and result.job.error
    await queue.stop()


async def test_runner_returning_non_string_is_coerced() -> None:
    """Runners may return bytes/None; the result output is always ``str``."""
    queue = JobQueue()
    await queue.start()

    class _Bytes:
        async def __call__(self, job_spec: JobSpec) -> bytes:
            return b"raw"

    result = await queue.submit(spec(user_id=1), _Bytes())
    assert result.output == "raw"
    assert result.job.output_bytes == 3
    await queue.stop()


# -- stop / cleanup ---------------------------------------------------------


async def test_stop_leaves_no_pending_tasks_and_resolves_queued() -> None:
    """``stop`` cancels queued jobs, frees tasks and settles every submitter."""
    queue = JobQueue(max_concurrent=1)
    await queue.start()
    blocker = FakeRunner()
    blocker.gate = asyncio.Event()
    blocker_task = asyncio.create_task(queue.submit(spec(user_id=1), blocker))
    await _drain(queue)
    queued = asyncio.create_task(queue.submit(spec(user_id=1), FakeRunner()))
    await _drain(queue)
    before = {id(t) for t in asyncio.all_tasks()}
    await queue.stop()
    assert not queue.list_pending(), "queued jobs must be dropped on stop"
    await _drain(queue)
    assert queued.done() is True
    queued_result = queued.result()
    assert queued_result.exit_ok is False
    assert queued_result.job.state is JobState.CANCELLED
    after = [task for task in asyncio.all_tasks() if id(task) not in before and not task.done()]
    assert after == [], f"stop leaked tasks: {after!r}"
    blocker_result = blocker_task.result()
    assert blocker_result.job.state is JobState.CANCELLED
    await queue.stop()


async def test_stop_is_idempotent_and_rejects_new_submissions() -> None:
    """Calling stop twice is safe; afterwards submit raises RuntimeError."""
    queue = JobQueue()
    await queue.start()
    await queue.submit(spec(user_id=1), FakeRunner())
    await queue.stop()
    await queue.stop()
    with pytest.raises(RuntimeError):
        await queue.submit(spec(user_id=1), FakeRunner())
    assert queue.stats()["running"] == 0


async def test_stop_awaits_briefly_then_cancels_unbounded_runner() -> None:
    """A runner that never finishes is cancelled after the grace window."""
    queue = JobQueue()
    queue._stop_grace = 0.05
    await queue.start()

    class _Forever:
        def __init__(self) -> None:
            self.cancelled = False

        async def __call__(self, job_spec: JobSpec) -> str:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    forever = _Forever()
    task = asyncio.create_task(queue.submit(spec(user_id=1), forever))
    await _drain(queue)
    await queue.stop()
    result = await task
    assert forever.cancelled is True
    assert result.job.state is JobState.CANCELLED
    assert queue.stats()["running"] == 0


# -- stats / inspection / metrics -------------------------------------------


async def test_stats_reports_full_lifecycle_counts() -> None:
    """``stats`` counts every terminal state plus backlog gauges."""
    queue = JobQueue(max_concurrent=1, max_pending_per_user=8, max_pending_total=64)
    await queue.start()
    await queue.submit(spec(user_id=1), FakeRunner(result="a"))
    await queue.submit(spec(user_id=1), FakeRunner(error=ValueError("x")))
    blocker = FakeRunner()
    blocker.gate = asyncio.Event()
    blocker_task = asyncio.create_task(queue.submit(spec(user_id=1), blocker))
    await _drain(queue)
    dup_a = _bg(queue, spec(user_id=1, dedupe_key="d"), FakeRunner())
    dup_b = _bg(queue, spec(user_id=1, dedupe_key="d"), FakeRunner())
    await _drain(queue)
    stats = queue.stats()
    assert stats["submitted"] == 4
    assert stats["done"] == 1
    assert stats["failed"] == 1
    assert stats["deduped"] == 1
    assert stats["running"] == 1
    assert stats["pending"] == 1
    assert stats["outstanding"] == 2
    assert stats["pending_users"] == 1
    blocker.gate.set()
    await asyncio.gather(blocker_task, dup_a, dup_b)
    await _drain(queue)
    final = queue.stats()
    assert final["done"] == 3, "blocker and dedupe prime job also succeed"
    assert final["failed"] == 1
    assert final["cancelled"] == 0
    assert final["running"] == 0
    assert final["pending"] == 0
    await queue.stop()


async def test_active_lists_running_jobs_only() -> None:
    """``active`` returns running jobs and filters by user."""
    queue = JobQueue(max_concurrent=2)
    await queue.start()
    runners = {1: FakeRunner(), 2: FakeRunner()}
    for runner in runners.values():
        runner.gate = asyncio.Event()
    tasks = [
        asyncio.create_task(queue.submit(spec(user_id=u, command=f"u{u}"), runners[u])) for u in (1, 2)
    ]
    await _drain(queue)
    assert [job.spec.user_id for job in queue.active()] == [1, 2]
    assert [job.spec.command for job in queue.active(2)] == ["u2"]
    assert queue.active(999) == []
    for runner in runners.values():
        runner.gate.set()
    await asyncio.gather(*tasks)
    assert queue.active() == [], "no jobs remain active once finished"
    await queue.stop()


async def test_metric_hook_receives_lifecycle_events() -> None:
    """The optional ``on_metric`` hook sees submit/start/finish transitions."""
    events: list[tuple[str, dict[str, object]]] = []
    queue = JobQueue(on_metric=lambda name, labels: events.append((name, dict(labels))))
    await queue.start()
    result = await queue.submit(spec(user_id=1, priority=3), FakeRunner())
    names = [name for name, _ in events]
    assert "jobs_submitted" in names and "jobs_started" in names and "jobs_finished" in names
    finish = next(dict(l) for n, l in events if n == "jobs_finished")
    assert finish["exit_ok"] is True and finish["state"] == JobState.DONE.value
    assert result.job.state is JobState.DONE
    await queue.stop()


async def test_metric_hook_failure_never_breaks_job() -> None:
    """A broken metric hook cannot break job execution."""
    calls: list[str] = []

    def broken(name: str, labels: object) -> None:
        calls.append(name)
        raise RuntimeError("metrics down")

    queue = JobQueue(on_metric=broken)
    await queue.start()
    result = await queue.submit(spec(user_id=1), FakeRunner())
    assert result.exit_ok is True
    assert calls, "the hook was still invoked"
    await queue.stop()


# -- constructor guards -----------------------------------------------------


async def test_constructor_rejects_invalid_caps() -> None:
    """Nonsensical caps are a programming error: fail at construction."""
    with pytest.raises(ValueError):
        JobQueue(max_concurrent=0)
    with pytest.raises(ValueError):
        JobQueue(max_pending_per_user=0)
    with pytest.raises(ValueError):
        JobQueue(max_pending_total=0)


async def test_null_runner_rejected_and_disconnected_spec_handled() -> None:
    """``runner`` must be callable; a None metadata mapping still works."""
    queue = JobQueue()
    await queue.start()
    with pytest.raises(TypeError):
        await queue.submit(spec(user_id=1), None)  # type: ignore[arg-type]
    result = await queue.submit(
        spec(user_id=1, command="echo", timeout=0.0, metadata={}), FakeRunner(result="x")
    )
    assert result.output == "x" and result.exit_ok is True
    await queue.stop()


# -- model sanity -----------------------------------------------------------


def test_job_spec_defaults_and_immutability() -> None:
    """JobSpec defaults match the contract and frozen specs cannot drift."""
    job_spec = JobSpec(user_id=1, alias="self", command="echo hi")
    assert job_spec.priority == 10
    assert job_spec.timeout == 60.0
    assert job_spec.dedupe_key is None
    assert job_spec.label == ""
    assert dict(job_spec.metadata) == {}
    with pytest.raises(Exception):
        job_spec.priority = 1  # type: ignore[misc]


def test_job_result_carries_duration_and_output_bytes() -> None:
    """JobResult is a frozen snapshot a handler can render with no queue access."""
    job = Job(
        id="job_abc",
        spec=JobSpec(user_id=1, alias="self", command="echo hi"),
        state=JobState.DONE,
        created_at=1.0,
        started_at=1.5,
        finished_at=2.5,
        exit_ok=True,
        output_bytes=2,
        error=None,
    )
    result = JobResult(job=job, output="hi", exit_ok=True, duration=1.0)
    assert result.job is job and result.duration == 1.0
    assert JobState("done") is JobState.DONE
