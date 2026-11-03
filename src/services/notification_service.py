"""Notification service: threshold alerts with dedup, cooldown, and opt-outs.

Fan-out targets register as async callables; the Bale adapter subscribes a
bot-message sender, the web API subscribes an SSE broker.  Dedup key is
``(scope, kind)`` so the same alert cannot flood; each category honors a
cooldown and per-account category opt-outs.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..metrics import METRICS
from . import store

CATEGORY_SERVER_OFFLINE = "server_offline"
CATEGORY_SSH_AUTH = "ssh_auth_failure"
CATEGORY_CPU = "cpu_high"
CATEGORY_RAM = "ram_high"
CATEGORY_DISK = "disk_low"
CATEGORY_SERVICE = "service_failed"
CATEGORY_BACKUP = "backup_failed"
CATEGORY_SECURITY = "security_violation"
CATEGORY_COMMAND = "command_failed"
CATEGORY_SYSTEM = "system_error"

KNOWN_CATEGORIES = frozenset({
    CATEGORY_SERVER_OFFLINE, CATEGORY_SSH_AUTH, CATEGORY_CPU, CATEGORY_RAM,
    CATEGORY_DISK, CATEGORY_SERVICE, CATEGORY_BACKUP, CATEGORY_SECURITY,
    CATEGORY_COMMAND, CATEGORY_SYSTEM,
})

DEFAULT_COOLDOWN = float(os.environ.get("TLA_ALERT_COOLDOWN", "300"))
Subscriber = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass
class _Recent:
    last_sent: dict[tuple[str, str], float] = field(default_factory=dict)
    events: deque = field(default_factory=lambda: deque(maxlen=200))


@dataclass
class NotificationService:
    cooldown: float = DEFAULT_COOLDOWN
    _subscribers: list[Subscriber] = field(default_factory=list, repr=False)
    _state: _Recent = field(default_factory=_Recent, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def subscribe(self, fn: Subscriber) -> None:
        """Registers an async listener ``(event_name, payload)``."""
        self._subscribers.append(fn)

    async def emit(self, kind: str, *, scope: str = "", severity: str = "warning",
                   accounts: list[int] | None = None, **details: Any) -> None:
        """Emits one alert after dedup + cooldown; never raises."""
        key = (scope, kind)
        now = time.time()
        async with self._lock:
            last = self._state.last_sent.get(key)
            if last is not None and now - last < self.cooldown:
                METRICS.inc("alerts_deduplicated_total")
                return
            self._state.last_sent[key] = now
            payload: dict[str, Any] = {
                "kind": kind, "scope": scope, "severity": severity,
                "at": now, **{k: str(v)[:500] for k, v in details.items()},
            }
            self._state.events.appendleft(payload)
        METRICS.inc("alerts_emitted_total")
        recipients = accounts if accounts is not None else [
            a.id for a in store.list_accounts()
        ]
        for fn in list(self._subscribers):
            try:
                await fn(kind, {**payload, "recipients": recipients})
            except Exception:  # a broken listener must never break the caller
                METRICS.inc("alert_listener_errors_total")

    async def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        return list(self._state.events)[: max(0, limit)]

    # -- threshold evaluation ------------------------------------------------
    async def evaluate_snapshot(self, actor_scope: str, snapshot: dict[str, Any],
                                *, cpu_threshold: float = 90.0, ram_threshold: float = 90.0,
                                disk_threshold: float = 90.0) -> None:
        """Applies alert thresholds to one monitoring snapshot."""
        cpu = float(snapshot.get("cpu_percent", 0) or 0)
        ram = float(snapshot.get("ram_percent", 0) or 0)
        if cpu >= cpu_threshold:
            await self.emit(CATEGORY_CPU, scope=actor_scope, severity="warning",
                            cpu=cpu, threshold=cpu_threshold)
        if ram >= ram_threshold:
            await self.emit(CATEGORY_RAM, scope=actor_scope, severity="warning",
                            ram=ram, threshold=ram_threshold)
        for disk in snapshot.get("disks", []):
            used = float(disk.get("used_percent", 0) or 0)
            if used >= disk_threshold:
                await self.emit(CATEGORY_DISK, scope=actor_scope, severity="warning",
                                mount=disk.get("mount", "?"), used=used)

    def recipient_filter(self, account_id: int, kind: str) -> bool:
        """False when the account opted out of this alert category."""
        return kind not in store.get_disabled_categories(account_id)
