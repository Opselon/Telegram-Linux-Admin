"""Turn a user-typed alias into an ``ExecutionTarget``.

PURPOSE:
    Handlers receive a bare string ("web1", "self") and must get back something
    runnable. Centralizing that lookup is what keeps local-vs-remote a
    *configuration* fact instead of an ``if`` scattered through every handler:
    flip ``local_enabled`` and every call site changes behavior at once.

OWNER:
    lane runtime (src/runtime/**, tests/test_runtime_*.py)

CONSUMES:
    an optional ``SSHManager``-like object (for remote targets) and
    ``src.database.get_server`` to look up host metadata for the audit trail.

PROVIDES:
    TargetResolver, LOCAL_ALIAS, resolve_target (convenience function),
    TargetError from src.runtime.target.

INVARIANTS:
    * ``LOCAL_ALIAS`` is exactly ``"self"``; never make it configurable per
      user, otherwise one user's alias could shadow another user's server.
    * ``"self"`` resolves to a LocalTarget ONLY when ``local_enabled=True``;
      otherwise raise ``TargetError`` with a message a user can act on.
    * Every other alias resolves to a RemoteTarget; alias matching is
      case-insensitive and strips whitespace, mirroring how users type.
    * ``resolve`` never returns None and never falls back to local.
    * Unknown aliases raise TargetError; a user typing a server that does not
      exist must not get a traceback.

EXTEND:
    New target kinds (containers, jump hosts) plug in here by adding a branch
    to ``resolve`` -- the ExecutionTarget protocol itself stays untouched.
"""

from __future__ import annotations

import logging
from typing import Any

from ..database import get_server
from .local import LocalTarget
from .remote import RemoteTarget
from .target import TargetError, TargetInfo, TargetKind

logger = logging.getLogger(__name__)

#: The reserved alias for "the machine the bot runs on".
LOCAL_ALIAS = "self"


class TargetResolver:
    """Maps a user-typed alias to a runnable target.

    ``local_enabled`` defaults to False because running commands on the bot
    host is a capability, not an accident: the resolver must fail closed until
    the operator opts in (env var or config).
    """

    def __init__(self, manager: Any = None, *, local_enabled: bool = False) -> None:
        self._manager = manager
        self._local_enabled = bool(local_enabled)
        self._local: LocalTarget | None = None

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    @property
    def local_enabled(self) -> bool:
        """Whether the reserved local alias is permitted (read-only view)."""
        return self._local_enabled

    def is_local_alias(self, alias: str) -> bool:
        """True when *alias* means "run here", i.e. equals :data:`LOCAL_ALIAS`.

        Accepts surrounding whitespace and any case; the check is cheap so
        handlers can call it before touching the database.
        """
        if alias is None:
            return False
        return str(alias).strip().lower() == LOCAL_ALIAS

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------
    def resolve(
        self,
        owner_id: int,
        alias: str,
        *,
        hostname: str = "",
        user: str = "",
    ) -> Any:
        """Returns the ``ExecutionTarget`` for *alias*, or raises ``TargetError``.

        ``owner_id`` scopes SSH connections per Telegram user. ``hostname`` /
        ``user`` are optional display hints; when absent the database is asked
        so ``TargetInfo`` stays populated for audit lines.
        """
        if alias is None or not str(alias).strip():
            raise TargetError("No server alias given. Use /servers to list your servers.")

        normalized = str(alias).strip()

        if self.is_local_alias(normalized):
            return self._resolve_local()

        if self._manager is None:
            raise TargetError(
                "No SSH manager configured; remote targets are unavailable in this build."
            )

        host = (hostname or "").strip()
        username = (user or "").strip()
        if not host or not username:
            record = self._lookup(owner_id, normalized)
            if record is not None:
                host = host or str(record.get("hostname") or "")
                username = username or str(record.get("user") or "")

        target = RemoteTarget(self._manager, owner_id, normalized, hostname=host, user=username)
        logger.debug("Resolved alias '%s' for user %s to a remote target.", normalized, owner_id)
        return target

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _resolve_local(self) -> LocalTarget:
        """Builds (once) and returns the local target, or refuses cleanly."""
        if not self._local_enabled:
            raise TargetError(
                "Local execution is disabled for this bot. Add the server over SSH instead, "
                "or start the bot with local_enabled=True."
            )
        if self._local is None:
            self._local = LocalTarget(allow=True)
        return self._local

    def _lookup(self, owner_id: int, alias: str) -> dict[str, Any] | None:
        """Reads server metadata, tolerating an unconfigured database.

        Host metadata is display-only enrichment; a database that has not been
        initialized yet must not make remote resolution impossible.
        """
        try:
            record = get_server(owner_id, alias)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Server lookup for '%s' failed (%s); continuing without metadata.", alias, exc)
            return None
        if record is None:
            return None
        if isinstance(record, dict):
            return record
        # Some layers return sqlite3.Row-like objects; read them structurally.
        try:
            return {str(key): record[key] for key in record.keys()}  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover - defensive
            return None


def resolve_target(
    owner_id: int,
    alias: str,
    manager: Any = None,
    *,
    local_enabled: bool = False,
) -> Any:
    """One-shot convenience wrapper around :class:`TargetResolver`.

    Exists so callers that resolve a single alias per process do not have to
    hold a resolver instance; anything repeated should construct one and reuse
    it (the LocalTarget is cached per resolver).
    """
    return TargetResolver(manager, local_enabled=local_enabled).resolve(owner_id, alias)


__all__ = ["LOCAL_ALIAS", "TargetError", "TargetInfo", "TargetKind", "TargetResolver", "resolve_target"]
