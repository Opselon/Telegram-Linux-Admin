"""Routing command manager: one API for SSH and local execution.

Historical handlers call ``ssh_manager.<method>(user_id, alias, ...)`` in ~25
places.  Rewriting all of them in one release would be a big-bang change, so
this adapter implements the same surface and dispatches on the target kind.
New code uses :class:`src.application.commands.CommandService` directly; the
adapter is the strangler bridge and enforces the same access policy.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
from typing import Any, AsyncIterator, Callable

from src.domain.errors import TransportUnavailable
from src.domain.policies import TargetAccessPolicy, is_reserved_alias
from src.domain.targets import ServerTarget, local_target
from src.infrastructure.local_shell import LocalShellPool
from src.infrastructure.local_transport import LocalTransport
from src.ssh_manager import SSHManager

logger = logging.getLogger(__name__)


def _process_exists(pid: int) -> bool:
    """True while *pid* is still present in the process table."""
    return os.path.exists(f"/proc/{pid}")


class RoutingCommandManager:
    """Drop-in replacement for :class:`SSHManager` that understands targets."""

    def __init__(
        self,
        ssh: SSHManager,
        *,
        local: LocalTransport | None = None,
        shell_pool: LocalShellPool | None = None,
        policy: TargetAccessPolicy | None = None,
        is_admin: Callable[[int | None], bool] | None = None,
    ) -> None:
        self._ssh = ssh
        self._local = local or LocalTransport()
        self._shells = shell_pool or LocalShellPool()
        self._policy = policy or TargetAccessPolicy()
        self._is_admin = is_admin or (lambda uid: False)

    # -- helpers -----------------------------------------------------------
    def _local_target(self, owner_id: int) -> ServerTarget:
        """Authorizes and returns the self-host target (fail-closed)."""
        target = local_target(owner_id)
        self._policy.ensure_can_use(target, is_admin=self._is_admin(owner_id))
        return target

    def is_local_alias(self, alias: str) -> bool:
        return is_reserved_alias(alias)

    # -- command execution -------------------------------------------------
    async def run_command(
        self,
        owner_id: int,
        alias: str,
        command: str,
        timeout: float | None = None,
        max_bytes: int | None = None,
    ) -> AsyncIterator[tuple[str, str]]:
        """Streams output from whichever transport owns *alias*."""
        if self.is_local_alias(alias):
            target = self._local_target(owner_id)
            async for item in self._local.run(target, command):
                yield item
            return

        kwargs: dict[str, Any] = {}
        if timeout is not None:
            kwargs["timeout"] = timeout
        if max_bytes is not None:
            kwargs["max_bytes"] = max_bytes
        async for item in self._ssh.run_command(owner_id, alias, command, **kwargs):
            yield item

    async def execute(self, owner_id: int, alias: str, command: str) -> tuple[str, str, int]:
        out: list[str] = []
        err: list[str] = []
        code = 0
        async for chunk, stream in self.run_command(owner_id, alias, command):
            if stream == "stdout":
                out.append(chunk)
            elif stream == "stderr":
                err.append(chunk)
            elif stream == "exit":
                try:
                    code = int(chunk)
                except ValueError:
                    code = 0
        return "".join(out), "".join(err), code

    async def kill_process(self, owner_id: int, alias: str, pid: int) -> None:
        """Terminates a process (and its group) on either target kind.

        Locally there is no remote shell to relay the signal, so the same
        escalation the operator expects from ``kill`` is applied here: TERM to
        the process group, then KILL after a short grace period.
        """
        if not self.is_local_alias(alias):
            await self._ssh.kill_process(owner_id, alias, pid)
            return

        self._local_target(owner_id)
        try:
            os.killpg(os.getpgid(pid), 15)
        except (ProcessLookupError, PermissionError):
            os.kill(pid, 15)

        for _ in range(20):
            if not _process_exists(pid):
                return
            await asyncio.sleep(0.1)

        try:
            os.killpg(os.getpgid(pid), 9)
        except (ProcessLookupError, PermissionError):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, 9)

    # -- shell -------------------------------------------------------------
    async def start_shell_session(self, owner_id: int, alias: str) -> None:
        if self.is_local_alias(alias):
            self._local_target(owner_id)
            await self._shells.start(owner_id)
            return
        await self._ssh.start_shell_session(owner_id, alias)

    async def run_command_in_shell(self, owner_id: int, alias: str, command: str) -> str:
        if self.is_local_alias(alias):
            self._local_target(owner_id)
            return await self._shells.run(owner_id, command)
        return await self._ssh.run_command_in_shell(owner_id, alias, command)

    async def close_shell(self, owner_id: int) -> None:
        await self._shells.close(owner_id)

    # -- files ------------------------------------------------------------
    async def download_file(
        self, owner_id: int, alias: str, remote_path: str, local_path: str
    ) -> None:
        if self.is_local_alias(alias):
            self._local_target(owner_id)
            if not os.path.isfile(remote_path):
                raise FileNotFoundError(remote_path)
            shutil.copyfile(remote_path, local_path)
            return
        await self._ssh.download_file(owner_id, alias, remote_path, local_path)

    async def upload_file(
        self, owner_id: int, alias: str, local_path: str, remote_path: str
    ) -> None:
        if self.is_local_alias(alias):
            self._local_target(owner_id)
            if not os.path.isfile(local_path):
                raise FileNotFoundError(local_path)
            shutil.copyfile(local_path, remote_path)
            return
        await self._ssh.upload_file(owner_id, alias, local_path, remote_path)

    # -- lifecycle ---------------------------------------------------------
    async def disconnect(self, owner_id: int, alias: str) -> None:
        if self.is_local_alias(alias):
            await self._shells.close(owner_id)
            return
        await self._ssh.disconnect(owner_id, alias)

    async def close_all_connections(self) -> None:
        await self._shells.close_all()
        await self._ssh.close_all_connections()

    async def pool_stats(self) -> dict[str, int]:
        stats = await self._ssh.pool_stats()
        stats["local_shells"] = self._shells.active_count()
        return stats


def ensure_supported(alias: str) -> None:
    """Guard used by callers that must never receive an unknown target."""
    if not alias:
        raise TransportUnavailable(alias, "empty alias")
