"""Domain policies: who may target what, and under which deployment flags.

The self-host target is powerful (the bot's process identity, usually root or
a dedicated service user), so access is fail-closed:

* it is only offered to an *administrator* resolved by the permission manager
  (the installer lands there automatically),
* it disappears entirely when ``TLA_LOCAL_TARGET_ENABLED`` is falsy,
* no SSH target may ever claim its reserved alias.
"""

from __future__ import annotations

import os

from .errors import TargetAccessDenied, TargetDisabled
from .targets import LOCAL_ALIAS, ServerTarget

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def local_target_enabled() -> bool:
    """Reads the deployment switch for the self-host target (default: on)."""
    raw = os.environ.get("TLA_LOCAL_TARGET_ENABLED", "1").strip().lower()
    return raw in _TRUTHY


def is_reserved_alias(alias: str) -> bool:
    """Case-insensitive reserved-alias check used by both repo and UI."""
    return (alias or "").strip().lower() == LOCAL_ALIAS


class TargetAccessPolicy:
    """Decides whether a user may use a target, without doing any I/O."""

    def __init__(self, *, local_enabled: bool | None = None) -> None:
        self._local_enabled = local_target_enabled() if local_enabled is None else local_enabled

    @property
    def local_enabled(self) -> bool:
        return self._local_enabled

    def default_target_for(self, owner_id: int) -> ServerTarget | None:
        """The implicit target an owner starts with, if the flag allows it."""
        if not self._local_enabled:
            return None
        from .targets import local_target

        return local_target(owner_id)

    def ensure_can_use(self, target: ServerTarget, *, is_admin: bool) -> None:
        """Raises when *target* must not be used by this principal.

        Fail-closed by construction: every branch either returns or raises.
        """
        if not target.is_local:
            return
        if not self._local_enabled:
            raise TargetDisabled(target.alias)
        if not is_admin:
            raise TargetAccessDenied(target.alias, "self-host target is admin-only")
