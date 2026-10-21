# PURPOSE: Errors raised by the job queue (kept separate so callers can
#          import the exception surface without pulling in the queue).
# OWNER: lane `jobs` of wave 3.2 (branch feat/3.2-jobs)
# CONSUMES: nothing.
# PROVIDES: QueueFull.
# INVARIANTS: errors are plain Exception subclasses; no module-level state.
# EXTEND: new queue errors go here and are re-exported from src/jobs.

"""Exception surface of the job queue."""

from __future__ import annotations


class QueueFull(Exception):
    """Raised when submitting a job would exceed a configured pending cap.

    WHY it exists: the bot must refuse work it has scheduled no room for,
    instead of silently growing an unbounded backlog while the user waits.
    """

    def __init__(self, message: str, *, user_id: int | None = None, limit: int | None = None) -> None:
        super().__init__(message)
        self.user_id = user_id
        self.limit = limit
