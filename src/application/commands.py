"""Command use cases: policy-checked execution against any transport.

Handlers used to call ``ssh_manager.run_command(user_id, alias, cmd)``
directly, which meant every feature silently assumed SSH.  They now call
:class:`CommandService`, which resolves the target through the catalog,
enforces the target access policy, and only then delegates to the transport
router.  Adding a target kind costs one adapter, not a rewrite of every menu.
"""

from __future__ import annotations

import logging
import time
from typing import AsyncIterator

from src.domain.policies import TargetAccessPolicy
from src.domain.ports import TransportRouter
from src.domain.targets import ServerTarget

from .catalog import TargetCatalog
from .dto import Principal

logger = logging.getLogger(__name__)


class CommandService:
    """Executes commands for a principal against a resolved target."""

    def __init__(
        self,
        catalog: TargetCatalog,
        router: TransportRouter,
        *,
        policy: TargetAccessPolicy | None = None,
    ) -> None:
        self._catalog = catalog
        self._router = router
        self._policy = policy or TargetAccessPolicy()

    def resolve(self, principal: Principal, alias: str) -> ServerTarget:
        """Resolves and authorizes a target; raises domain errors on failure."""
        target = self._catalog.find(principal, alias)
        self._policy.ensure_can_use(target, is_admin=principal.is_admin)
        return target

    async def stream(
        self, principal: Principal, alias: str, command: str
    ) -> AsyncIterator[tuple[str, str]]:
        """Streams ``(chunk, stream)`` tuples for *command* on *alias*."""
        target = self.resolve(principal, alias)
        transport = self._router.transport_for(target)
        started = time.monotonic()
        async for item in transport.run(target, command):
            yield item
        logger.info(
            "command.streamed user=%s target=%s kind=%s duration_ms=%d",
            principal.user_id,
            target.alias,
            target.kind.value,
            int((time.monotonic() - started) * 1000),
        )

    async def snapshot(
        self, principal: Principal, alias: str, command: str, *, limit: int = 3800
    ) -> str:
        """Runs *command* and returns its combined output, capped to *limit*.

        Used by status/monitoring menus where streaming would be noise.
        """
        out: list[str] = []
        err: list[str] = []
        async for chunk, stream in self.stream(principal, alias, command):
            if stream == "stdout":
                out.append(chunk)
            elif stream == "stderr":
                err.append(chunk)
        combined = ("".join(out) + "".join(err)).strip()
        return combined[:limit] if combined else ""

    async def disconnect(self, principal: Principal, alias: str) -> None:
        target = self.resolve(principal, alias)
        await self._router.transport_for(target).disconnect(target)
