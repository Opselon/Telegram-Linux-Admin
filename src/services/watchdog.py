"""Platform watchdog: periodic server reachability + threshold checks.

One lightweight loop (default every 120s, ``TLA_ALERT_CHECK_INTERVAL``):
* probes every registered server once with a cheap SSH ``true``;
* emits ``server_offline`` / recovery alerts through NotificationService
  (cooldown + dedup handled there);
* evaluates threshold alerts for fresh monitoring snapshots when present.

Deliberately NOT a high-frequency monitor: the web dashboard polls on demand;
this loop exists for push alerts when nobody is watching.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os

from .. import database
from .permissions import Actor

logger = logging.getLogger(__name__)


class ServerWatchdog:
    """Probes servers and emits offline/recovery alerts."""

    def __init__(self, services, *, interval: float | None = None) -> None:
        self.services = services
        self.interval = interval or float(os.environ.get("TLA_ALERT_CHECK_INTERVAL", "120"))
        self._offline: set[int] = set()  # server ids currently believed offline
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="server-watchdog")

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.check_all()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("watchdog check failed")
            await asyncio.sleep(self.interval)

    async def check_all(self) -> dict[str, int]:
        servers = database.get_all_servers()
        stats = {"online": 0, "offline": 0}
        for server in servers:
            actor = self._actor_for(server)
            if actor is None:
                continue
            server_id = int(server["id"])
            alias = str(server["alias"])
            try:
                await asyncio.wait_for(
                    self.services.manager.execute(actor.owner_id, alias, "true", timeout=15),
                    timeout=20,
                )
            except Exception:
                stats["offline"] += 1
                if server_id not in self._offline:
                    self._offline.add(server_id)
                    await self.services.notifications.emit(
                        "server_offline",
                        scope=alias,
                        severity="critical",
                        alias=alias,
                        hostname=str(server["hostname"]),
                    )
                continue
            stats["online"] += 1
            if server_id in self._offline:
                self._offline.discard(server_id)
                await self.services.notifications.emit(
                    "server_recovered", scope=alias, severity="info", alias=alias,
                )
        return stats

    @staticmethod
    def _actor_for(server: dict) -> Actor | None:
        """Actor owning this server; None when the owner account vanished."""
        from . import store

        owner_account_id = server.get("owner_account_id")
        if owner_account_id is not None:
            account = store.get_account(int(owner_account_id))
        else:
            account = store.ensure_account(
                store.KIND_TELEGRAM, str(server["owner_id"])
            )
        if account is None:
            return None
        return store.actor_from_account(account)
