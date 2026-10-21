"""Persistent local shells, so the terminal feature works on the bot's host.

SSH mode keeps a channel per user; the local target needs the same illusion.
Each session is one long-lived ``bash`` whose stdin/stdout are piped, and every
command is terminated by a random marker echoed by the shell itself::

    <command>
    printf '__TLA_END_<token>:%s\n' "$?"

Reading until the marker gives the exact output of that command even when it
prints nothing, and the marker's suffix is the real exit status.  The token is
random per command so command output can never forge a boundary.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import uuid
from dataclasses import dataclass, field
from typing import Dict

logger = logging.getLogger(__name__)

MARKER_PREFIX = "__TLA_END_"
DEFAULT_SHELL_TIMEOUT = float(os.environ.get("TLA_LOCAL_SHELL_TIMEOUT", "60"))


def _shell_argv() -> list[str]:
    bash = shutil.which("bash")
    if bash:
        return [bash, "--noprofile", "--norc", "-s"]
    return ["/bin/sh", "-s"]


@dataclass
class LocalShellSession:
    """One persistent shell process owned by a Telegram user."""

    owner_id: int
    process: asyncio.subprocess.Process
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _closed: bool = False

    async def run(self, command: str, *, timeout: float = DEFAULT_SHELL_TIMEOUT) -> str:
        """Runs *command* in the session and returns its combined output."""
        if self._closed:
            raise RuntimeError("Shell session is closed.")
        token = uuid.uuid4().hex[:16]
        marker = f"{MARKER_PREFIX}{token}:"
        payload = f"{command}\nprintf '{marker}%s\\n' \"$?\"\n"
        async with self._lock:
            assert self.process.stdin is not None
            assert self.process.stdout is not None
            try:
                self.process.stdin.write(payload.encode("utf-8"))
                await self.process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                self._closed = True
                raise RuntimeError("Shell session terminated.") from exc

            buffer = bytearray()
            deadline = asyncio.get_running_loop().time() + timeout
            try:
                while True:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise asyncio.TimeoutError
                    line = await asyncio.wait_for(
                        self.process.stdout.readline(), timeout=remaining
                    )
                    if not line:
                        self._closed = True
                        raise RuntimeError("Shell session ended unexpectedly.")
                    text = line.decode("utf-8", "replace")
                    if text.startswith(marker):
                        break
                    buffer.extend(line)
            except asyncio.TimeoutError:
                # The timed-out command keeps running and would emit its marker
                # later, mis-attributing output to the next command; tear the
                # session down instead of reusing it.
                await self.close()
                raise
            return buffer.decode("utf-8", "replace")

    async def close(self) -> None:
        self._closed = True
        if self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.process.terminate()
            with contextlib.suppress(asyncio.TimeoutError, ProcessLookupError):
                await asyncio.wait_for(self.process.wait(), timeout=2)
        if self.process.returncode is None:  # pragma: no cover - stubborn child
            with contextlib.suppress(ProcessLookupError):
                self.process.kill()


class LocalShellPool:
    """Keeps at most one local shell per user; drops it on disconnect."""

    def __init__(self, *, cwd: str | None = None) -> None:
        self._sessions: Dict[int, LocalShellSession] = {}
        self._cwd = cwd

    async def start(self, owner_id: int) -> LocalShellSession:
        existing = self._sessions.get(owner_id)
        if existing is not None and not existing._closed:
            return existing
        process = await asyncio.create_subprocess_exec(
            *_shell_argv(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=self._cwd,
            start_new_session=True,
        )
        session = LocalShellSession(owner_id=owner_id, process=process)
        self._sessions[owner_id] = session
        logger.info("local shell started for user %s (pid=%s)", owner_id, process.pid)
        return session

    async def run(self, owner_id: int, command: str) -> str:
        session = await self.start(owner_id)
        return await session.run(command)

    def active(self, owner_id: int) -> bool:
        session = self._sessions.get(owner_id)
        return session is not None and not session._closed

    def active_count(self) -> int:
        return sum(1 for session in self._sessions.values() if not session._closed)


    async def close(self, owner_id: int) -> None:
        session = self._sessions.pop(owner_id, None)
        if session is not None:
            await session.close()
            logger.info("local shell closed for user %s", owner_id)

    async def close_all(self) -> None:
        for owner_id in list(self._sessions):
            await self.close(owner_id)
