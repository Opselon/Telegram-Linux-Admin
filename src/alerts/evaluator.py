"""PURPOSE: The single pure predicate that decides whether a rule fires.

OWNER: lane `alerts` of wave 3.4 (branch feat/3.4-alerts).
CONSUMES: src.alerts.models.AlertRule only — no I/O, no clock, no metrics.
PROVIDES: ``evaluate(rule, reading) -> bool``.
INVARIANTS:
  * Pure and total: same inputs always yield the same answer, nothing is
    mutated, nothing is awaited, nothing is logged.
  * Boundary is INCLUSIVE: a reading exactly at the threshold fires.  An
    operator who asks for "alert me at 80 %" expects 80.0 % to alert.
  * ``None`` never fires.  ``None`` is the checker's "no reading" signal
    (target unreachable / probe declined), not a zero, so treating it as a
    number would either alert at every threshold or silently mask an outage.
    ``nan`` behaves the same way because every comparison against it is
    false — the guard below makes that explicit rather than incidental.
  * Hysteresis is deliberately NOT applied here: the contract puts it in
    the caller, so ``evaluate`` stays a single comparison a test can pin.
EXTEND: hysteresis (a clear margin) belongs in the sweep that owns the
  per-rule previous state, not in this predicate.
"""

from __future__ import annotations

from .models import AlertRule

__all__ = ["evaluate"]


def evaluate(rule: AlertRule, reading: float | None) -> bool:
    """True when ``reading`` meets or exceeds ``rule.threshold``.

    A ``None`` (or NaN) reading is "no measurement" and never fires; see the
    module INVARIANTS for why that is not the same as zero.
    """
    if reading is None:
        return False
    # NaN is the only float that is not equal to itself; it is a broken
    # reading, not a hot one, so refuse it before the comparison.
    if reading != reading:  # noqa: PLR0124 - explicit NaN guard, not a typo
        return False
    return float(reading) >= float(rule.threshold)
