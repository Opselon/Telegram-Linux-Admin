"""PURPOSE: Delivery of fired alert events plus the one renderer for their
localized text.

OWNER: lane `alerts` of wave 3.4 (branch feat/3.4-alerts).
CONSUMES: src.alerts.models (AlertEvent/AlertRule, kind labels),
  src.localization.translate, src.metrics.METRICS.  No Telegram import.
PROVIDES: ``Sender``, ``notify(event, sender=None) -> bool`` and
  ``render_event_text(...)`` (used by the checker and the UI).
INVARIANTS (contract hard law L5 — degrade, never crash):
  * ``notify`` never raises.  "No sender" is the DEFAULT deployment: the
    event is counted and reported as not-delivered, because bot wiring is
    orchestrator work and this package stays transport free.
  * Any exception from an injected sender is logged once at warning level
    and turned into ``alerts_notify_failures_total``; a delivery problem must
    never reach the bot loop.
  * ``render_event_text`` is the single place an event becomes user-visible
    text, so L4 (every string through translate) has one audit point.  It
    prints only identifiers the recipient already owns — alias, kind, numbers
    — never a hostname, credential or payload (contract hard law L2).
EXTEND: a new transport is another injected ``Sender``; nothing here changes.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import TypeAlias

from ..localization import translate
from ..metrics import METRICS
from .models import AlertEvent, AlertRule, alert_kind_label

logger = logging.getLogger(__name__)

__all__ = ["Sender", "format_ts", "notify", "render_event_text"]

#: ``sender(event) -> delivered?``.  The orchestrator injects an async
#: function that resolves the owner's chat and sends the rendered text.
Sender: TypeAlias = Callable[[AlertEvent], Awaitable[bool]]


def format_ts(fired_at: float) -> str:
    """UTC ``HH:MM`` stamp for an epoch value.

    UTC (not the host's localtime) keeps a rendered message identical on every
    machine and makes the value a test can pin without clock faking.
    """
    return datetime.fromtimestamp(float(fired_at), tz=timezone.utc).strftime("%H:%M")


def render_event_text(
    event: AlertEvent | None = None,
    *,
    rule: AlertRule | None = None,
    value: float | None = None,
    fired_at: float | None = None,
    language: str | None = None,
) -> str:
    """One localized line describing a fired alert.

    Accepts either a finished :class:`AlertEvent` (the stored form) or the
    raw ``rule + value + fired_at`` the checker holds before it freezes the
    snapshot, so the message kept on the event and any re-render go through
    the same string.
    """
    if event is not None:
        alias = event.alias
        kind = event.kind
        threshold = event.threshold
        reading = float(event.value)
        when = float(event.fired_at)
    elif rule is not None and value is not None and fired_at is not None:
        alias = rule.alias
        kind = rule.kind
        threshold = rule.threshold
        reading = float(value)
        when = float(fired_at)
    else:
        raise ValueError(
            "render_event_text needs an event, or rule plus value and fired_at"
        )

    return translate(
        "alerts_event_line",
        language,
        alias=alias,
        kind=alert_kind_label(kind, language),
        value=f"{reading:.1f}",
        threshold=threshold,
        ts=format_ts(when),
    )


async def notify(event: AlertEvent, sender: Sender | None = None) -> bool:
    """Delivers one event, or records it and returns False when unwired.

    Returns True only when an injected sender reported delivery.  Never raises
    — see the module INVARIANTS.
    """
    METRICS.inc("alerts_events_total", kind=event.kind)
    if sender is None:
        logger.info(
            "alert event recorded (no sender wired): %s on %s at %s",
            event.kind,
            event.alias,
            event.fired_at,
        )
        return False

    try:
        delivered = await sender(event)
    except Exception as exc:  # noqa: BLE001 - a failed send must stop here
        METRICS.inc("alerts_notify_failures_total", kind=event.kind)
        logger.warning("alert notify failed for %s: %s", event.alias, exc)
        return False

    if delivered:
        METRICS.inc("alerts_notify_sent_total", kind=event.kind)
        return True

    METRICS.inc("alerts_notify_failures_total", kind=event.kind)
    return False
