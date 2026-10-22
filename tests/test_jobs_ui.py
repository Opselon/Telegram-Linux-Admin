# PURPOSE: Cover the 3.3 jobs operator surface end-to-end: the Telegram panel
#          (jobs_ui), timeout enforcement in the queue, per-user stats, and
#          the on_metric -> METRICS bridge metering a real queue.
# OWNER: lane `jobs-hardening` of wave 3.3 (branch feat/3.3-jobs-hardening)
# CONSUMES: src.bot.jobs_ui, src.bot.state, src.jobs, src.metrics.
#           unittest.mock (fakes of the PTB transport only). No network I/O.
# PROVIDES: tests/test_jobs_ui.py.
# INVARIANTS: real JobQueue instances drive every scenario; handlers get a
#             real Update whose callback_query carries the exact data string
#             application.py routes; METRICS counters are restored after the
#             bridge test so the shared registry stays untouched for others.
# EXTEND: new panel behavior goes here next to the scenario that pins it.

"""Tests for the Telegram "My Jobs" panel and the queue-hardening contract."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from src.bot import state
from src.bot.jobs_ui import (
    CANCEL_PREFIX,
    REFRESH_CALLBACK,
    _build_jobs_panel,
    cancel_job_callback,
    jobs_refresh_callback,
    jobs_status_menu,
)
from src.jobs import JobQueue, JobSpec
from src.jobs.models import JobResult, JobState
from src.metrics import METRICS

pytestmark = pytest.mark.asyncio

USER_A = 4242
USER_B = 4343


class _InstantRunner:
    """Runner that resolves immediately (no gating, no sleep)."""

    async def __call__(self, job_spec: JobSpec) -> str:
        await asyncio.sleep(0)
        return "ok"


class _FakeUpdate:
    """A stand-in Update whose callback_query is a fake carrying routed data."""

    def __init__(self, data: str, user_id: int = USER_A) -> None:
        self.callback_query = MagicMock()
        self.callback_query.data = data
        self.callback_query.answer = _async_recorder("answer")
        self.callback_query.edit_message_text = _async_recorder("edit_message_text")
        self.effective_user = MagicMock()
        self.effective_user.id = user_id
        self.effective_user.from_user = self.effective_user
        self.effective_message = None
        self.effective_chat = None
        self.message = None


def _async_recorder(name: str):
    """Async mock recording calls without touching any PTB type."""
    calls: list[tuple] = []

    async def call(*args, **kwargs):
        calls.append((args, kwargs))
        return None

    call.calls = calls  # type: ignore[attr-defined]
    call.__name__ = name
    return call


@pytest.fixture(autouse=True)
def _reset_shared_state():
    """Guards the shared registry and the state singleton around each test."""
    before_counters = dict(METRICS._counters)
    before_gauges = dict(METRICS._gauges)
    state.job_queue = None
    state.user_language_cache.pop(USER_A, None)
    state.user_language_cache.pop(USER_B, None)
    yield
    METRICS._counters.clear()
    METRICS._counters.update(before_counters)
    METRICS._gauges.clear()
    METRICS._gauges.update(before_gauges)
    state.job_queue = None


def _unescaped_for_edit(update: _FakeUpdate, text: str) -> str:
    """Reverses the MarkdownV2 escaping ``_edit_message_safely`` applies."""
    special = "\\_*[]()~`>#+-=|{}.!"
    out = []
    i = 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text) and text[i + 1] in special:
            out.append(text[i + 1])
            i += 2
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _edit_text(update: _FakeUpdate) -> str:
    """The text the last panel edit left on the message (user-render form)."""
    assert update.callback_query.edit_message_text.calls, "the panel must have been edited"
    args, _kwargs = update.callback_query.edit_message_text.calls[-1]
    return _unescaped_for_edit(update, args[0])


def _edit_markup(update: _FakeUpdate):
    return update.callback_query.edit_message_text.calls[-1][1]["reply_markup"]


def _cancel_rows(markup) -> list[str]:
    """Callback data of every single-button cancel row in a keyboard."""
    return [
        row[0].callback_data
        for row in markup.inline_keyboard
        if len(row) == 1 and row[0].callback_data.startswith(CANCEL_PREFIX)
    ]


def _gated_runner(gate: asyncio.Event):
    async def runner(job_spec: JobSpec) -> str:
        await gate.wait()
        return "ok"

    return runner


# -- panel rendering: 2 running + 3 pending ----------------------------------


async def test_menu_renders_running_and_pending_lines() -> None:
    queue = JobQueue(max_concurrent=4)
    gate = asyncio.Event()
    runner = _gated_runner(gate)
    await queue.start()
    # Two users hold the two running slots (the queue serializes per user,
    # so one user can never show two RUNNING lines); user A also queues 3.
    tasks = [
        asyncio.create_task(
            queue.submit(JobSpec(user_id=USER_A, alias="self", command="tail -f log"), runner)
        ),
        asyncio.create_task(
            queue.submit(JobSpec(user_id=USER_B, alias="self", command="disk check"), runner)
        ),
    ]
    tasks.extend(
        asyncio.create_task(
            queue.submit(JobSpec(user_id=USER_A, alias="self", command=f"ping server{i}"), runner)
        )
        for i in range(3)
    )
    for _ in range(10):
        await asyncio.sleep(0)
    assert len(queue.active()) == 2 and len(queue.list_pending(USER_A)) == 3
    state.job_queue = queue

    update = _FakeUpdate("jobs_status_menu")
    await jobs_status_menu(update, None)  # type: ignore[arg-type]

    text = _edit_text(update)
    assert "`tail -f log`" in text
    assert all(f"`ping server{i}`" in text for i in range(3))
    assert "`disk check`" not in text, "other users' running jobs must not leak in"
    assert "⏳ Running (1):" in text
    assert "🕒 Waiting (3):" in text
    assert "other" not in text, "the panel must only show the pressing user's own jobs"
    markup = _edit_markup(update)
    assert len(_cancel_rows(markup)) == 3, "one cancel button per pending job"
    assert any(
        len(row) == 1 and row[0].callback_data == REFRESH_CALLBACK for row in markup.inline_keyboard
    )
    assert update.callback_query.answer.calls, "the callback must be answered first"

    gate.set()
    await asyncio.gather(*tasks)
    await queue.stop()


async def test_empty_queue_shows_the_empty_line() -> None:
    queue = JobQueue()
    await queue.start()
    state.job_queue = queue
    update = _FakeUpdate("jobs_status_menu")
    await jobs_status_menu(update, None)  # type: ignore[arg-type]
    assert "No running or waiting jobs." in _edit_text(update)
    assert _cancel_rows(_edit_markup(update)) == []
    await queue.stop()


# -- cancel actually dequeues (QUEUED -> terminal) ----------------------------


async def test_cancel_button_dequeues_queued_job() -> None:
    queue = JobQueue(max_concurrent=1)
    blocker = asyncio.Event()
    runner = _gated_runner(blocker)
    await queue.start()
    running = asyncio.create_task(
        queue.submit(JobSpec(user_id=USER_A, alias="self", command="busy"), runner)
    )
    for _ in range(10):
        await asyncio.sleep(0)
    state.job_queue = queue
    queued = asyncio.create_task(
        queue.submit(JobSpec(user_id=USER_A, alias="self", command="victim-cmd"), runner)
    )
    for _ in range(10):
        await asyncio.sleep(0)
    pending = queue.list_pending(USER_A)
    assert len(pending) == 1 and pending[0].state is JobState.QUEUED
    victim_id = pending[0].id

    update = _FakeUpdate(f"{CANCEL_PREFIX}{victim_id}")
    await cancel_job_callback(update, None)  # type: ignore[arg-type]

    assert update.callback_query.answer.calls, "the callback must be answered first"
    text = _edit_text(update)
    assert "cancelled" in text
    assert _cancel_rows(_edit_markup(update)) == [], "cancelled job's button must disappear"

    result = await queued
    assert result.job.state is JobState.CANCELLED
    assert result.exit_ok is False
    assert result.job.is_terminal() is True
    assert queue.list_pending(USER_A) == [], "cancel must dequeue the job"

    blocker.set()
    await running
    await queue.stop()


async def test_cancel_unknown_job_says_not_found_and_keeps_panel() -> None:
    queue = JobQueue()
    await queue.start()
    state.job_queue = queue
    update = _FakeUpdate(f"{CANCEL_PREFIX}job_unknown")
    await cancel_job_callback(update, None)  # type: ignore[arg-type]
    assert "no longer pending" in _edit_text(update)
    await queue.stop()


# -- refresh idempotence -------------------------------------------------------


async def test_refresh_re_renders_identical_panel() -> None:
    queue = JobQueue(max_concurrent=1)
    gate = asyncio.Event()
    runner = _gated_runner(gate)
    await queue.start()
    task_a = asyncio.create_task(
        queue.submit(JobSpec(user_id=USER_A, alias="self", command="hold"), runner)
    )
    task_b = asyncio.create_task(
        queue.submit(JobSpec(user_id=USER_A, alias="self", command="queued-1"), runner)
    )
    for _ in range(10):
        await asyncio.sleep(0)
    state.job_queue = queue

    text_before, markup_before = _build_jobs_panel(USER_A)
    update = _FakeUpdate(REFRESH_CALLBACK)
    await jobs_refresh_callback(update, None)  # type: ignore[arg-type]

    assert update.callback_query.edit_message_text.calls, "refresh must re-render the panel"
    args, kwargs = update.callback_query.edit_message_text.calls[-1]
    assert _unescaped_for_edit(update, args[0]) == text_before, (
        "re-render must be idempotent for unchanged state"
    )
    assert _cancel_rows(kwargs["reply_markup"]) == _cancel_rows(markup_before), (
        "cancel callbacks must not duplicate on refresh"
    )

    gate.set()
    await asyncio.gather(task_a, task_b)
    await queue.stop()


# -- degraded mode -------------------------------------------------------------


async def test_degraded_mode_answers_single_explanatory_message(monkeypatch) -> None:
    monkeypatch.setattr(state, "job_queue", None)
    update = _FakeUpdate("jobs_status_menu")
    await jobs_status_menu(update, None)  # type: ignore[arg-type]

    assert update.callback_query.answer.calls, "the callback must still be answered"
    assert len(update.callback_query.edit_message_text.calls) == 1, (
        "degraded mode shows one explanatory message, exactly once"
    )
    args, _kwargs = update.callback_query.edit_message_text.calls[-1]
    assert "no longer rendered" not in args[0]
    assert "queue" in _unescaped_for_edit(update, args[0]).lower(), (
        "the degraded message must explain the queue, not render a panel"
    )


async def test_degraded_mode_cannot_cancel() -> None:
    state.job_queue = None
    update = _FakeUpdate(f"{CANCEL_PREFIX}job_x")
    await cancel_job_callback(update, None)  # type: ignore[arg-type]
    assert update.callback_query.answer.calls, "must answer with the degraded explanation"


# -- per-user stats breakdown ---------------------------------------------------


async def test_stats_per_user_running_and_pending() -> None:
    queue = JobQueue(max_concurrent=2)
    gate = asyncio.Event()
    runner = _gated_runner(gate)
    await queue.start()
    tasks = [
        asyncio.create_task(
            queue.submit(JobSpec(user_id=USER_A, alias="self", command="a"), runner)
        ),
        asyncio.create_task(
            queue.submit(JobSpec(user_id=USER_B, alias="self", command="b"), runner)
        ),
        asyncio.create_task(
            queue.submit(JobSpec(user_id=USER_A, alias="self", command="p1"), runner)
        ),
        asyncio.create_task(
            queue.submit(JobSpec(user_id=USER_A, alias="self", command="p2"), runner)
        ),
    ]
    for _ in range(10):
        await asyncio.sleep(0)

    breakdown = queue.stats_per_user()
    assert breakdown[USER_A] == {"running": 1, "pending": 2}
    assert breakdown[USER_B] == {"running": 1, "pending": 0}

    gate.set()
    await asyncio.gather(*tasks)
    assert queue.stats_per_user() == {}, "breakdown empties when nothing is outstanding"
    await queue.stop()


# -- timeout enforcement end-to-end ----------------------------------------------


async def test_timeout_yields_failed_result_not_traceback() -> None:
    queue = JobQueue()
    await queue.start()

    async def slow(job_spec: JobSpec) -> str:
        await asyncio.sleep(30)
        return "never"

    result = await asyncio.wait_for(
        queue.submit(JobSpec(user_id=USER_A, alias="self", command="slow", timeout=0.05), slow),
        timeout=5.0,
    )
    assert isinstance(result, JobResult)
    assert result.exit_ok is False
    assert result.job.state is JobState.FAILED
    assert "timed out" in (result.job.error or "").lower()
    assert queue.stats()["running"] == 0
    await queue.stop()


# -- METRICS bridge --------------------------------------------------------------


async def test_on_metric_lands_in_src_metrics() -> None:
    def bridge(name: str, labels: object) -> None:
        fields = dict(labels or {})
        fields.pop("value", None)
        METRICS.inc(f"jobs_{name}_total", **{str(k): v for k, v in fields.items()})

    METRICS.reset()
    base = METRICS.get("jobs_jobs_submitted_total")
    queue = JobQueue(max_concurrent=1, on_metric=bridge)
    await queue.start()
    result = await queue.submit(
        JobSpec(user_id=USER_A, alias="self", command="echo hi"), _InstantRunner()
    )
    assert result.job.state is JobState.DONE
    await queue.stop()

    assert METRICS.get("jobs_jobs_submitted_total", priority=10) >= base + 1
    assert METRICS.get("jobs_jobs_started_total", priority=10) >= 1
    assert METRICS.get("jobs_jobs_finished_total", state=JobState.DONE.value, exit_ok=True) >= 1, (
        "the on_metric bridge must land finished counters in src.metrics.METRICS"
    )


async def test_bridge_carries_label_dimensions() -> None:
    seen: list[tuple[str, dict[str, object]]] = []

    def collector(name: str, labels: object) -> None:
        seen.append((name, dict(labels or {})))

    queue = JobQueue(on_metric=collector)
    await queue.start()
    await queue.submit(JobSpec(user_id=1, alias="self", command="x", priority=3), _InstantRunner())
    names = {name for name, _ in seen}
    assert {"jobs_submitted", "jobs_started", "jobs_finished"} <= names
    finish = next(labels for name, labels in seen if name == "jobs_finished")
    assert finish["state"] == JobState.DONE.value
    assert finish["exit_ok"] is True
    await queue.stop()
