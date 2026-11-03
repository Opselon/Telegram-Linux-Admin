"""Secure remote file service with server-side path policy.

The Telegram bot's file manager reaches any path on the target host; that is
acceptable for a single-admin bot, but a web surface serving multiple roles
needs a policy layer.  This service enforces:

* no path traversal (``..`` components, control characters, shell metachars),
* optional per-server chroot list (``files_allowed_roots`` in the server row's
  ``extra``/config), defaulting to ``/`` for admin/owner roles only — operator
  and read-only roles must operate inside allowed roots when configured,
* upload size caps and generated temp names (original filename never becomes
  a local path),
* audit on every operation.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from ..audit import audit as _audit


def audit_record(event: str, **fields: object) -> None:
    """Audit one security-relevant event (shared helper for all services)."""
    _audit.record(event, **fields)
from .permissions import Actor
from .server_service import ServerService

_UNSAFE = set("`\r\n\t\x00")
_MAX_LIST = 500
DEFAULT_UPLOAD_LIMIT = 50 * 1024 * 1024


class FilePolicyError(ValueError):
    """The requested path or operation violates the file-access policy."""


def validate_remote_path(path: str) -> str:
    """Normalizes and validates an absolute POSIX path; returns the clean form."""
    if not path or not path.startswith("/"):
        raise FilePolicyError("path must be absolute")
    if any(ch in path for ch in _UNSAFE):
        raise FilePolicyError("path contains control characters")
    pure = PurePosixPath(path)
    if ".." in pure.parts:
        raise FilePolicyError("path traversal is not permitted")
    cleaned = "/" + "/".join(p for p in pure.parts[1:] if p not in ("", "."))
    if len(cleaned) > 1024:
        raise FilePolicyError("path too long")
    return cleaned or "/"


def validate_filename(name: str) -> str:
    name = (name or "").strip()
    if not name or "/" in name or "\\" in name or name in {".", ".."}:
        raise FilePolicyError("invalid filename")
    if any(ch in name for ch in _UNSAFE | {"$;&|<>*?\"'"}):
        raise FilePolicyError("filename contains unsafe characters")
    if len(name) > 255:
        raise FilePolicyError("filename too long")
    return name


@dataclass
class FileService:
    servers: ServerService
    upload_limit: int = DEFAULT_UPLOAD_LIMIT

    # -- policy ----------------------------------------------------------
    def _check_path(self, actor: Actor, path: str) -> str:
        path = validate_remote_path(path)
        if actor.role in ("operator", "read_only"):
            roots = self._allowed_roots()
            if roots and not any(path == r or path.startswith(r.rstrip("/") + "/") for r in roots):
                raise FilePolicyError("path is outside the allowed roots")
        return path

    @staticmethod
    def _allowed_roots() -> list[str]:
        """Configured chroots (``TLA_FILES_ALLOWED_ROOTS=/srv,/home``)."""
        import os
        raw = os.environ.get("TLA_FILES_ALLOWED_ROOTS", "").strip()
        if not raw:
            return []
        roots = [validate_remote_path(r) for r in raw.split(",") if r.strip()]
        return roots

    # -- operations --------------------------------------------------------
    async def list_dir(self, actor: Actor, server_id: int, path: str) -> dict[str, Any]:
        actor.require("files.read")
        path = self._check_path(actor, path)
        stdout, stderr, _exit = await self.servers.run_on_server(
            actor, server_id,
            f"ls -la --time-style=+%s {shlex.quote(path)} 2>&1 | head -n {_MAX_LIST + 5}",
            timeout=30,
        )
        if "No such file or directory" in (stdout + stderr):
            raise FilePolicyError("directory not found")
        entries: list[dict[str, Any]] = []
        for line in stdout.splitlines()[1:]:
            parts = line.split(None, 8)
            if len(parts) < 9:
                continue
            perms, links, owner, group, size, mtime = parts[0], parts[1], parts[2], parts[3], parts[4], parts[5]
            name = parts[8]
            if name in (".", ".."):
                continue
            entries.append({
                "name": name,
                "type": "dir" if perms.startswith("d") else ("link" if perms.startswith("l") else "file"),
                "size": int(size) if size.isdigit() else 0,
                "mtime": int(mtime) if mtime.isdigit() else 0,
                "owner": f"{owner}:{group}",
                "perms": perms,
            })
        audit_record("file.listed", path=path, account_id=actor.account_id)
        return {"path": path, "entries": entries[:_MAX_LIST]}

    async def read_file(self, actor: Actor, server_id: int, path: str, *, max_bytes: int = 256 * 1024) -> str:
        actor.require("files.read")
        path = self._check_path(actor, path)
        stdout, _stderr, _exit = await self.servers.run_on_server(
            actor, server_id,
            f"head -c {max_bytes} {shlex.quote(path)}",
            timeout=30,
        )
        audit_record("file.read", path=path, account_id=actor.account_id)
        return stdout

    async def download_file(self, actor: Actor, server_id: int, path: str, local_path: str) -> None:
        actor.require("files.download")
        path = self._check_path(actor, path)
        await self.manager_download(actor, server_id, path, local_path)
        audit_record("file.downloaded", path=path, account_id=actor.account_id)

    async def manager_download(self, actor: Actor, server_id: int, remote: str, local: str) -> None:
        remote = self._check_path(actor, remote)
        server = self.servers.get_server(actor, server_id)
        await self.servers.manager.download_file(
            actor.owner_id, str(server["alias"]), remote, local
        )

    async def manager_upload(self, actor: Actor, server_id: int, local: str, remote_dir: str,
                             remote_name: str) -> str:
        actor.require("files.upload")
        remote_dir = self._check_path(actor, remote_dir)
        remote_name = validate_filename(remote_name)
        import os
        if os.path.getsize(local) > self.upload_limit:
            raise FilePolicyError("upload exceeds the size limit")
        remote_path = remote_dir.rstrip("/") + "/" + remote_name
        server = self.servers.get_server(actor, server_id)
        await self.servers.manager.upload_file(
            actor.owner_id, str(server["alias"]), local, remote_path
        )
        audit_record("file.uploaded", path=remote_path, account_id=actor.account_id)
        return remote_path

    async def delete_file(self, actor: Actor, server_id: int, path: str) -> None:
        actor.require("files.delete")
        path = self._check_path(actor, path)
        if path in ("/", "/etc", "/usr", "/var", "/bin", "/sbin", "/boot", "/dev", "/proc", "/sys"):
            raise FilePolicyError("refusing to delete a system path")
        await self.servers.run_on_server(
            actor, server_id, f"rm -rf -- {shlex.quote(path)}", timeout=30,
        )
        audit_record("file.deleted", path=path, account_id=actor.account_id, kind=actor.kind)

    async def mkdir(self, actor: Actor, server_id: int, path: str) -> None:
        actor.require("files.write")
        path = self._check_path(actor, path)
        await self.servers.run_on_server(
            actor, server_id, f"mkdir -p -- {shlex.quote(path)}", timeout=30,
        )
        audit_record("file.mkdir", path=path, account_id=actor.account_id)

    async def rename(self, actor: Actor, server_id: int, path: str, new_name: str) -> None:
        actor.require("files.write")
        path = self._check_path(actor, path)
        new_name = validate_filename(new_name)
        target = str(PurePosixPath(path).parent / new_name)
        await self.servers.run_on_server(
            actor, server_id,
            f"mv -- {shlex.quote(path)} {shlex.quote(target)}", timeout=30,
        )
        audit_record("file.renamed", path=path, target=target, account_id=actor.account_id)
