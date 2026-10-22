# PURPOSE: The serialized, capacity-capped async job queue that every command
#          runs through (one running job per user, global cap, priority+FIFO).
# OWNER: lane `jobs` of wave 3.2 (branch feat/3.2-jobs)
# CONSUMES: src.jobs.models, src.jobs.errors (nothing else, no third-party libs).
# PROVIDES: JobQueue(start/stop/submit/list_pending/active/cancel/stats/
#           set_runner_concurrency) and the optional metric hook contract.
# INVARIANTS: pure asyncio on ONE event loop (no threads, no locks needed);
#             submit() never raises a runner exception; stop() leaves no tasks.
# EXTEND: new scheduling policies belong in _pick(); new counters in stats().

"""Capacity-capped, per-user-serialized asyncio job queue.

WHY this exists: the legacy bot awaited one SSH command inside the update
handler, so a slow box blocked a user and two users could trample each other.
The queue gives every command a scheduling slot: it serializes per user,
shares a bounded global concurrency window, keeps lower ``priority`` first,
coalesces duplicate work through ``dedupe_key`` and always resolves to a
``JobResult`` instead of leaking an exception into the Telegram handler.

The implementation is deliberately single-threaded: all mutable state is only
touched from the owning event loop, and the only await inside the scheduler is
the wake-up event, so no locks or thread hand-offs are needed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping

from src.jobs.errors import QueueFull
from src.jobs.models import Job, JobResult, JobState, JobSpec

logger = logging.getLogger(__name__)

#: A runner receives the spec and resolves to the command output (or raises).
Runner = Callable[[JobSpec], Awaitable[str]]

#: How long ``stop()`` lets in-flight runners finish before cancelling them.
DEFAULT_STOP_GRACE = 5.0


@contextlib.asynccontextmanager
async def _deadline(seconds: float) -> AsyncIterator[None]:
    """Bounds a runner by ``seconds`` only when a positive timeout was asked for."""
    if seconds and seconds > 0:
        async with asyncio.timeout(seconds):
            yield
    else:
        yield


def _coerce_output(raw: object) -> str:
    """Normalizes a runner's return value to ``str`` so byte accounting is exact."""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return str(raw)


