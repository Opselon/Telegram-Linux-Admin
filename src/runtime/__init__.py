"""Runtime abstraction for command execution targets.

PURPOSE:
    Package entry point. Re-exports the public runtime surface so callers write
    ``from src.runtime import ExecutionTarget, TargetResolver`` instead of
    importing individual modules; the submodules stay independently importable
    (no cycles: ``target`` imports nothing from this package).

OWNER:
    lane runtime (src/runtime/**, tests/test_runtime_*.py)

CONSUMES:
    src.runtime.target (contracts), src.runtime.local (bot-host execution),
    src.runtime.remote (SSH execution), src.runtime.resolver (alias mapping).

PROVIDES:
    TargetKind, TargetInfo, ExecutionTarget, TargetError, LocalTarget,
    RemoteTarget, TargetResolver, LOCAL_ALIAS, resolve_target.

INVARIANTS:
    * Importing this package performs no I/O and no configuration read.
    * Every name the orchestrator wires is re-exported here with the exact
      spelling from CONTRACT.md, so wiring needs no renames.
"""

from __future__ import annotations

from .local import LocalTarget
from .remote import RemoteTarget
from .resolver import LOCAL_ALIAS, TargetResolver, resolve_target
from .target import (
    DEFAULT_MAX_BYTES,
    DEFAULT_TIMEOUT,
    ExecutionTarget,
    TargetError,
    TargetInfo,
    TargetKind,
)

__all__ = [
    "DEFAULT_MAX_BYTES",
    "DEFAULT_TIMEOUT",
    "LOCAL_ALIAS",
    "ExecutionTarget",
    "LocalTarget",
    "RemoteTarget",
    "TargetError",
    "TargetInfo",
    "TargetKind",
    "TargetResolver",
    "resolve_target",
]
