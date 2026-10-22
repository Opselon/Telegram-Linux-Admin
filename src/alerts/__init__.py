"""PURPOSE: Proactive threshold alerts for the 3.4 wave (lane `alerts`).

OWNER: lane `alerts` of wave 3.4 (branch feat/3.4-alerts).
CONSUMES: nothing at import time — the submodules import lazily so that
  ``import src.alerts`` never opens a database, reads a config section or
  requires python-telegram-bot.  The command surface (``src.alerts.ui``)
  does import PTB, which is why it is NOT re-exported here.
PROVIDES: models (AlertRule/AlertEvent), evaluator.evaluate, checker.
  check_rules, storage (SCHEMA_SQL + CRUD), notifier.notify.
INVARIANTS:
  * The package is transport-free and state-free: the scheduled sweep and the
    Telegram sender are INJECTED by the orchestrator (application.py /
    src.ops registration), never created here.
  * Nothing in this package registers a handler or starts a task.
EXTEND: new submodules stay import-light; ui.py is the only PTB consumer.
"""

from __future__ import annotations

from .checker import check_rules
from .evaluator import evaluate
from .models import ALERT_KINDS, AlertEvent, AlertRule, alert_kind_label
from .notifier import notify, render_event_text
from .storage import SCHEMA_SQL

__all__ = [
    "ALERT_KINDS",
    "SCHEMA_SQL",
    "AlertEvent",
    "AlertRule",
    "alert_kind_label",
    "check_rules",
    "evaluate",
    "notify",
    "render_event_text",
]

