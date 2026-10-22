"""Execution target contracts shared by the local and remote adapters.

PURPOSE:
    Defines what "a place a command can run" means for the whole bot: a small
    value object describing the target, a protocol both adapters satisfy, and
    the typed error callers catch. Nothing here executes anything, so importing
    this module is free of side effects and safe from any layer.

OWNER:
    lane runtime (src/runtime/**, tests/test_runtime_*.py)

CONSUMES:
    stdlib only (dataclasses, enum, typing). Deliberately no imports from
    ``src.ssh_manager`` here so the contracts stay dependency-free; the concrete
    adapters do the importing.

PROVIDES:
    TargetKind, TargetInfo, ExecutionTarget, TargetError, DEFAULT_MAX_BYTES

INVARIANTS:
    * ``TargetInfo`` is frozen and slotted: an identity, never a mutable record.
    * ``ExecutionTarget.run`` yields ``(chunk, stream)`` where ``stream`` is one
      of ``"pid"``, ``"stdout"``, ``"stderr"`` and the FIRST yield is always
      ``(pid, "pid")`` -- parity with ``SSHManager.run_command``.
    * ``TargetError`` is the only error a resolver raises for a target it
      refuses to build; it is never used to wrap execution failures.

EXTEND:
    Add a new backend (docker exec, systemd-run, ...) by implementing
    ``ExecutionTarget`` in a sibling module and teaching ``TargetResolver`` to
    return it. Do not widen this file with I/O.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

#: Default per-command output budget, identical to ``SSHManager.run_command``.
DEFAULT_MAX_BYTES = 2 * 1024 * 1024

#: Default per-command timeout, identical to ``SSHManager.run_command``.
DEFAULT_TIMEOUT = 60.0


class TargetKind(str, Enum):
    """Where a command actually executes.

    The bot must be honest about this: ``LOCAL`` means the process runs on the
    machine that hosts the bot (no SSH hop at all), ``REMOTE`` means a pooled
    SSH session. Audit lines and menus branch on it.
    """

    LOCAL = "local"
    REMOTE = "remote"


class TargetError(RuntimeError):
    """Raised when a target cannot be built or refuses to run.

    It exists so callers get one predictable exception type instead of a
    grab-bag of ``ValueError``/``KeyError`` when a user asks for a target that
    does not exist or is disabled by configuration.
    """


@dataclass(frozen=True, slots=True)
class TargetInfo:
    """Immutable identity of a target, shown to users and written to audit.

    ``alias`` is the user-facing name, ``kind`` tells local from remote. The
    remaining fields are display-only hints and may be empty when unknown, so
    callers must never use them for routing decisions.
    """

    alias: str
    hostname: str
    user: str
    kind: TargetKind
    description: str = ""


@runtime_checkable
class ExecutionTarget(Protocol):
    """Minimal surface the command pipeline needs from any target.

    Keeping the protocol this small is what lets the job queue, policy engine
    and handlers stay ignorant of SSH vs local execution.
    """

    info: TargetInfo

    def run(
        self,
        command: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> AsyncIterator[tuple[str, str]]:
        """Streams ``(chunk, stream)`` tuples for *command*; pid first."""
        ...

    async def close(self) -> None:
        """Releases whatever the target holds; safe to call more than once."""
        ...
