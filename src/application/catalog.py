"""Target catalog use cases: listing, resolving and guarding targets.

This is the single place that knows a user's *effective* set of targets — the
stored SSH servers plus, for administrators, the bot's own host.  Handlers
asking "what can this user reach?" go through here, never through sqlite.
"""

from __future__ import annotations

from typing import Sequence

from src.domain.errors import TargetNotFound
from src.domain.policies import TargetAccessPolicy, is_reserved_alias
from src.domain.ports import ServerRepository
from src.domain.targets import ServerTarget, local_target

from .dto import Principal, TargetView


class TargetCatalog:
    """Use cases around the set of targets a principal may reach."""

    def __init__(
        self,
        repository: ServerRepository,
        policy: TargetAccessPolicy | None = None,
    ) -> None:
        self._repository = repository
        self._policy = policy or TargetAccessPolicy()

    # -- queries -----------------------------------------------------------
    def list_targets(self, principal: Principal) -> list[ServerTarget]:
        """Returns the principal's targets, self-host first when allowed."""
        targets: list[ServerTarget] = []
        self_host = self._policy.default_target_for(principal.user_id)
        if self_host is not None and self._policy.local_enabled and principal.is_admin:
            targets.append(self_host)
        targets.extend(self._repository.list_targets(principal.user_id))
        return targets

    def list_views(self, principal: Principal) -> list[TargetView]:
        """Convenience projection for the presentation layer."""
        return [TargetView.from_target(target) for target in self.list_targets(principal)]

    def find(self, principal: Principal, alias: str) -> ServerTarget:
        """Resolves *alias* for *principal* or raises :class:`TargetNotFound`.

        Resolution order is explicit: the reserved local alias first (admin
        only, fail-closed), then the owner's own SSH rows.  A local alias is
        never looked up in persistence, and an SSH alias can never shadow it.
        """
        normalized = (alias or "").strip()
        if not normalized:
            raise TargetNotFound(normalized)

        if is_reserved_alias(normalized):
            self_host = local_target(principal.user_id)
            self._policy.ensure_can_use(self_host, is_admin=principal.is_admin)
            return self_host

        stored = self._repository.find(principal.user_id, normalized)
        if stored is None:
            raise TargetNotFound(normalized)
        return stored

    def count_remote(self, principal: Principal) -> int:
        return self._repository.count(principal.user_id)

    def alias_available(self, principal: Principal, alias: str) -> bool:
        """False when *alias* is reserved or already used by this owner."""
        if is_reserved_alias(alias):
            return False
        return not self._repository.exists(principal.user_id, alias)

    # -- commands ----------------------------------------------------------
    def remove(self, principal: Principal, alias: str) -> bool:
        """Deletes an SSH target.  The self-host target is virtual and fixed."""
        if is_reserved_alias(alias):
            raise TargetNotFound(alias)
        return self._repository.remove(principal.user_id, alias)

    def self_host_available(self, principal: Principal) -> bool:
        """Whether the self-host target should be advertised to *principal*."""
        if not principal.is_admin or not self._policy.local_enabled:
            return False
        self._policy.ensure_can_use(local_target(principal.user_id), is_admin=True)
        return True

    def ordered_aliases(self, principal: Principal) -> Sequence[str]:
        return tuple(target.alias for target in self.list_targets(principal))
