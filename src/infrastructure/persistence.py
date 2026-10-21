"""Persistence adapter: translates ``servers`` rows into domain targets.

A thin wrapper over the existing :mod:`src.database` functions so the domain
never sees a raw sqlite row and main.py never re-implements owner scoping.
"""

from __future__ import annotations

from typing import Sequence

from src.database import get_all_servers, get_server, remove_server
from src.domain.targets import LOCAL_ALIAS, ServerTarget, ssh_target_from_row


class SqliteTargetRepository:
    """Implements :class:`~src.domain.ports.ServerRepository` over sqlite."""

    def list_targets(self, owner_id: int) -> Sequence[ServerTarget]:
        rows = get_all_servers(owner_id)
        return [ssh_target_from_row(row) for row in rows]

    def find(self, owner_id: int, alias: str) -> ServerTarget | None:
        row = get_server(owner_id, alias)
        return ssh_target_from_row(row) if row else None

    def exists(self, owner_id: int, alias: str) -> bool:
        return get_server(owner_id, alias) is not None

    def remove(self, owner_id: int, alias: str) -> bool:
        return remove_server(owner_id, alias)

    def count(self, owner_id: int) -> int:
        return len(get_all_servers(owner_id))


_target_repository = SqliteTargetRepository()


def target_repository() -> SqliteTargetRepository:
    """Process-wide accessor used by the application layer."""
    return _target_repository


def is_local_alias(alias: str) -> bool:
    """Repository-side guard so the reserved alias is never stored as SSH."""
    return (alias or "").strip().lower() == LOCAL_ALIAS
