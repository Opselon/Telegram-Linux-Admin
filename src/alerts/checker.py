"""PURPOSE: The sweep that turns rules + readings into fired alert events.

OWNER: lane `alerts` of wave 3.4 (branch feat/3.4-alerts).
CONSUMES: src.alerts.models (AlertRule/AlertEvent), src.alerts.evaluator
  (the only firing predicate), src.alerts.notifier.render_event_text,
  src.metrics.METRICS.
PROVIDES: ``check_rules(rules, probe, *, now=None) -> list[AlertEvent]`` and
  the ``Probe`` type alias.
INVARIANTS (contract hard law L5 — degrade, never crash):
  * ``check_rules`` never raises.  A probe that raises, a probe that returns
    garbage and a probe that times out are all "no reading for this rule":
    the rule is skipped, the failure is logged once at warning level and the
    ``alerts_probe_failures_total{kind}`` counter turns.  One unreachable
    server must not stop the other rules in the same sweep.
  * ``None`` from the probe is a deliberate skip (target unreachable, probe
    declined) and is counted as ``alerts_probe_skips_total{kind}`` — it is
    NOT a failure and NOT a zero reading.
  * Disabled rules are never probed: a sweep must not connect to a host to
    answer a question the operator already muted.
  * Time is injected (``now``), never read implicitly by tests, so every
    ``fired_at`` assertion is deterministic (contract hard law L3).
  * Order is the caller's rule order, so a rendering layer may rely on it.
EXTEND: hysteresis lives in the caller that owns per-rule previous state;
  do not add state to this function.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import TypeAlias

from ..metrics import METRICS
from .evaluator import evaluate
from .models import AlertEvent, AlertRule
from .notifier import render_event_text

logger = logging.getLogger(__name__)

__all__ = ["Probe", "Clock", "check_rules"]

#: ``probe(rule) -> percent`` or ``None`` when the target cannot be read.
Probe: TypeAlias = Callable[[AlertRule], Awaitable[float | None]]

#: Injectable wall clock (epoch seconds) so tests stay deterministic.
Clock: TypeAlias = Callable[[], float]


async def check_rules(
    rules: Iterable[AlertRule],
    probe: Probe,
    *,
    now: Clock | None = None,
) -> list[AlertEvent]:
    """Probes every enabled rule once and returns the events that fired.

    See the module INVARIANTS: this coroutine never raises, never sleeps and
    never mutates its input.
    """
    clock: Clock = now if now is not None else time.time
    events: list[AlertEvent] = []

    for rule in rules:
        if not rule.enabled:
            METRICS.inc("alerts_rules_skipped_total", reason="disabled")
            continue

        reading = await _safe_probe(rule, probe)
        if reading is None:
            continue

        if not evaluate(rule, reading):
            continue

        fired_at = float(clock())
        event = AlertEvent(
            owner_id=rule.owner_id,
            alias=rule.alias,
            kind=rule.kind,
            threshold=rule.threshold,
            value=float(reading),
            fired_at=fired_at,
            message=render_event_text(
                rule=rule, value=float(reading), fired_at=fired_at
            ),
            rule_id=rule.id,
        )
        events.append(event)
        METRICS.inc("alerts_fired_total", kind=rule.kind)

    return events


async def _safe_probe(rule: AlertRule, probe: Probe) -> float | None:
    """Runs one probe, converting every failure mode into a logged skip.

    A non-numeric return is treated exactly like a raising probe: the probe
    contract is ``float | None``, so anything else is a bug in the wiring and
    must not be compared against a threshold.
    """
    try:
        reading = await probe(rule)
    except Exception as exc:  # noqa: BLE001 - a probe must never escape
        METRICS.inc("alerts_probe_failures_total", kind=rule.kind)
        logger.warning(
            "alert probe failed for %s/%s: %s", rule.alias, rule.kind, exc
        )
        return None

    if reading is None:
        METRICS.inc("alerts_probe_skips_total", kind=rule.kind)
        logger.debug("alert probe skipped %s/%s", rule.alias, rule.kind)
        return None

    if isinstance(reading, bool) or not isinstance(reading, (int, float)):
        METRICS.inc("alerts_probe_failures_total", kind=rule.kind)
        logger.warning(
            "alert probe for %s/%s returned %r, expected a number or None",
            rule.alias,
            rule.kind,
            reading,
        )
        return None

    return float(reading)
