"""SSH transport adapter: keeps :class:`SSHManager` behind the port.

The hardened manager predates the layered refactor (connection pooling, host
key pinning, quoted commands).  Rather than rewriting it, this adapter adapts
its ``(owner_id, alias)`` signature to the domain's :class:`ServerTarget`, so
the application layer only ever deals in targets.
"""

from __future__ import annotations

from typing import AsyncIterator

from src.domain.errors import TransportUnavailable
from src.domain.targets import ServerTarget
from src.ssh_manager import SSHManager


class SshTransport:
    """Implements :class:`src.domain.ports.CommandTransport` over SSH."""

    def __init__(self, manager: SSHManager) -> None:
        self._manager = manager

    async def run(
        self, target: ServerTarget, command: str
    ) -> AsyncIterator[tuple[str, str]]:
        if target.is_local:
            raise TransportUnavailable(target.alias, "SSH transport got a local target")
        async for item in self._manager.run_command(target.owner_id, target.alias, command):
            yield item

    async def disconnect(self, target: ServerTarget) -> None:
        if target.is_local:
            return
        await self._manager.disconnect(target.owner_id, target.alias)
