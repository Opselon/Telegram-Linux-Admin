"""Composition root: wires adapters into the application layer.

Import this module from ``main.py`` once the runtime objects exist; tests can
call :func:`build_service` with fakes instead.  No handler imports this file's
internals — they ask :class:`Container` for what they need.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.application.catalog import TargetCatalog
from src.application.commands import CommandService
from src.domain.policies import TargetAccessPolicy
from src.domain.ports import CommandTransport, ServerRepository
from src.infrastructure.local_transport import LocalTransport
from src.infrastructure.persistence import target_repository
from src.infrastructure.router import DefaultTransportRouter


@dataclass(slots=True)
class Container:
    """Runtime dependency container (composition root output)."""

    catalog: TargetCatalog
    commands: CommandService
    router: DefaultTransportRouter
    policy: TargetAccessPolicy

    def is_self_host(self, alias: str) -> bool:
        from src.domain.policies import is_reserved_alias

        return is_reserved_alias(alias)


def build_service(
    *,
    ssh_transport: CommandTransport | None = None,
    local_transport: CommandTransport | None = None,
    repository: ServerRepository | None = None,
    policy: TargetAccessPolicy | None = None,
) -> Container:
    """Builds the wired container, injecting defaults where omitted."""
    resolved_repository = repository or target_repository()
    resolved_policy = policy or TargetAccessPolicy()
    router = DefaultTransportRouter(
        ssh=ssh_transport,
        local=local_transport if local_transport is not None else LocalTransport(),
    )
    catalog = TargetCatalog(resolved_repository, resolved_policy)
    commands = CommandService(catalog, router, policy=resolved_policy)
    return Container(
        catalog=catalog,
        commands=commands,
        router=router,
        policy=resolved_policy,
    )
