"""Run commands on the host that runs the bot, without an SSH round trip.

PURPOSE:
    Self-host support: an admin who installed the bot on their own VPS must be
    able to manage *that* server from Telegram. Without this module the bot
    would have to SSH into localhost with stored credentials, which is slower,
    needs a key or password on disk, and fails when sshd is down -- exactly the
    situations where you still want a shell.

OWNER:
    lane runtime (src/runtime/**, tests/test_runtime_*.py)

CONSUMES:
    src.metrics.METRICS for counters; src.ssh_manager.CommandOutputTooLarge so
    the local path raises the *same* exception (and message) as the SSH path.
    stdlib: asyncio, os, signal, subprocess, sys.

PROVIDES:
    LocalTarget -- ExecutionTarget over ``asyncio`` subprocesses, pid-first,
    byte-capped, timeout kills the whole process group.

INVARIANTS:
    * Disabled by default: ``LocalTarget()`` refuses to run anything until
      ``allow=True``. A mis-wired composition root must fail closed.
    * First yield is ``(str(pid), "pid")``; then ``"stdout"``/``"stderr"``
      chunks; a non-zero exit appends ``"\\n[exit status N]\\n"`` to stderr,
      and a timeout yields ``"Error: Command timed out."`` on stderr -- both
      copied verbatim from ``SSHManager.run_command``.
    * Output over ``max_bytes`` raises ``CommandOutputTooLarge`` with the SSH
      message ``Command produced more than N bytes of output.``
    * On timeout or early abandonment the whole process group is killed, so a
      shell and its children cannot outlive the command.
    * No ``/proc``, no unconditional ``signal.SIGKILL``: POSIX uses
      ``os.killpg``, Windows falls back to ``taskkill /T``.

EXTEND:
    New capabilities (pty, stdin feed) belong in a subclass or a sibling
    module; keep ``run`` streaming lazily and keep the parity invariants.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import AsyncIterator

from ..metrics import METRICS
from ..ssh_manager import CommandOutputTooLarge
from .target import (
    DEFAULT_MAX_BYTES,
    DEFAULT_TIMEOUT,
    TargetError,
    TargetInfo,
    TargetKind,
)

logger = logging.getLogger(__name__)

#: Alias the local host answers to. Defined here as well as in the resolver so
#: ``LocalTarget`` can describe itself without importing the resolver.
LOCAL_ALIAS = "self"

#: How long to wait for a killed process to actually disappear before giving up.
_KILL_GRACE_SECONDS = 5.0


def _taskkill_binary() -> str:
    """Returns an absolute ``taskkill.exe`` path when available.

    Windows kill-tree needs the real binary; ``SystemRoot`` is used first so we
    never depend on the inherited ``PATH`` of the bot process.
    """
    system_root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    candidate = os.path.join(system_root, "System32", "taskkill.exe")
    return candidate if os.path.exists(candidate) else "taskkill"


class LocalTarget:
    """Executes commands on the bot host itself, in a disposable process group.

    ``allow`` is a deliberate second key: constructing the object does not
    grant execution, so a config typo can never turn a remote alias into a root
    shell on the bot. ``shell`` overrides the shell binary used to interpret the
    command string (defaults to the platform shell).
    """

    def __init__(self, *, allow: bool = False, shell: str | None = None) -> None:
        self._allow = bool(allow)
        self._shell = shell
        self._closed = False
        self.info = TargetInfo(
            alias=LOCAL_ALIAS,
            hostname=_safe_hostname(),
            user=_safe_user(),
            kind=TargetKind.LOCAL,
            description="This server (no SSH)",
        )

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    @property
    def allow(self) -> bool:
        """Whether this instance is permitted to execute (read-only view)."""
        return self._allow

    @staticmethod
    def supported() -> bool:
        """Whether this host can host a local target at all.

        POSIX always can. On Windows, asyncio needs a Proactor loop to spawn
        subprocesses; a Selector loop would fail at call time with
        ``NotImplementedError``, so we check up front and refuse cleanly.
        """
        if os.name == "posix":
            return True
        if os.name != "nt":
            return False
        proactor = getattr(asyncio, "ProactorEventLoop", None)
        if proactor is None:  # pragma: no cover - non-CPython runtimes
            return False
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            return True
        return isinstance(running, proactor)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    async def run(
        self,
        command: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,  # noqa: ASYNC109 - `timeout` is fixed by CONTRACT.md
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> AsyncIterator[tuple[str, str]]:
        """Streams the command's output exactly like the SSH path does.

        Raises ``TargetError`` when the target is disabled or the host cannot
        spawn subprocesses; raises ``CommandOutputTooLarge`` past *max_bytes*;
        a timeout is reported as an stderr chunk, not an exception, so the
        handler code is identical for local and remote targets.
        """
        if self._closed:
            raise TargetError("Local target is closed; create a new target to run commands.")
        if not self._allow:
            raise TargetError(
                "Local execution is disabled: start the bot with local_enabled=True "
                "(TLA_ALLOW_LOCAL=1) to manage this server from Telegram."
            )
        if not LocalTarget.supported():
            raise TargetError(
                "Local execution is not supported on this host/runtime (Windows requires the Proactor event loop)."
            )

        if not command or not command.strip():
            raise TargetError("No command given.")

        started = time.monotonic()
        proc = await self._spawn(command)
        queue: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue()
        total = 0
        tasks: list[asyncio.Task[None]] = []

        async def pump(stream: asyncio.StreamReader | None, name: str) -> None:
            if stream is None:  # pragma: no cover - defensive
                await queue.put(None)
                return
            try:
                while True:
                    chunk = await stream.read(65536)
                    if not chunk:
                        break
                    await queue.put((chunk.decode("utf-8", "replace"), name))
            finally:
                await queue.put(None)

        try:
            # Parity with SSHManager.run_command: pid arrives before any output.
            yield str(proc.pid), "pid"

            tasks = [
                asyncio.create_task(pump(proc.stdout, "stdout")),
                asyncio.create_task(pump(proc.stderr, "stderr")),
            ]
            deadline = started + timeout
            finished = 0
            while finished < len(tasks):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                item = await asyncio.wait_for(queue.get(), timeout=remaining)
                if item is None:
                    finished += 1
                    continue
                chunk, name = item
                total += len(chunk)
                if total > max_bytes:
                    raise CommandOutputTooLarge(
                        f"Command produced more than {max_bytes} bytes of output."
                    )
                yield chunk, name

            status = await proc.wait()
            if status:
                yield f"\n[exit status {status}]\n", "stderr"
        except TimeoutError:
            METRICS.inc("runtime_local_timeouts_total")
            await self._terminate(proc)
            logger.warning("Local command timed out after %.1fs (pid %s).", timeout, proc.pid)
            yield "Error: Command timed out.", "stderr"
        except CommandOutputTooLarge:
            METRICS.inc("runtime_local_output_capped_total")
            await self._terminate(proc)
            raise
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            # Reached also when the caller abandons the generator early: never
            # leave a shell (and its children) behind.
            if proc.returncode is None:
                await self._terminate(proc)
            METRICS.inc("runtime_local_commands_total")

    async def close(self) -> None:
        """Marks the target unusable. Idempotent, because shutdown races."""
        self._closed = True

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    async def _spawn(self, command: str) -> asyncio.subprocess.Process:
        """Starts *command* in its own process group so it can be killed as a whole."""
        kwargs: dict[str, object] = {
            "stdin": asyncio.subprocess.DEVNULL,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
        }
        if os.name == "posix":
            kwargs["start_new_session"] = True  # setsid(): new process group + no tty
        else:
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

        # An explicit override always wins; otherwise a POSIX shell is preferred
        # so command syntax matches what the SSH path runs remotely. When none
        # is installed we fall back to the platform's native shell.
        shell = self._shell or _default_shell()
        if shell is not None:
            return await asyncio.create_subprocess_exec(
                shell, "-c", command, **kwargs  # type: ignore[arg-type]
            )
        return await asyncio.create_subprocess_shell(command, **kwargs)  # type: ignore[arg-type]

    async def _terminate(self, proc: asyncio.subprocess.Process) -> None:
        """Kills the process group and reaps it, tolerating an already-dead pid."""
        if proc.returncode is not None:
            self._close_pipes(proc)
            return
        self._kill_group(proc)
        try:
            await asyncio.wait_for(proc.wait(), timeout=_KILL_GRACE_SECONDS)
        except (TimeoutError, ProcessLookupError):
            logger.error("Local process %s did not exit after kill.", proc.pid)
        self._close_pipes(proc)

    def _close_pipes(self, proc: asyncio.subprocess.Process) -> None:
        """Closes the stdout/stderr transports after a kill.

        A killed child leaves its pipe transports open until garbage collection,
        which both leaks file handles in a long-lived bot and makes the Windows
        proactor complain during teardown. Closing them here keeps the kill path
        resource-clean.

        The transport handle is private API on purpose: asyncio exposes no public
        "close this child's pipes" call, and ``StreamReader`` alone cannot close
        the underlying socket.
        """
        transport = getattr(proc, "_transport", None)
        if transport is None:  # pragma: no cover - non-standard loop implementation
            return
        close = getattr(transport, "close", None)
        if callable(close):
            with contextlib.suppress(Exception):
                close()

    def _kill_group(self, proc: asyncio.subprocess.Process) -> None:
        """Sends a group-wide kill: ``killpg`` on POSIX, ``taskkill /T`` on Windows.

        Killing only the direct child would leave grandchildren (a shell's own
        children, a background daemon started by the command) running forever.
        """
        if os.name == "posix":
            killer = getattr(os, "killpg", None)
            sig = getattr(signal, "SIGKILL", None)
            if killer is not None and sig is not None:
                try:
                    killer(os.getpgid(proc.pid), sig)
                    return
                except (ProcessLookupError, PermissionError, OSError):
                    logger.debug("killpg failed for pid %s; falling back to kill().", proc.pid)
        elif os.name == "nt":
            try:
                subprocess.run(
                    [_taskkill_binary(), "/F", "/T", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=_KILL_GRACE_SECONDS,
                    check=False,
                )
                return
            except (OSError, subprocess.SubprocessError):
                logger.debug("taskkill failed for pid %s; falling back to kill().", proc.pid)

        try:
            proc.kill()
        except ProcessLookupError:  # pragma: no cover - already gone
            pass


def _default_shell() -> str | None:
    """Returns the shell used to interpret commands, or None for the OS default.

    Commands come from the same users and docs as the SSH ones, so they must be
    interpreted with POSIX syntax (``&&``, ``>&2``, ``$!``). On Linux that is the
    default already; on Windows ``asyncio`` would otherwise hand them to
    ``cmd.exe``, which would mangle them into a syntax error or -- worse -- a
    silently different command. A discoverable ``sh``/``bash`` is therefore
    preferred, and only when none exists do we return None and let the platform
    shell run.
    """
    if os.name == "posix":
        return "/bin/sh"
    for candidate in ("bash", "sh"):
        found = shutil.which(candidate)
        if found:
            return found
    return None


def _safe_hostname() -> str:
    """Best-effort hostname for display; empty string instead of raising."""
    try:
        return os.uname().nodename  # type: ignore[attr-defined]
    except AttributeError:
        return os.environ.get("COMPUTERNAME", "") or ""


def _safe_user() -> str:
    """Best-effort current user for display; never raises (no /proc dependency)."""
    try:
        import getpass

        return getpass.getuser()
    except Exception:  # pragma: no cover - exotic service environments
        return os.environ.get("USER") or os.environ.get("USERNAME") or ""


def _platform_summary() -> str:
    """Short platform tag used in log/audit lines, not in user messages."""
    return sys.platform
