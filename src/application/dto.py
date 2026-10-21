"""Application DTOs: the shapes passed between use cases and presentation."""

from __future__ import annotations

from dataclasses import dataclass

from src.domain.targets import ServerTarget


@dataclass(frozen=True, slots=True)
class Principal:
    """The acting Telegram user, with the permission verdict already resolved."""

    user_id: int
    is_admin: bool = False


@dataclass(frozen=True, slots=True)
class TargetView:
    """Presentation-ready projection of a target (no secrets, no raw rows)."""

    alias: str
    kind: str
    is_local: bool
    hostname: str = ""
    user: str = ""

    @classmethod
    def from_target(cls, target: ServerTarget) -> "TargetView":
        return cls(
            alias=target.alias,
            kind=target.kind.value,
            is_local=target.is_local,
            hostname=target.hostname,
            user=target.user,
        )
