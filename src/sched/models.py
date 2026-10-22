"""Purpose: frozen value objects for scheduled backups, validated on construction.

OWNER: lane `backup` of wave 3.4 (branch feat/3.4-backup), package src/sched/.
CONSUMES: stdlib only (dataclasses, datetime). No src.* imports, no I/O.
PROVIDES: :class:`BackupPlan` (what the admin configured) and
  :class:`BackupRun` (what one execution did), both immutable.
INVARIANTS:
  * a plan that constructs successfully is storable: ``alias`` is a non-empty
    str, ``hour`` is an int in 0..23, ``keep`` is an int >= 1, ``enabled`` is
    a bool, ``owner_id`` is a non-negative int;
  * every violation raises ``ValueError`` at construction, so a bad row can
    never reach the filesystem or the runner;
  * ``BackupRun.detail`` is operator-facing text and must never carry a
    secret: callers pass exception *messages* from the archive layer, never
    credentials, connection strings or file contents (CONTRACT hard law L2);
  * ``BackupRun.status`` is one of :data:`RUN_STATUSES`.
EXTEND: add a field with a default so existing positional callers keep
  working, and validate it in ``__post_init__``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

__all__ = ["BackupPlan", "BackupRun", "RUN_STATUSES", "OK", "RETRIEVAL_FAILED", "OVERWRITE_FAILED"]

#: Terminal outcomes of one scheduled run.
OK = "ok"
RETRIEVAL_FAILED = "retrieval_failed"
OVERWRITE_FAILED = "overwrite_failed"
RUN_STATUSES: tuple[str, ...] = (OK, RETRIEVAL_FAILED, OVERWRITE_FAILED)


def _require_int(name: str, value: object, low: int, high: int | None = None) -> int:
    """Validates an integer bound, rejecting bools (``True`` is not an hour)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an int, got {type(value).__name__}")
    if value < low:
        raise ValueError(f"{name} must be >= {low}, got {value}")
    if high is not None and value > high:
        raise ValueError(f"{name} must be <= {high}, got {value}")
    return value


@dataclass(frozen=True)
class BackupPlan:
    """One scheduled backup: which server, which local hour, how many to keep.

    ``owner_id`` is the plan's owner (the admin who created it).  It is not in
    the contract's field list but the contract also requires ownership checks
    on every mutation, so the owner travels with the plan instead of being
    re-derived at each call site.
    """

    alias: str
    hour: int
    keep: int = 7
    enabled: bool = True
    owner_id: int = 0
    id: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.alias, str) or not self.alias.strip():
            raise ValueError("alias must be a non-empty string")
        if self.alias != self.alias.strip():
            raise ValueError("alias must not have surrounding whitespace")
        # The alias becomes a directory name under data/backups/scheduled/,
        # so reject anything that could escape it before it reaches disk.
        if self.alias in (".", "..") or any(sep in self.alias for sep in ("/", "\\", "\0")):
            raise ValueError("alias must be a plain directory-safe name")
        object.__setattr__(self, "hour", _require_int("hour", self.hour, 0, 23))
        object.__setattr__(self, "keep", _require_int("keep", self.keep, 1))
        object.__setattr__(self, "owner_id", _require_int("owner_id", self.owner_id, 0))
        object.__setattr__(self, "id", _require_int("id", self.id, 0))
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be a bool")


@dataclass(frozen=True)
class BackupRun:
    """One execution record: which plan, when, what happened.

    ``started_at`` is a naive local instant (the plan's time base, see
    :mod:`src.sched.schedule`).  ``detail`` is a single human-readable line
    safe to log and to show an operator — never a secret.
    """

    plan_id: int
    alias: str
    started_at: datetime
    status: str = OK
    detail: str = field(default="")

    def __post_init__(self) -> None:
        if self.status not in RUN_STATUSES:
            raise ValueError(f"status must be one of {RUN_STATUSES}, got {self.status!r}")
        if not isinstance(self.alias, str) or not self.alias.strip():
            raise ValueError("alias must be a non-empty string")
        if not isinstance(self.started_at, datetime):
            raise ValueError("started_at must be a datetime")
        if not isinstance(self.detail, str):
            raise ValueError("detail must be a string")

    @property
    def ok(self) -> bool:
        """True when the run completed and the archive is in place."""
        return self.status == OK
