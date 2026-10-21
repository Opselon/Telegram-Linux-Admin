"""Local transport: runs commands on the bot's own host.

This is the adapter behind the *self-managed* target.  It executes through a
login shell in the bot process' own identity, streams stdout/stderr the same
way the SSH adapter does, and enforces the same timeout and output cap so the
UI behaves identically for both target kinds.

Access control is **not** implemented here: the domain policy
(:class:`src.domain.policies.TargetAccessPolicy`) decides whether a principal
may use the local target at all, and every caller goes through the application
layer, which consults that policy first.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import time
from typing import AsyncIterator, Sequence

from src.domain.targets import ServerTarget

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = float(os.environ.get("TLA_LOCAL_COMMAND_TIMEOUT", "60"))
DEFAULT_MAX_BYTES = int(os.environ.get("TLA_LOCAL_MAX_BYTES", str(2 * 1024 * 1024)))
#: Hard cap on concurrently running local commands (one user could otherwise
#: fork-bomb the host through a loop of callbacks).
MAX_CONCURRENT = int(os.environ.get("TLA_LOCAL_MAX_CONCURRENCY", "4"))


def shell_argv(command: str) -> list[str]:
    """Returns argv for a login shell that runs *command* verbatim.

    argv (not a single string) means the command is passed as one argument and
    never re-parsed by a second shell, so quotes and non-ASCII text survive.
    """
    bash = shutil.which("bash")
    if bash:
        return [bash, "-lc", command]
    return ["/bin/sh", "-lc", command]


class LocalTransport:
    """Implements :class:`src.domain.ports.CommandTransport` for the bot host."""

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        max_bytes: int = DEFAULT_MAX_BYTES,
        concurrency: int = MAX_CONCURRENT,
        cwd: str | None = None,
    ) -> None:
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._semaphore = asyncio.Semaphore(concurrency)
        self._cwd = cwd

    # -- transport contract ------------------------------------------------
    async def run(
        self, target: ServerTarget, command: str
    ) -> AsyncIterator[tuple[str, str]]:
        """Streams ``(chunk, stream)`` tuples for a locally executed command."""
        async with self._semaphore:
            async for item in self._stream(command):
                yield item

    async def disconnect(self, target: ServerTarget) -> None:
        """No pooled resources exist locally; kept for interface parity."""
        return None

    # -- helpers -----------------------------------------------------------
    async def execute(self, command: str, *, timeout: float | None = None) -> tuple[str, str, int]:
        """Runs *command* to completion and returns ``(stdout, stderr, code)``."""
        out: list[str] = []
        err: list[str] = []
        code = 0
        async for chunk, stream in self._stream(command, timeout=timeout):
            if stream == "stdout":
                out.append(chunk)
            elif stream == "stderr":
                err.append(chunk)
            elif stream == "exit":
                with contextlib.suppress(ValueError):
                    code = int(chunk)
        return "".join(out), "".join(err), code

    async def _stream(
        self, command: str, *, timeout: float | None = None
    ) -> AsyncIterator[tuple[str, str]]:
        effective_timeout = self._timeout if timeout is None else timeout
        argv: Sequence[str] = shell_argv(command)
        queue: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue()
        total = 0
        started = time.monotonic()

        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
                cwd=self._cwd,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:  # pragma: no cover - host dependent
            logger.error("Local command spawn failed: %s", exc)
            raise RuntimeError(f"Unable to start a local shell: {exc}") from exc

        async def pump(stream: asyncio.StreamReader | None, name: str) -> None:
            try:
                if stream is None:
                    return
                while True:
                    chunk = await stream.read(4096)
                    if not chunk:
                        break
                    await queue.put((chunk.decode("utf-8", "replace"), name))
            finally:
                await queue.put(None)

        tasks = [
            asyncio.create_task(pump(process.stdout, "stdout")),
            asyncio.create_task(pump(process.stderr, "stderr")),
        ]
        finished = 0
        deadline = started + effective_timeout
        try:
            yield str(process.pid), "pid"
            while finished < len(tasks):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                item = await asyncio.wait_for(queue.get(), timeout=remaining)
                if item is None:
                    finished += 1
                    continue
                chunk, name = item
                total += len(chunk)
                if total > self._max_bytes:
                    await self._terminate(process)
                    raise RuntimeError(
                        f"Command produced more than {self._max_bytes} bytes of output."
                    )
                yield chunk, name

            returncode = await process.wait()
            yield str(returncode), "exit"
            if returncode:
                yield f"\n[exit status {returncode}]\n", "stderr"
        except asyncio.TimeoutError:
            await self._terminate(process)
            yield "Error: Command timed out.", "stderr"
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if process.returncode is None:
                await self._terminate(process)

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        """Kills the whole process group, then reaps it."""
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(process.pid), 15)
        with contextlib.suppress(asyncio.TimeoutError, ProcessLookupError):
            await asyncio.wait_for(process.wait(), timeout=2)
        if process.returncode is None:  # pragma: no cover - stubborn child
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(os.getpgid(process.pid), 9)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=2)
