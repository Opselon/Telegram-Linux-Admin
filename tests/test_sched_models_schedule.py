# PURPOSE: Pin the pure half of wave 3.4's backup lane: BackupPlan/BackupRun
#          validation and the datetime schedule math (no I/O, no clock).
# OWNER: lane `backup` of wave 3.4 (branch feat/3.4-backup).
# CONSUMES: src.sched.models, src.sched.schedule. stdlib datetime only.
# PROVIDES: tests/test_sched_models_schedule.py.
# INVARIANTS: every case passes an explicit `now`, so nothing here depends on
#             the wall clock or on a tz database (DST cases use naive local
#             datetimes exactly as the runner does).
# EXTEND: new plan fields / new schedule predicates belong here.

"""Validation and schedule-math tests for src.sched (pure functions only)."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta

import pytest

from src.sched.models import BackupPlan, BackupRun
from src.sched.schedule import next_run, should_run, slot_for

NOW = datetime(2026, 10, 22, 9, 30, 0)


# -- models -------------------------------------------------------------------


def test_plan_accepts_the_documented_bounds() -> None:
    assert BackupPlan(alias="web", hour=0, keep=1).hour == 0
    assert BackupPlan(alias="web", hour=23, keep=1000).keep == 1000


@pytest.mark.parametrize("hour", [-1, 24, 99])
def test_plan_rejects_out_of_range_hour(hour: int) -> None:
    with pytest.raises(ValueError):
        BackupPlan(alias="web", hour=hour)


def test_plan_rejects_zero_keep() -> None:
    with pytest.raises(ValueError, match="keep"):
        BackupPlan(alias="web", hour=3, keep=0)


def test_plan_rejects_non_int_hour_including_bool() -> None:
    with pytest.raises(ValueError):
        BackupPlan(alias="web", hour=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        BackupPlan(alias="web", hour="7")  # type: ignore[arg-type]


@pytest.mark.parametrize("alias", ["", "   ", "../etc", "a/b", "a\\b", ".", "..", " pad "])
def test_plan_rejects_aliases_that_are_not_directory_safe(alias: str) -> None:
    with pytest.raises(ValueError):
        BackupPlan(alias=alias, hour=3)


def test_plan_is_frozen() -> None:
    plan = BackupPlan(alias="web", hour=3, keep=7, owner_id=1, id=5)
    with pytest.raises(FrozenInstanceError):
        plan.hour = 4  # type: ignore[misc]


def test_run_status_is_validated_and_ok_property_tracks_it() -> None:
    run = BackupRun(plan_id=1, alias="web", started_at=NOW)
    assert run.status == "ok" and run.ok is True
    assert BackupRun(plan_id=1, alias="web", started_at=NOW, status="retrieval_failed").ok is False
    with pytest.raises(ValueError, match="status"):
        BackupRun(plan_id=1, alias="web", started_at=NOW, status="exploded")


# -- next_run -----------------------------------------------------------------


def test_next_run_today_when_hour_is_still_ahead() -> None:
    assert next_run(18, NOW) == datetime(2026, 10, 22, 18, 0, 0)


def test_next_run_tomorrow_when_hour_has_passed() -> None:
    assert next_run(3, NOW) == datetime(2026, 10, 23, 3, 0, 0)


def test_next_run_is_strictly_future_when_now_is_exactly_the_hour() -> None:
    """`now` exactly on the slot must roll to tomorrow (no zero-length loop)."""
    on_the_hour = datetime(2026, 10, 22, 6, 0, 0)
    assert next_run(6, on_the_hour) == datetime(2026, 10, 23, 6, 0, 0)


def test_next_run_clears_sub_second_noise() -> None:
    noisy = datetime(2026, 10, 22, 9, 30, 0, 987654)
    assert next_run(18, noisy).microsecond == 0
    assert next_run(18, noisy).second == 0


def test_next_run_crosses_a_month_boundary() -> None:
    assert next_run(1, datetime(2026, 10, 31, 23, 59)) == datetime(2026, 11, 1, 1, 0)


def test_next_run_is_dst_safe_across_a_spring_forward_day() -> None:
    """Naive-local math: the wall-clock hour is honoured on the DST-change day."""
    result = next_run(2, datetime(2027, 3, 13, 23, 30))
    assert result == datetime(2027, 3, 14, 2, 0, 0)
    # and it is genuinely in the future of the input instant
    assert result > datetime(2027, 3, 13, 23, 30)


def test_next_run_rejects_bad_hour() -> None:
    with pytest.raises(ValueError):
        next_run(-1, NOW)
    with pytest.raises(ValueError):
        next_run(24, NOW)


# -- should_run ---------------------------------------------------------------


def test_should_run_before_the_hour_is_false() -> None:
    plan = BackupPlan(alias="web", hour=18, keep=7)
    assert should_run(plan, NOW, None) is False


def test_should_run_never_run_at_the_hour_is_true() -> None:
    plan = BackupPlan(alias="web", hour=9, keep=7)
    assert should_run(plan, NOW, None) is True


def test_should_run_disabled_plan_is_false() -> None:
    plan = BackupPlan(alias="web", hour=9, keep=7, enabled=False)
    assert should_run(plan, NOW, None) is False


def test_should_run_is_false_after_a_run_later_today() -> None:
    plan = BackupPlan(alias="web", hour=9, keep=7)
    assert should_run(plan, NOW, datetime(2026, 10, 22, 9, 15)) is False


def test_should_run_is_false_after_a_run_exactly_on_the_slot() -> None:
    plan = BackupPlan(alias="web", hour=9, keep=7)
    assert should_run(plan, NOW, datetime(2026, 10, 22, 9, 0)) is False


def test_should_run_is_true_again_the_next_day() -> None:
    plan = BackupPlan(alias="web", hour=9, keep=7)
    tomorrow = datetime(2026, 10, 23, 9, 5)
    assert should_run(plan, tomorrow, datetime(2026, 10, 22, 9, 15)) is True


def test_should_run_fires_once_per_day_on_a_minute_ticker() -> None:
    """Deterministic ticker: no sleeps, just a walk over today's minutes."""
    plan = BackupPlan(alias="web", hour=9, keep=7)
    last_run = None
    fired = 0
    for minute in range(0, 5 * 60):  # 09:00 .. 13:59, one tick per minute
        now = datetime(2026, 10, 22, 9, 0) + timedelta(minutes=minute)
        if should_run(plan, now, last_run):
            fired += 1
            last_run = now
    assert fired == 1, "a daily plan must fire exactly once per calendar day"


def test_slot_for_is_the_same_day_at_the_plan_hour() -> None:
    plan = BackupPlan(alias="web", hour=18, keep=7)
    assert slot_for(plan, NOW) == datetime(2026, 10, 22, 18, 0)
