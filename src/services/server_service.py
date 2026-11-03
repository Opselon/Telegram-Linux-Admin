"""Server management service — the shared CRUD/connection core.

All adapters (web API, Bale, and future Telegram calls) go through here so
ownership + permission checks cannot be bypassed.  Wraps the existing
``src.database`` functions; the Telegram bot continues to use those directly
for its legacy paths, which share the same underlying tables and encryption.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from .. import database
from ..audit import audit as _audit


def audit_record(event: str, **fields: object) -> None:
    """Audit one security-relevant event (shared helper for all services)."""
    _audit.record(event, **fields)
from ..metrics import METRICS
from ..runtime.resolver import TargetResolver
from ..runtime.target import TargetError
from ..ssh_manager import SSHManager
from . import store
from .permissions import Actor

SECRET_FIELDS = ("password", "key_path")


@dataclass
class ServerService:
    """Ownership-scoped server CRUD + connection operations."""

    manager: SSHManager
    resolver: TargetResolver | None = None

    # -- helpers -------------------------------------------------------------
    def _resolve_server(self, actor: Actor, server_id: int) -> dict[str, Any]:
        """Loads a server by id, enforcing ownership (404 semantics for strangers)."""
        conn = database.get_db_connection()
        row = conn.execute("SELECT * FROM servers WHERE id = ?", (server_id,)).fetchone()
        if row is None:
            raise LookupError("server not found")
        server = dict(row)
        is_admin = actor.has("security.read") and actor.role in ("owner", "admin")
        owner_account_id = server.get("owner_account_id")
        if owner_account_id is not None:
            owned = int(owner_account_id) == actor.account_id
        else:
            owned = server["owner_id"] == actor.owner_id
        if not owned and not is_admin:
            audit_record(
                "security.denied",
                action="server.read",
                server_id=server_id,
                account_id=actor.account_id,
            )
            raise LookupError("server not found")  # never leak existence
        server["password"] = database._decrypt_value(server.get("password"))  # noqa: SLF001
        server["key_path"] = database._decrypt_value(server.get("key_path"))  # noqa: SLF001
        return server

    @staticmethod
    def _alias_of(server: dict[str, Any]) -> str:
        return str(server["alias"])

    # -- CRUD ----------------------------------------------------------------
    def list_servers(self, actor: Actor) -> list[dict[str, Any]]:
        actor.require("servers.read")
        servers = database.get_all_servers(actor.owner_id)
        if actor.role in ("owner", "admin") and actor.has("security.read"):
            servers = database.get_all_servers(None)
        for server in servers:
            for field in SECRET_FIELDS:
                server.pop(field, None)  # never leaves the service
            server["owner"] = bool(
                server.get("owner_account_id") is not None
                and int(server["owner_account_id"]) == actor.account_id
            ) or server.get("owner_id") == actor.owner_id
        return servers

    def get_server(self, actor: Actor, server_id: int) -> dict[str, Any]:
        actor.require("servers.read")
        server = self._resolve_server(actor, server_id)
        for field in SECRET_FIELDS:
            server.pop(field, None)
        owner_account_id = server.get("owner_account_id")
        server["owner"] = bool(
            (owner_account_id is not None and int(owner_account_id) == actor.account_id)
            or server.get("owner_id") == actor.owner_id
        )
        return server

    def create_server(
        self,
        actor: Actor,
        *,
        alias: str,
        hostname: str,
        user: str,
        password: str | None = None,
        key_path: str | None = None,
        port: int = 22,
    ) -> dict[str, Any]:
        actor.require("servers.create")
        from ..validators import validate_alias, validate_hostname, validate_username

        validate_alias(alias)
        validate_hostname(hostname)
        validate_username(user)
        if not (1 <= int(port) <= 65535):
            raise ValueError("port must be between 1 and 65535")
        database.add_server(
            actor.owner_id, alias, hostname, user,
            password=password, key_path=key_path, port=int(port),
        )
        # Link the new row to the canonical account identity.
        account = store.get_account(actor.account_id)
        if account is not None:
            with database.transaction() as conn:
                conn.execute(
                    """
                    UPDATE servers SET owner_account_id = ?
                    WHERE owner_id = ? AND alias = ? AND owner_account_id IS NULL
                    """,
                    (account.id, actor.owner_id, alias),
                )
        audit_record("server.created", alias=alias, account_id=actor.account_id, kind=actor.kind)
        server = database.get_server(actor.owner_id, alias)
        assert server is not None
        for field in SECRET_FIELDS:
            server.pop(field, None)
        server["owner"] = True
        return server

    def update_server(self, actor: Actor, server_id: int, changes: dict[str, Any]) -> dict[str, Any]:
        actor.require("servers.update")
        server = self._resolve_server(actor, server_id)
        alias = self._alias_of(server)
        allowed = {"hostname", "user", "password", "key_path", "port"}
        payload = {k: v for k, v in changes.items() if k in allowed and v is not None}
        if "port" in payload:
            if not (1 <= int(payload["port"]) <= 65535):
                raise ValueError("port must be between 1 and 65535")
        if not payload:
            raise ValueError("no updatable fields provided")
        database.update_server(actor.owner_id, alias, **payload)
        audit_record("server.updated", alias=alias, fields=sorted(payload), account_id=actor.account_id)
        updated = database.get_server(actor.owner_id, alias)
        assert updated is not None
        for field in SECRET_FIELDS:
            updated.pop(field, None)
        return updated

    def rename_server(self, actor: Actor, server_id: int, new_alias: str) -> dict[str, Any]:
        """Renames in place (the id stays stable; the alias is a unique column)."""
        actor.require("servers.update")
        from ..validators import validate_alias

        validate_alias(new_alias)
        server = self._resolve_server(actor, server_id)
        old_alias = self._alias_of(server)
        try:
            with database.transaction() as conn:
                cursor = conn.execute(
                    "UPDATE servers SET alias = ? WHERE id = ?",
                    (new_alias, server_id),
                )
                if cursor.rowcount == 0:
                    raise LookupError("server not found")
        except sqlite3.IntegrityError as exc:
            raise ValueError(
                f"Server with alias '{new_alias}' already exists."
            ) from exc
        audit_record("server.renamed", from_alias=old_alias, to_alias=new_alias,
                     account_id=actor.account_id)
        updated = database.get_server(actor.owner_id, new_alias)
        assert updated is not None
        for field in SECRET_FIELDS:
            updated.pop(field, None)
        updated["owner"] = True
        return updated

    def delete_server(self, actor: Actor, server_id: int) -> None:
        actor.require("servers.delete")
        server = self._resolve_server(actor, server_id)
        alias = self._alias_of(server)
        database.remove_server(actor.owner_id, alias)
        audit_record("server.deleted", alias=alias, account_id=actor.account_id)

    # -- connection ops --------------------------------------------------------
    async def test_connection(self, actor: Actor, server_id: int) -> dict[str, Any]:
        actor.require("servers.connect")
        server = self._resolve_server(actor, server_id)
        alias = self._alias_of(server)
        audit_record("server.test", alias=alias, account_id=actor.account_id, kind=actor.kind)
        try:
            output = await self.manager.execute(actor.owner_id, alias, "echo ok", timeout=20)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": output[2] == 0, "exit_status": output[2]}

    async def connect(self, actor: Actor, server_id: int) -> dict[str, Any]:
        actor.require("servers.connect")
        server = self._resolve_server(actor, server_id)
        alias = self._alias_of(server)
        try:
            await self.manager.execute(actor.owner_id, alias, "true", timeout=20)
        except Exception as exc:
            audit_record("server.connect.failed", alias=alias, account_id=actor.account_id)
            return {"ok": False, "error": str(exc)}
        audit_record("server.connected", alias=alias, account_id=actor.account_id)
        return {"ok": True}

    async def disconnect(self, actor: Actor, server_id: int) -> dict[str, Any]:
        actor.require("servers.connect")
        server = self._resolve_server(actor, server_id)
        alias = self._alias_of(server)
        await self.manager.disconnect(actor.owner_id, alias)
        audit_record("server.disconnected", alias=alias, account_id=actor.account_id)
        return {"ok": True}

    def host_key_status(self, actor: Actor, server_id: int) -> dict[str, Any]:
        actor.require("servers.read")
        server = self._resolve_server(actor, server_id)
        fingerprint = database.get_server_host_key(actor.owner_id, self._alias_of(server))
        return {
            "pinned": fingerprint is not None,
            "fingerprint": fingerprint,
            "policy": server.get("host_key_policy", "tofu"),
        }

    # -- execution helper used by other services ------------------------------
    async def run_on_server(
        self, actor: Actor, server_id: int, command: str, *, timeout: float = 60.0
    ) -> tuple[str, str, int]:
        """Runs *command* on the owned server; returns (stdout, stderr, exit)."""
        server = self._resolve_server(actor, server_id)
        return await self.manager.execute(
            actor.owner_id, self._alias_of(server), command, timeout=timeout
        )


class ServerNotFoundError(LookupError):
    """Alias kept for API-layer mapping (maps to HTTP 404)."""
