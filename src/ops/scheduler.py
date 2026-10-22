"""Recurring-task scheduler on asyncio (wave 3.3 ops lane).

PURPOSE
    Tiny dependency-free async scheduler: register named periodic jobs,
    run them on an asyncio loop, survive individual job failures and expose
    per-task operational state for dashboards and health surfaces.

OWNER
    Lane ops (feat/3.3-ops). Orchestrator owns every wiring outside
    src/ops/** — this module is registered by the caller, never by main().

CONSUMES
    stdlib async/await only.  No src.* imports: the scheduler is reusable
    and the maintainer/healthcheck layers adapt it to the bot.

PROVIDES
    :class:`TaskInfo` — per-task runtime record.
    :class:`Scheduler` — ``add()``, ``start()``, ``stop()``, ``status()``
    and a manual ``now()`` tick so tests can drive the engine without a
    wall clock.  A job that raises is caught, its traceback text recorded
    on its TaskInfo, and the loop keeps running: one broken job must never
    stop the other tasks (CONTRACT.md hard law 4).

INVARIANTS
    - interval <= 0 raises ValueError at registration time;
    - a raising job never propagates out of the scheduler loop;
    - ``status()`` is a plain dict suitable for JSON rendering;
    - after :meth:`stop` no task is left scheduled (no-leak guarantee).
"""

from __future__ import annotations

import asyncio
import logging
import time
import traceback
from collections.abc import Coroutine
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

JobFn = Callable[[], Awaitable[None]] | Callable[[], object]


class TaskInfo:
    """Runtime record for one scheduled task."""

    __slots__ = ("name", "interval", "last_run", "next_run", "last_error", "run_count")

    def __init__(self, name: str, interval: float) -> None:
        self.name = name
        self.interval = interval
        self.last_run: float | None = None
        self.next_run: float | None = None
        self.last_error: str | None = None
        self.run_count = 0

    def as_dict(self) -> dict[str, object]:
        """Plain-dict projection used by status()/health surfaces."""
        return {
            "name": self.name,
            "interval": self.interval,
            "last_run": self.last_run,
            "next_run": self.next_run,
            "last_error": self.last_error,
            "run_count": self.run_count,
        }


class Scheduler:
    """Recurring-task scheduler with a manual tick for tests.

    Two operating modes share one code path:

    * battery mode (:meth:`start`) — one internal loop computes due jobs
      from a wall/monotonic clock.
    * manual mode (:meth:`now`) — the driver decides when time advances
      and passes that instant in; the engine below still owns ordering
      and error capture.  Tests use this so scheduling is deterministic.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, TaskInfo] = {}
        self._fns: dict[str, JobFn] = {}
        self._loop_task: asyncio.Task[None] | None = None
        self._stopping = False

    # -- registration ----------------------------------------------------
    def add(self, name: str, interval: float, fn: JobFn) -> None:
        """Registers a recurring task. interval must be strictly positive."""
        if not name:
            raise ValueError("scheduler task needs a name")
        if not isinstance(interval, (int, float)) or interval <= 0:
            raise ValueError("interval must be a positive number of seconds")
        if name in self._fns:
            raise ValueError(f"scheduler task {name!r} is already registered")
        self._fns[name] = fn
        self._tasks[name] = TaskInfo(name, float(interval))

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        """Starts the background loop (idempotent)."""
        if self._loop_task is not None and not self._loop_task.done():
            return
        self._stopping = False
        self._loop_task = asyncio.get_running_loop().create_task(self._run_forever())

    async def stop(self) -> None:
        """Stops the loop and awaits it. Safe when never started."""
        self._stopping = True
        task = self._loop_task
        self._loop_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:  # noqa: B904 - consumed by design
                pass

    # -- ticking ---------------------------------------------------------
    def now(self, at: float | None = None) -> None:
        """Runs every due task once, synchronously in the caller's context.

        ``at`` defaults to the current monotonic clock.  Synchronous fns
        are invoked inline; when a coroutine function was registered and
        there is no running loop (the manual-tick/test path), one is
        created on the spot with :func:`asyncio.run`.  When called from
        inside a live loop an async job cannot be driven inline, so it is
        left to the battery loop (or to whichever caller started one).
        Tasks that raise are recorded and swallowed — the same engine the
        battery path exercises.
        """
        self.tick(at if at is not None else time.monotonic())

    def tick(self, at: float) -> None:
        """Engine tick: due-task selection plus per-task error capture.

        A failing job is recorded on its own TaskInfo and the remaining
        due tasks still run — the loop itself never propagates a job's
        exception (CONTRACT.md lane ops #1 and #4).
        """
        for name, info in list(self._tasks.items()):
            if info.next_run is not None and at < info.next_run:
                continue
            fn = self._fns.get(name)
            if fn is None:
                continue
            info.last_run = at
            info.run_count += 1
            if info.next_run is None:
                info.next_run = at + info.interval
            else:
                info.next_run += info.interval
                if info.next_run < at:
                    # Shed any backlog a stopped/advanced clock produced.
                    info.next_run = at + info.interval
            self._invoke(name, fn, info)

    def _invoke(self, name: str, fn: JobFn, info: TaskInfo) -> None:
        """Runs one job body, capturing its failure on the TaskInfo."""
        try:
            if asyncio.iscoroutinefunction(fn):
                # A coroutine function registered by the caller: whether
                # it can be driven inline depends on the ambient loop.
                try:
                    running = asyncio.get_running_loop()
                except RuntimeError:
                    running = None
                if running is None:
                    # Manual-tick path outside a loop: spin a fresh one.
                    coro = fn()
                    asyncio.run(coro)
                    return
                # Inside a live loop, delegate: the coroutine is scheduled
                # there and _drive owns its per-task error capture.
                coro = fn()
                running.create_task(self._drive(name, coro, info))
                return
            fn()
        except asyncio.CancelledError:  # pragma: no cover - defensive
            raise
        except Exception:
            info.last_error = traceback.format_exc()
            logger.error("scheduled task %s failed", name, exc_info=True)

    async def _drive(
        self, name: str, coro: Coroutine[Any, Any, None], info: TaskInfo
    ) -> None:
        """Awaits a loop-scheduled job coroutine and captures its failure."""
        try:
            await coro
        except asyncio.CancelledError:  # pragma: no cover - defensive
            raise
        except Exception:
            info.last_error = traceback.format_exc()
            logger.error("scheduled task %s failed", name, exc_info=True)

    async def _run_forever(self) -> None:
        """Battery loop built on the same tick engine over the real clock."""
        base = time.monotonic()
        for info in self._tasks.values():
            info.next_run = base + info.interval
        while not self._stopping:
            await asyncio.sleep(0.05)
            if self._stopping:
                break
            try:
                self.tick(time.monotonic())
            except asyncio.CancelledError:  # pragma: no cover - defensive
                raise
            except Exception:  # pragma: no cover - the tick cannot raise
                logger.exception("scheduler tick failed unexpectedly")

    # -- introspection ---------------------------------------------------
    def status(self) -> dict[str, dict[str, object]]:
        """Returns {name: per-task record} for dashboards and tests."""
        return {name: info.as_dict() for name, info in self._tasks.items()}
