"""SSH execution adapter: the same streaming contract, backed by SSHManager.

PURPOSE:
    The command pipeline (jobs, handlers) must not know that a command leaves
    the box. This module is the only place that does: it wraps
    ``SSHManager.run_command`` in the ``ExecutionTarget`` protocol and records
    the target metadata (alias/hostname/user) the audit and UI layers need.

OWNER:
    lane runtime (src/runtime/**, tests/test_runtime_*.py)

CONSUMES:
    an ``SSHManager``-like object (duck-typed so tests can inject a fake) and
    ``src.ssh_manager.run_command``'s streaming semantics.

PROVIDES:
    RemoteTarget -- ExecutionTarget whose ``run`` delegates to a manager.

INVARIANTS:
    * ``run`` is a pass-through: the manager owns the connection pool, the
      timeout, the byte cap, the pid-first ordering and the exception types.
      Do not add translation layers that would hide ``HostKeyMismatch`` or
      ``CommandOutputTooLarge`` from callers.
    * ``info`` reports ``TargetKind.REMOTE`` and never invents a hostname: an
      empty hostname means "ask the manager's database".
    * ``close`` never touches the manager's pooled connections; tearing down a
      shared pool from one target would break every other user of the alias.

EXTEND:
    Adapt a different transport by adding a sibling adapter, not by widening
    this class.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any, Protocol

from ..metrics import METRICS
from .target import (
    DEFAULT_MAX_BYTES,
    DEFAULT_TIMEOUT,
    TargetError,
    TargetInfo,
    TargetKind,
)

logger = logging.getLogger(__name__)


class _RunCommandLike(Protocol):
    """Structural type of ``SSHManager.run_command`` (for typing the injection)."""

    def __call__(
        self,
        owner_id: int,
        alias: str,
        command: str,
        timeout: float = ...,
        max_bytes: int = ...,
    ) -> Any: ...


class _ManagerLike(Protocol):
    """Structural subset of SSHManager this adapter actually needs.

    Duck-typed on purpose: production passes a real ``SSHManager``, tests pass
    a fake with only ``run_command``. Importing the concrete class here would
    make the adapter untestable without an SSH server.
    """

    def run_command(
        self,
        owner_id: int,
        alias: str,
        command: str,
        timeout: float = DEFAULT_TIMEOUT,  # noqa: ASYNC109 - `timeout` is fixed by CONTRACT.md
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> Any: ...


class RemoteTarget:
    """Executes on a server reached through an ``SSHManager`` (real or fake).

    A thin adapter is not an indirection for its own sake: it is what lets the
    resolver hand the pipeline one object per command destination, with the
    metadata attached, instead of scattering ``(owner_id, alias)`` pairs.
    """

    def __init__(
        self,
        manager: Any,
        owner_id: int,
        alias: str,
        hostname: str = "",
        user: str = "",
    ) -> None:
        if manager is None:
            raise ValueError("RemoteTarget requires an SSHManager; got None.")
        if not alias or not str(alias).strip():
            raise ValueError("RemoteTarget requires a non-empty server alias.")
        self._manager = manager
        self._owner_id = int(owner_id)
        self._alias = str(alias)
        self._closed = False
        self.info = TargetInfo(
            alias=str(alias),
            hostname=str(hostname or ""),
            user=str(user or ""),
            kind=TargetKind.REMOTE,
            description="SSH target",
        )

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    @property
    def owner_id(self) -> int:
        """The Telegram user the underlying connection is scoped to."""
        return self._owner_id

    @property
    def alias(self) -> str:
        """The server alias this target runs on."""
        return self._alias

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    async def run(
        self,
        command: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,  # noqa: ASYNC109 - `timeout` is fixed by CONTRACT.md
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> AsyncIterator[tuple[str, str]]:
        """Delegates to ``SSHManager.run_command`` unchanged, then counts.

        Everything about the stream -- pid-first ordering, concurrent stdout /
        stderr pumping, exit-status chunk, timeout chunk and exception types --
        is the manager's contract, which this class must not reimplement.
        """
        if self._closed:
            raise TargetError("Remote target is closed; create a new target to run commands.")
        if not command or not command.strip():
            raise TargetError("No command given.")

        METRICS.inc("runtime_remote_commands_total")
        async for chunk, stream in self._manager.run_command(
            self._owner_id, self._alias, command, timeout=timeout, max_bytes=max_bytes
        ):
            yield chunk, stream

    async def close(self) -> None:
        """Marks the target unusable without touching the shared SSH pool.

        ``RemoteTarget`` never owns a connection, so closing it cannot close
        one; the manager stays usable for other targets on the same alias.
        """
        self._closed = True


__all__ = ["RemoteTarget"]


def _unused(_obj: _RunCommandLike) -> None:  # pragma: no cover - typing anchor
    """Keeps the run-command structural type referenced for type checkers."""
