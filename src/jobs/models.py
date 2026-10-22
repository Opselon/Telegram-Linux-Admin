# PURPOSE: Immutable data model of the job queue (spec + live record + result).
# OWNER: lane `jobs` of wave 3.2 (branch feat/3.2-jobs)
# CONSUMES: nothing (pure dataclasses + enums).
# PROVIDES: JobSpec, JobState, Job, JobResult.
# INVARIANTS: JobSpec/JobResult are frozen; Job is mutable but only the queue
#             mutates it, and every transition moves forward in time
#             (created_at <= started_at <= finished_at when set).
# EXTEND: new fields belong on JobSpec (frozen, additive with a default).

"""Data model for the job queue: what was asked for, where it is, what it did."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping


class JobState(str, Enum):
    """Lifecycle of one job, in strict order (CANCELLED/DONE/FAILED terminal)."""

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class JobSpec:
    """What a user asked the queue to run.

    ``priority`` is ascending (lower runs first); ``dedupe_key`` coalesces
    identical queued/running work for the same user instead of re-running it.
    """

    user_id: int
    alias: str
    command: str
    priority: int = 10
    timeout: float = 60.0
    dedupe_key: str | None = None
    label: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class Job:
    """Live record of one submission; the queue is the only writer."""

    id: str
    spec: JobSpec
    state: JobState
    created_at: float
    started_at: float | None
    finished_at: float | None
    exit_ok: bool | None
    output_bytes: int
    error: str | None

    def is_terminal(self) -> bool:
        """True once the job can never run or change again."""
        return self.state in (JobState.DONE, JobState.FAILED, JobState.CANCELLED)


@dataclass(frozen=True, slots=True)
class JobResult:
    """What ``submit`` resolves to: the finished job plus its flattened output.

    ``exit_ok`` mirrors ``job.exit_ok`` so callers need no state knowledge.
    """

    job: Job
    output: str
    exit_ok: bool
    duration: float
