"""Target entities: everything a user can run commands against.

A *target* is either a remote SSH server (stored, encrypted, host-key pinned)
or the bot's own host — the "self-managed" target every installer gets for
free.  Keeping both behind one immutable entity lets menus, policies and
transports stay oblivious to the difference.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from .errors import TargetNotFound

#: Reserved alias of the bot's own host.  It satisfies the alias validator
#: (so it is always callback-safe) and can never collide with a user alias
#: because :class:`~src.infrastructure.persistence.sqlite_target_repository.SqliteTargetRepository`
#: refuses to store it for SSH targets.
LOCAL_ALIAS = "self-host"
LOCAL_HOSTNAME = "127.0.0.1"
LOCAL_USER = "local"


class TargetKind(str, Enum):
    """How commands reach a target."""

    SSH = "ssh"
    LOCAL = "local"


@dataclass(frozen=True, slots=True)
class ServerTarget:
    """Immutable view of a manageable target."""

    alias: str
    kind: TargetKind
    owner_id: int
    hostname: str = ""
    user: str = ""
    has_credentials: bool = False

    @property
    def is_local(self) -> bool:
        return self.kind is TargetKind.LOCAL

    @property
    def display_name(self) -> str:
        """Stable label used in logs and callback payloads."""
        return LOCAL_ALIAS if self.is_local else self.alias

    def for_owner(self, owner_id: int | None) -> bool:
        """True when *owner_id* may see this target at all (strict isolation)."""
        return owner_id is not None and self.owner_id == owner_id


def local_target(owner_id: int) -> ServerTarget:
    """Builds the virtual target representing the bot's own host."""
    return ServerTarget(
        alias=LOCAL_ALIAS,
        kind=TargetKind.LOCAL,
        owner_id=owner_id,
        hostname=LOCAL_HOSTNAME,
        user=LOCAL_USER,
        has_credentials=True,
    )


def ssh_target_from_row(row: Mapping[str, Any]) -> ServerTarget:
    """Builds a target from a raw ``servers`` row (keys per database.py)."""
    return ServerTarget(
        alias=str(row["alias"]),
        kind=TargetKind.SSH,
        owner_id=int(row["owner_id"]),
        hostname=str(row.get("hostname") or ""),
        user=str(row.get("user") or ""),
        has_credentials=bool(row.get("password") or row.get("key_path")),
    )


def resolve_alias(alias: str) -> str:
    """Normalizes an incoming alias and rejects empty values."""
    normalized = (alias or "").strip()
    if not normalized:
        raise TargetNotFound(normalized)
    return normalized
