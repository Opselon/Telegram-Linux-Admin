# PURPOSE: Public interface of the job-queue package (models/errors/queue).
# OWNER: lane `jobs` of wave 3.2 (branch feat/3.2-jobs)
# CONSUMES: nothing from src (the queue is dependency-free, asyncio only).
# PROVIDES: JobSpec, Job, JobState, JobResult, QueueFull, JobQueue.
# INVARIANTS: importing this package must not create side effects;
#             re-exports below are the single source the orchestrator wires.
# EXTEND: add adapters here only; keep the runner logic out of this file.

"""Job queue package: per-user serialized async command execution at scale.

The queue is the heart of the control plane:/admins do not wait on one
3779-line script, several users run commands concurrently while staying
serialized per user. Callers await a single future per submission.
"""

from __future__ import annotations

from src.jobs.errors import QueueFull
from src.jobs.models import Job, JobResult, JobState, JobSpec
from src.jobs.queue import JobQueue

__all__ = ["Job", "JobQueue", "JobResult", "JobState", "JobSpec", "QueueFull"]
