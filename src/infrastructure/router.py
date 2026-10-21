"""Transport router: the only place that maps a target kind to an adapter."""

from __future__ import annotations

from src.domain.errors import TransportUnavailable
from src.domain.ports import CommandTransport
from src.domain.targets import ServerTarget


class DefaultTransportRouter:
    """Implements :class:`src.domain.ports.TransportRouter`."""

    def __init__(
        self,
        *,
        ssh: CommandTransport | None = None,
        local: CommandTransport | None = None,
    ) -> None:
        self._ssh = ssh
        self._local = local

    def transport_for(self, target: ServerTarget) -> CommandTransport:
        transport = self._local if target.is_local else self._ssh
        if transport is None:
            raise TransportUnavailable(
                target.alias, f"no transport registered for kind '{target.kind.value}'"
            )
        return transport

    @property
    def local_available(self) -> bool:
        return self._local is not None
