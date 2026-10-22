"""Purpose: pure datetime math for daily backup plans (no I/O, no clock access).

OWNER: lane `backup` of wave 3.4 (branch feat/3.4-backup), package src/sched/.
CONSUMES: stdlib ``datetime`` and :class:`src.sched.models.BackupPlan`.
PROVIDES: :func:`next_run`, :func:`slot_for`, :func:`should_run`.
INVARIANTS:
  * no clock is read here and nothing is awaited: callers pass ``now`` (and
    the plan's previous run), which is what makes the schedule deterministic
    under test and immune to the wall clock (CONTRACT hard law L3);
  * only *naive local* datetimes are compared, so the module never has to
    reason about tz database availability;
  * DST-safe by construction: the next occurrence is computed by walking
    calendar days (``replace`` + ``timedelta(days=1)``), never by adding
    absolute hour offsets. A DST jump therefore shifts the wall-clock hour a
    plan fires at — the local hour is the operator's choice — but can never
    yield a wrong day or an instant already in the past;
  * :func:`next_run` returns a strictly future instant for every valid hour,
    so a caller looping on it cannot spin;
  * a plan fires at most once per calendar day: a ``last_run`` at or after
    today's slot satisfies today.
EXTEND: new predicates go here and stay pure; the runner does the I/O.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .models import BackupPlan

__all__ = ["next_run", "slot_for", "should_run"]


def _at_hour(now: datetime, hour: int) -> datetime:
    """``now``'s calendar day at ``hour`` o'clock, naive local time."""
    return now.replace(hour=hour, minute=0, second=0, microsecond=0)


def next_run(hour: int, now: datetime) -> datetime:
    """Returns the next occurrence of local ``hour`` strictly after ``now``.

    * ``now`` before ``hour`` today -> today at ``hour``;
    * ``now`` at or past ``hour`` today (exactly on it included) -> tomorrow
      at ``hour``.
    """
    if isinstance(hour, bool) or not isinstance(hour, int) or not 0 <= hour <= 23:
        raise ValueError("hour must be an int in 0..23")
    candidate = _at_hour(now, hour)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def slot_for(plan: BackupPlan, now: datetime) -> datetime:
    """The instant ``plan`` is scheduled at on ``now``'s calendar day."""
    return _at_hour(now, plan.hour)


def should_run(plan: BackupPlan, now: datetime, last_run: datetime | None) -> bool:
    """True when ``plan`` is due: enabled, its hour has arrived, not yet run today.

    ``last_run`` is the plan's previous run instant (``None`` = never run).
    Any run at or after today's slot satisfies today, so a minute-by-minute
    caller fires the plan at most once per calendar day.
    """
    if not plan.enabled:
        return False
    slot = slot_for(plan, now)
    if now < slot:
        return False
    return not (last_run is not None and last_run >= slot)
