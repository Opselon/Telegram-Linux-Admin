"""Ports: contracts the domain needs from the outside world.

Protocol bodies are deliberately empty (``...``) — that is the canonical
spelling for a Protocol member and satisfies both ruff's F821/F811 checks
and Pyright's "must return a value" complaint.  Nothing here imports
Telegram, asyncssh or sqlite.
"""

from __future__ import annotations

from typing import AsyncIterator, Protocol, Sequence, runtime_checkable

from .targets import ServerTarget


@runtime_checkable
class ServerRepository(Protocol):
    """Persistence contract for SSH targets owned by a Telegram user."""

    def list_targets(self, owner_id: int) -> Sequence[ServerTarget]:
        """Returns every stored SSH target for *owner_id* (never local)."""
        ...

    def find(self, owner_id: int, alias: str) -> ServerTarget | None:
        """Returns one stored target or ``None`` when absent/not owned."""
        ...

    def exists(self, owner_id: int, alias: str) -> bool:
        """True when *alias* is already taken for this owner."""
        ...

    def remove(self, owner_id: int, alias: str) -> bool:
        """Deletes a target; returns whether a row was removed."""
        ...

    def count(self, owner_id: int) -> int:
        """Number of stored SSH targets for the owner."""
        ...


@runtime_checkable
class CommandTransport(Protocol):
    """Runs commands and streams their output for one target kind."""

    def run(
        self, target: ServerTarget, command: str
    ) -> AsyncIterator[tuple[str, str]]:
        """Yields ``(chunk, stream)`` pairs until the command finishes.

        ``stream`` is either ``"stdout"`` or ``"stderr"``.
        """
        ...

    async def disconnect(self, target: ServerTarget) -> None:
        """Releases pooled resources for *target* (idempotent)."""
        ...


@runtime_checkable
class TransportRouter(Protocol):
    """Selects the transport for a target; one entry point for the app layer."""

    def transport_for(self, target: ServerTarget) -> CommandTransport:
        """Returns the transport responsible for *target*'s kind."""
        ...