class JobQueue:
    """Schedules command jobs with per-user serialization and bounded backlog.

    Capacity model: a job counts against both caps from ``submit`` until it is
    terminal, so ``max_pending_per_user``/``max_pending_total`` bound the total
    outstanding work (queued *and* running) rather than only the waiting line.
    Exceeding either raises :class:`~src.jobs.errors.QueueFull` immediately --
    refusing work is always better than an unbounded backlog.
    """

    def __init__(
        self,
        *,
        max_concurrent: int = 4,
        max_pending_per_user: int = 8,
        max_pending_total: int = 64,
        on_metric: Callable[[str, Mapping[str, object]], None] | None = None,
    ) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        if max_pending_per_user < 1:
            raise ValueError("max_pending_per_user must be >= 1")
        if max_pending_total < 1:
            raise ValueError("max_pending_total must be >= 1")
        self._max_concurrent = int(max_concurrent)
        self._max_pending_per_user = int(max_pending_per_user)
        self._max_pending_total = int(max_pending_total)
        self._on_metric = on_metric
        self._stop_grace = DEFAULT_STOP_GRACE

        self._pending: dict[str, Job] = {}
        self._running: dict[str, Job] = {}
        self._running_by_user: dict[int, str] = {}
        self._running_tasks: dict[str, asyncio.Task[None]] = {}
        self._runners: dict[str, Runner] = {}
        self._futures: dict[str, asyncio.Future[JobResult]] = {}
        self._dedupe: dict[tuple[int, str], str] = {}
        self._outstanding_by_user: dict[int, int] = {}
        self._seq: dict[str, int] = {}
        self._started_mono: dict[str, float] = {}
        self._next_seq = 0

        self._counts: dict[str, int] = {state.value: 0 for state in JobState}
        self._finalized: set[str] = set()
        self._submitted = 0
        self._deduped = 0
        self._rejected = 0

        self._wake = asyncio.Event()
        self._dispatcher: asyncio.Task[None] | None = None
        self._closing = False
        self._closed = False

    # -- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        """Starts the dispatcher task; idempotent so wiring can call it eagerly."""
        if self._dispatcher is not None and not self._dispatcher.done():
            return
        self._closed = False
        self._closing = False
        self._dispatcher = asyncio.create_task(self._dispatch_loop(), name="jobs-dispatcher")
        logger.debug("job queue started (max_concurrent=%d)", self._max_concurrent)

    async def stop(self) -> None:
        """Shuts the queue down: drops queued work, waits briefly, leaks nothing.

        Every waiting submitter is resolved (with ``exit_ok=False``) so no
        Telegram handler can be left awaiting a future that will never settle.
        """
        self._closing = True
        self._closed = True
        self._wake.set()

        for job in list(self._pending.values()):
            self._finalize(
                job, JobState.CANCELLED, False, "Queue stopped before the job started", ""
            )

        tasks = [task for task in self._running_tasks.values() if not task.done()]
        if tasks:
            _done, still_running = await asyncio.wait(tasks, timeout=self._stop_grace)
            for task in still_running:
                task.cancel()
            if still_running:
                await asyncio.gather(*still_running, return_exceptions=True)

        dispatcher, self._dispatcher = self._dispatcher, None
        if dispatcher is not None and not dispatcher.done():
            dispatcher.cancel()
            await asyncio.gather(dispatcher, return_exceptions=True)

        for job in [*self._running.values(), *self._pending.values()]:
            self._finalize(job, JobState.CANCELLED, False, "Queue stopped", "")
        self._running_tasks.clear()
        logger.debug("job queue stopped; stats=%s", self.stats())

    # -- submission ------------------------------------------------------
    async def submit(self, spec: JobSpec, runner: Runner) -> JobResult:
        """Queues one job and resolves to its result (never raises runner errors).

        Raises :class:`QueueFull` when a cap would be exceeded and
        ``RuntimeError`` when the queue has been stopped. A duplicate
        ``dedupe_key`` for the same user joins the existing job's future, so the
        command is executed once and both callers see the same result.
        """
        if not callable(runner):
            raise TypeError("runner must be callable")
        if self._closed:
            raise RuntimeError("JobQueue has been stopped")
        if self._dispatcher is None or self._dispatcher.done():
            await self.start()

        dedupe_key = (spec.user_id, spec.dedupe_key) if spec.dedupe_key else None
        if dedupe_key is not None:
            existing = self._dedupe.get(dedupe_key)
            if existing is not None:
                pending_future = self._futures.get(existing)
                if pending_future is not None:
                    self._deduped += 1
                    self._emit("jobs_deduped", priority=spec.priority)
                    return await asyncio.shield(pending_future)

        outstanding = self._outstanding_by_user.get(spec.user_id, 0)
        if outstanding >= self._max_pending_per_user:
            self._rejected += 1
            self._emit("jobs_rejected", reason="user")
            raise QueueFull(
                f"User {spec.user_id} already has {outstanding} job(s) scheduled "
                f"(limit {self._max_pending_per_user}).",
                user_id=spec.user_id,
                limit=self._max_pending_per_user,
            )
        if len(self._pending) + len(self._running) >= self._max_pending_total:
            self._rejected += 1
            self._emit("jobs_rejected", reason="total")
            raise QueueFull(
                f"Queue is full ({self._max_pending_total} jobs scheduled). Try again shortly.",
                user_id=spec.user_id,
                limit=self._max_pending_total,
            )

        job = Job(
            id=f"job_{uuid.uuid4().hex[:12]}",
            spec=spec,
            state=JobState.QUEUED,
            created_at=time.time(),
            started_at=None,
            finished_at=None,
            exit_ok=None,
            output_bytes=0,
            error=None,
        )
        future: asyncio.Future[JobResult] = asyncio.get_running_loop().create_future()
        self._pending[job.id] = job
        self._runners[job.id] = runner
        self._futures[job.id] = future
        self._seq[job.id] = self._next_seq
        self._next_seq += 1
        self._outstanding_by_user[spec.user_id] = outstanding + 1
        if dedupe_key is not None:
            self._dedupe[dedupe_key] = job.id
        self._submitted += 1
        self._emit("jobs_submitted", priority=spec.priority)
        logger.debug("job %s queued (user=%s, priority=%d)", job.id, spec.user_id, spec.priority)

        self._wake.set()
        return await asyncio.shield(future)

    # -- inspection ------------------------------------------------------
    def list_pending(self, user_id: int | None = None) -> list[Job]:
        """Queued jobs in the order they will run (priority, then FIFO)."""
        jobs = [
            job for job in self._pending.values() if user_id is None or job.spec.user_id == user_id
        ]
        return sorted(jobs, key=lambda job: (job.spec.priority, self._seq.get(job.id, 0)))

    def active(self, user_id: int | None = None) -> list[Job]:
        """Currently running jobs (at most one per user).

        Ordered by submission sequence, not by wall-clock ``started_at``:
        concurrently-started jobs can share a ``time.time()`` tick and the
        random job id is not a stable ordering key.
        """
        jobs = [
            job for job in self._running.values() if user_id is None or job.spec.user_id == user_id
        ]
        return sorted(jobs, key=lambda job: self._seq.get(job.id, 0))

    def cancel(self, user_id: int, job_id: str) -> bool:
        """Cancels one of ``user_id``'s jobs; False when it is unknown or terminal.

        Queued jobs are dropped immediately (their submitter resolves with
        ``exit_ok=False``); running jobs are asked to cancel, which their runner
        observes as :class:`asyncio.CancelledError`.
        """
        job = self._pending.get(job_id)
        if job is not None and job.spec.user_id == user_id:
            self._finalize(job, JobState.CANCELLED, False, "Cancelled before start", "")
            return True
        running = self._running.get(job_id)
        if running is not None and running.spec.user_id == user_id:
            task = self._running_tasks.get(job_id)
            if task is not None and not task.done():
                task.cancel()
                return True
        return False

    def stats(self) -> dict[str, int]:
        """A flat integer snapshot for /status, health probes and metrics."""
        series: dict[str, int] = {
            "pending": len(self._pending),
            "running": len(self._running),
            "outstanding": len(self._pending) + len(self._running),
            "pending_users": len({job.spec.user_id for job in self._pending.values()}),
            "done": self._counts[JobState.DONE.value],
            "failed": self._counts[JobState.FAILED.value],
            "cancelled": self._counts[JobState.CANCELLED.value],
            "submitted": self._submitted,
            "deduped": self._deduped,
            "rejected": self._rejected,
            "max_concurrent": self._max_concurrent,
            "max_pending_per_user": self._max_pending_per_user,
            "max_pending_total": self._max_pending_total,
        }
        series["per_user"] = self.stats_per_user()  # type: ignore[assignment]
        return series

    def stats_per_user(self) -> dict[int, dict[str, int]]:
        """Per-user {user_id: {running, pending}} breakdown of outstanding work."""
        breakdown: dict[int, dict[str, int]] = {}
        for job in list(self._pending.values()) + list(self._running.values()):
            entry = breakdown.setdefault(job.spec.user_id, {"running": 0, "pending": 0})
            if job.state is JobState.RUNNING:
                entry["running"] += 1
            else:
                entry["pending"] += 1
        return breakdown

    def set_runner_concurrency(self, value: int) -> None:
        """Changes the global cap at runtime (e.g. a /settings toggle)."""
        if value < 1:
            raise ValueError("concurrency must be >= 1")
        self._max_concurrent = int(value)
        self._wake.set()

    # -- scheduler -------------------------------------------------------
    async def _dispatch_loop(self) -> None:
        """Reserves slots for runnable jobs; the only await is the wake-up event."""
        while not self._closing:
            self._wake.clear()
            job = self._pick()
            if job is None:
                await self._wake.wait()
                continue
            try:
                self._reserve(job)
            except Exception:  # pragma: no cover - defensive: never stall the queue
                logger.exception("failed to schedule job %s", job.id)
                self._finalize(job, JobState.FAILED, False, "Failed to schedule job", "")

    def _pick(self) -> Job | None:
        """Lowest ``priority`` runnable job, FIFO on ties, skipping busy users."""
        if len(self._running) >= self._max_concurrent:
            return None
        best: Job | None = None
        best_key: tuple[int, int] | None = None
        for job in self._pending.values():
            if job.spec.user_id in self._running_by_user:
                continue
            key = (job.spec.priority, self._seq.get(job.id, 0))
            if best_key is None or key < best_key:
                best, best_key = job, key
        return best

    def _reserve(self, job: Job) -> None:
        """Moves a job to RUNNING and spawns its execution task (no awaits)."""
        self._pending.pop(job.id, None)
        job.state = JobState.RUNNING
        job.started_at = time.time()
        self._started_mono[job.id] = time.monotonic()
        self._running[job.id] = job
        self._running_by_user[job.spec.user_id] = job.id
        runner = self._runners[job.id]
        self._running_tasks[job.id] = asyncio.create_task(
            self._execute(job, runner), name=f"job-{job.id}"
        )
        self._emit("jobs_started", priority=job.spec.priority)
        logger.info("job %s running (user=%s)", job.id, job.spec.user_id)

    async def _execute(self, job: Job, runner: Runner) -> None:
        """Runs one job and always finalizes it, converting failures to results."""
        output = ""
        exit_ok = False
        error: str | None = None
        cancelled = False
        try:
            async with _deadline(job.spec.timeout):
                raw = await runner(job.spec)
            output = _coerce_output(raw)
            exit_ok = True
        except TimeoutError:
            error = f"Job timed out after {job.spec.timeout:g}s"
            logger.warning("job %s timed out", job.id)
        except asyncio.CancelledError:
            cancelled = True
            error = "Job cancelled"
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            logger.warning("job %s failed: %s", job.id, error)
        finally:
            if cancelled:
                state = JobState.CANCELLED
            elif exit_ok:
                state = JobState.DONE
            else:
                state = JobState.FAILED
            self._finalize(job, state, exit_ok, error, output)
        if cancelled:
            raise asyncio.CancelledError

    def _finalize(
        self,
        job: Job,
        state: JobState,
        exit_ok: bool,
        error: str | None,
        output: str,
    ) -> None:
        """Settles a job exactly once: bookkeeping, result future, metric, wake.

        The once-only guard keeps accounting honest when ``stop()`` sweeps
        stragglers that already finalized themselves.
        """
        if job.id in self._finalized:
            return
        self._finalized.add(job.id)
        started_mono = self._started_mono.pop(job.id, None)
        duration = 0.0 if started_mono is None else max(0.0, time.monotonic() - started_mono)
        job.state = state
        job.finished_at = time.time()
        job.exit_ok = exit_ok
        job.error = error
        job.output_bytes = len(output.encode("utf-8")) if output else 0

        self._pending.pop(job.id, None)
        self._running.pop(job.id, None)
        self._running_tasks.pop(job.id, None)
        self._runners.pop(job.id, None)
        self._seq.pop(job.id, None)
        if self._running_by_user.get(job.spec.user_id) == job.id:
            self._running_by_user.pop(job.spec.user_id, None)

        remaining = self._outstanding_by_user.get(job.spec.user_id, 0) - 1
        if remaining > 0:
            self._outstanding_by_user[job.spec.user_id] = remaining
        else:
            self._outstanding_by_user.pop(job.spec.user_id, None)

        if job.spec.dedupe_key is not None:
            key = (job.spec.user_id, job.spec.dedupe_key)
            if self._dedupe.get(key) == job.id:
                self._dedupe.pop(key, None)

        self._counts[state.value] = self._counts.get(state.value, 0) + 1
        future = self._futures.pop(job.id, None)
        if future is not None and not future.done():
            future.set_result(JobResult(job=job, output=output, exit_ok=exit_ok, duration=duration))
        self._emit("jobs_finished", state=state.value, exit_ok=exit_ok)
        logger.info("job %s %s (%.3fs)", job.id, state.value, duration)
        self._wake.set()

    # -- metrics ---------------------------------------------------------
    def _emit(self, name: str, **labels: object) -> None:
        """Emits a metric to the injected hook; a broken hook never breaks jobs."""
        if self._on_metric is None:
            return
        try:
            self._on_metric(name, labels)
        except Exception:  # pragma: no cover - defensive
            logger.debug("metric hook failed for %s", name, exc_info=True)
