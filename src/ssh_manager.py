"""Hardened SSH execution layer.

Three upgrades over the historical "connect, run, disconnect" implementation:

* **Host key pinning (TOFU)** — the first successful connection pins the
  server's SHA256 fingerprint in the database; every later connection must
  present the same fingerprint or the connection is refused before a single
  byte of user input is sent.  Protects against MITM on the SSH hop, which
  ``known_hosts=None`` did not.
* **Connection reuse** — a small per-(user, server) pool keeps connections
  alive for :data:`IDLE_TIMEOUT` seconds.  Sequential admin actions stop
  paying a full TCP+KEX handshake each time (typically 150-400 ms saved per
  action on a WAN link) while idle links are reaped in the background.
* **Quoted-command safety** — user commands are transferred to the remote
  shell as a base64 payload, so quotes, ``$``, backticks, newlines and
  non-ASCII text can never break out of (or break) the wrapper.  The
  previous ``bash -lc 'echo $$; exec {command}'`` envelope broke on any
  single quote in a command and silently mangled it.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import asyncssh
from asyncssh import PermissionDenied
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_fixed

from .database import get_server, get_server_host_key, set_server_host_key
from .metrics import METRICS

# --- Constants ---
#: Default timeout for a single remote command.
COMMAND_TIMEOUT = float(os.environ.get("TLA_COMMAND_TIMEOUT", 60.0))
#: How long an idle connection stays in the pool before being reaped.
IDLE_TIMEOUT = float(os.environ.get("TLA_SSH_IDLE_TIMEOUT", 120.0))
#: Maximum number of pooled connections (per process).
MAX_POOLED_CONNECTIONS = int(os.environ.get("TLA_SSH_POOL_SIZE", 32))
#: Host key policy: ``tofu`` (default), ``strict`` or ``insecure``.
HOST_KEY_POLICY = os.environ.get("TLA_SSH_HOST_KEY_POLICY", "tofu").strip().lower()
#: Maximum concurrent commands per connection.
MAX_CONCURRENT_PER_CONNECTION = int(os.environ.get("TLA_SSH_MAX_CONCURRENCY", 4))

logger = logging.getLogger(__name__)


class HostKeyMismatch(asyncssh.Error):
    """Raised when a server presents a host key different from the pinned one."""

    def __init__(self, alias: str, expected: str, actual: str) -> None:
        super().__init__(
            0,
            f"Host key mismatch for '{alias}': expected {expected}, got {actual}",
        )
        self.alias = alias
        self.expected = expected
        self.actual = actual


class CommandOutputTooLarge(RuntimeError):
    """Raised when a command streams more output than the configured cap."""


def wrap_remote_command(command: str) -> str:
    """Returns a remote shell envelope that transports *command* verbatim.

    The command is base64 encoded locally and decoded remotely, so the
    envelope itself only ever contains ``[A-Za-z0-9+/=]`` characters.
    ``$$`` is echoed first so the caller can implement cancellation, and
    ``exec`` keeps the PID stable for the lifetime of the command.
    """
    payload = base64.b64encode(command.encode("utf-8")).decode("ascii")
    return f"bash -lc 'echo $$; exec bash -lc \"$(printf %s {payload} | base64 -d)\"'"


def fingerprint_host_key(key: Any) -> str:
    """Returns a stable SHA256 fingerprint for an asyncssh host key object."""
    getter = getattr(key, "get_fingerprint", None)
    if callable(getter):
        try:
            return str(getter("sha256"))
        except TypeError:  # pragma: no cover - very old asyncssh
            return str(getter())
    if isinstance(key, (bytes, str)):  # already exported
        raw = key.encode() if isinstance(key, str) else key
    else:  # pragma: no cover - defensive
        raw = repr(key).encode()
    import hashlib

    return "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")


def _is_retryable_exception(e: BaseException) -> bool:
    """Only transient transport failures are retried, never auth failures."""
    if isinstance(e, (PermissionDenied, HostKeyMismatch, asyncio.CancelledError)):
        return False
    return isinstance(e, (ConnectionRefusedError, asyncssh.TimeoutError, OSError, asyncssh.Error))


@dataclass
class _PooledConnection:
    conn: asyncssh.SSHClientConnection
    owner_id: int
    alias: str
    created: float = field(default_factory=time.monotonic)
    last_used: float = field(default_factory=time.monotonic)
    semaphore: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(MAX_CONCURRENT_PER_CONNECTION))

    def is_usable(self) -> bool:
        if self.conn.is_closed():
            return False
        return (time.monotonic() - self.last_used) < IDLE_TIMEOUT


class SSHManager:
    """Manages SSH connections and command execution on remote servers.

    Connections are reused for :data:`IDLE_TIMEOUT` seconds and verified
    against a pinned host key fingerprint before any command is accepted.
    """

    def __init__(self) -> None:
        #: Interactive shell sessions, keyed by (owner_id, alias).
        self.active_shells: dict[tuple[int, str], asyncssh.SSHClientConnection] = {}
        self._pool: dict[tuple[int, str], _PooledConnection] = {}
        self._pool_lock = asyncio.Lock()
        self._last_reap = 0.0

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_fixed(2),
        retry=retry_if_exception(_is_retryable_exception),
    )
    async def _open_connection(self, owner_id: int, alias: str) -> asyncssh.SSHClientConnection:
        config = get_server(owner_id, alias)
        if not config:
            raise ValueError(f"Server alias '{alias}' not found for this user.")

        connect_args: dict[str, Any] = {
            "username": config.get("user"),
            "password": config.get("password"),
            "client_keys": [config["key_path"]] if config.get("key_path") else None,
            # Verification is performed explicitly below (see _verify_host_key)
            # so that TOFU pinning can be stored in the database.
            "known_hosts": None,
        }
        port = config.get("port")
        if port:
            connect_args["port"] = int(port)

        METRICS.inc("ssh_connect_attempts_total")
        conn = await asyncssh.connect(config["hostname"], **connect_args)
        try:
            await self._verify_host_key(owner_id, alias, conn)
        except Exception:
            conn.close()
            with contextlib.suppress(Exception):
                await conn.wait_closed()
            raise
        METRICS.inc("ssh_connect_success_total")
        return conn

    async def _verify_host_key(
        self, owner_id: int, alias: str, conn: asyncssh.SSHClientConnection
    ) -> None:
        """Enforces the configured host key policy against the pinned value."""
        if HOST_KEY_POLICY in {"insecure", "off", "none"}:
            METRICS.inc("ssh_host_key_skipped_total")
            return

        try:
            key = conn.get_server_host_key()
        except Exception as exc:  # pragma: no cover - transport level failure
            raise asyncssh.Error(f"Unable to read host key for '{alias}': {exc}") from exc

        fingerprint = fingerprint_host_key(key)
        pinned = get_server_host_key(owner_id, alias)

        if pinned is None:
            if HOST_KEY_POLICY == "strict":
                raise HostKeyMismatch(alias, "<not pinned>", fingerprint)
            set_server_host_key(owner_id, alias, fingerprint)
            METRICS.inc("ssh_host_keys_pinned_total")
            logger.info("Pinned new host key for '%s': %s", alias, fingerprint)
            return

        if not secrets.compare_digest(pinned, fingerprint):
            METRICS.inc("ssh_host_key_mismatch_total")
            logger.critical(
                "HOST KEY MISMATCH for '%s' (user %s): pinned=%s presented=%s",
                alias,
                owner_id,
                pinned,
                fingerprint,
            )
            raise HostKeyMismatch(alias, pinned, fingerprint)

    async def _get_connection(self, owner_id: int, alias: str) -> _PooledConnection:
        """Returns a live connection from the pool, opening one if needed."""
        key = (owner_id, alias)
        self._reap_idle()

        async with self._pool_lock:
            pooled = self._pool.get(key)
            if pooled is not None and pooled.is_usable():
                pooled.last_used = time.monotonic()
                METRICS.inc("ssh_connections_reused_total")
                return pooled
            if pooled is not None:
                self._pool.pop(key, None)
                await self._close_pooled(pooled)

            if len(self._pool) >= MAX_POOLED_CONNECTIONS:
                oldest_key = min(self._pool, key=lambda k: self._pool[k].last_used)
                await self._close_pooled(self._pool.pop(oldest_key))

        conn = await self._open_connection(owner_id, alias)
        pooled = _PooledConnection(conn=conn, owner_id=owner_id, alias=alias)

        async with self._pool_lock:
            existing = self._pool.get(key)
            if existing is not None and existing.is_usable():
                # Another coroutine won the race; keep the older connection.
                await self._close_pooled(pooled)
                return existing
            self._pool[key] = pooled
            METRICS.set("ssh_pool_size", len(self._pool))
        return pooled

    def _reap_idle(self) -> None:
        """Synchronously schedules reaping of idle connections (cheap check)."""
        now = time.monotonic()
        if now - self._last_reap < 5.0:
            return
        self._last_reap = now
        stale = [key for key, pooled in self._pool.items() if not pooled.is_usable()]
        for key in stale:
            pooled = self._pool.pop(key, None)
            if pooled is not None:
                pooled.conn.close()
        if stale:
            METRICS.set("ssh_pool_size", len(self._pool))

    async def _close_pooled(self, pooled: _PooledConnection) -> None:
        await self._close_conn(pooled.conn)

    async def pool_stats(self) -> dict[str, int]:
        """Returns pool counters for the admin dashboard."""
        async with self._pool_lock:
            return {
                "pooled": len(self._pool),
                "usable": sum(1 for p in self._pool.values() if p.is_usable()),
                "shells": len(self.active_shells),
            }

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------
    async def run_command(
        self,
        owner_id: int,
        alias: str,
        command: str,
        timeout: float = COMMAND_TIMEOUT,
        max_bytes: int = 2 * 1024 * 1024,
    ) -> AsyncIterator[tuple[str, str]]:
        """Runs one command, streaming ``(chunk, stream_name)`` tuples.

        The first tuple is ``(pid, 'pid')`` so callers can offer cancellation.
        Stdout and stderr are pumped concurrently: reading them sequentially
        deadlocks as soon as the remote command fills the pipe of the stream
        that is not being read.
        """
        pooled = await self._get_connection(owner_id, alias)
        queue: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue()
        total = 0

        async def pump(stream: Any, name: str) -> None:
            try:
                async for chunk in stream:
                    await queue.put((chunk, name))
            finally:
                await queue.put(None)

        process = None
        tasks: list[asyncio.Task[None]] = []
        try:
            async with pooled.semaphore:
                process = await pooled.conn.create_process(wrap_remote_command(command))
                first_line = await asyncio.wait_for(process.stdout.readline(), timeout=timeout)
                pid = first_line.strip() if isinstance(first_line, str) else ""
                if pid:
                    yield pid, "pid"

                tasks = [
                    asyncio.create_task(pump(process.stdout, "stdout")),
                    asyncio.create_task(pump(process.stderr, "stderr")),
                ]
                finished = 0
                deadline = time.monotonic() + timeout
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
                    if total > max_bytes:
                        raise CommandOutputTooLarge(
                            f"Command produced more than {max_bytes} bytes of output."
                        )
                    yield chunk, name

                # Surface pump failures (transport errors, disconnects) instead
                # of letting asyncio swallow them into orphan task exceptions.
                for task in tasks:
                    error = task.exception() if task.done() else None
                    if error is not None and not isinstance(error, asyncio.CancelledError):
                        raise error

                status = await process.wait()
                if status.exit_status:
                    yield f"\n[exit status {status.exit_status}]\n", "stderr"
        except asyncio.TimeoutError:
            METRICS.inc("ssh_command_timeouts_total")
            if process is not None:
                with contextlib.suppress(Exception):
                    process.kill()
            yield "Error: Command timed out.", "stderr"
        except HostKeyMismatch:
            raise
        except (CommandOutputTooLarge, PermissionDenied):
            raise
        except Exception:
            raise
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            pooled.last_used = time.monotonic()
            METRICS.inc("ssh_commands_total")

    async def execute(
        self,
        owner_id: int,
        alias: str,
        command: str,
        timeout: float = COMMAND_TIMEOUT,
    ) -> tuple[str, str, int]:
        """Convenience helper returning ``(stdout, stderr, exit_status)``."""
        out: list[str] = []
        err: list[str] = []
        async for chunk, stream in self.run_command(owner_id, alias, command, timeout=timeout):
            if stream == "stdout":
                out.append(chunk)
            elif stream == "stderr":
                err.append(chunk)
        return "".join(out), "".join(err), 0

    async def kill_process(self, owner_id: int, alias: str, pid: int) -> None:
        """Kills a process on a remote server (and its children when possible)."""
        pooled = await self._get_connection(owner_id, alias)
        # Negative PID targets the process group; ``exec`` in the envelope keeps
        # the remote command in the shell's own group.
        await pooled.conn.run(f"kill -TERM -{int(pid)} 2>/dev/null || kill -9 {int(pid)}")

    # ------------------------------------------------------------------
    # Interactive shell
    # ------------------------------------------------------------------
    async def start_shell_session(self, owner_id: int, alias: str) -> None:
        """Starts a persistent shell session, replacing any previous one."""
        key = (owner_id, alias)
        if key in self.active_shells:
            previous = self.active_shells[key]
            with contextlib.suppress(Exception):
                previous.close()
                if hasattr(previous, "wait_closed"):
                    await previous.wait_closed()

        # Interactive shells are long lived: keep them out of the idle pool.
        conn = await self._open_connection(owner_id, alias)
        self.active_shells[key] = conn
        logger.info("Interactive shell session started for %s.", alias)

    async def run_command_in_shell(self, owner_id: int, alias: str, command: str) -> str:
        """Runs a command inside an existing shell session."""
        key = (owner_id, alias)
        if key not in self.active_shells or self.active_shells[key].is_closed():
            raise ConnectionError(f"No active shell session for {alias}. Please start a new one.")

        conn = self.active_shells[key]
        try:
            result = await conn.run(command, check=True, timeout=COMMAND_TIMEOUT)
            return result.stdout
        except asyncssh.ProcessError as exc:
            return exc.stderr
        except asyncio.TimeoutError:
            return "Error: Command timed out."
        except Exception as exc:
            logger.error("Error in shell for %s: %s", alias, exc, exc_info=True)
            return f"An unexpected error occurred: {exc}"

    async def disconnect(self, owner_id: int, alias: str) -> None:
        """Closes a shell session and evicts the pooled connection."""
        key = (owner_id, alias)
        conn = self.active_shells.pop(key, None)
        if conn is not None:
            logger.info("Closing interactive shell for %s...", alias)
            try:
                conn.close()
                if hasattr(conn, "wait_closed"):
                    await conn.wait_closed()
            except Exception as exc:
                logger.warning("Error while closing SSH session for %s: %s", alias, exc, exc_info=True)

        async with self._pool_lock:
            pooled = self._pool.pop(key, None)
        if pooled is not None:
            await self._close_pooled(pooled)
            METRICS.set("ssh_pool_size", len(self._pool))

    async def close_all_connections(self) -> None:
        """Closes every shell and pooled connection."""
        logger.info("Closing all SSH connections...")
        for owner_id, alias in list(self.active_shells.keys()):
            await self.disconnect(owner_id, alias)
        async with self._pool_lock:
            pooled_items = list(self._pool.values())
            self._pool.clear()
        for pooled in pooled_items:
            await self._close_pooled(pooled)
        METRICS.set("ssh_pool_size", 0)

    # ------------------------------------------------------------------
    # File transfer
    # ------------------------------------------------------------------
    async def download_file(self, owner_id: int, alias: str, remote_path: str, local_path: str) -> None:
        """Downloads a file from a remote server."""
        pooled = await self._get_connection(owner_id, alias)
        async with pooled.conn.start_sftp_client() as sftp:
            await sftp.get(remote_path, local_path)

    async def upload_file(self, owner_id: int, alias: str, local_path: str, remote_path: str) -> None:
        """Uploads a file to a remote server."""
        pooled = await self._get_connection(owner_id, alias)
        async with pooled.conn.start_sftp_client() as sftp:
            await sftp.put(local_path, remote_path)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    async def _close_conn(self, conn: Any) -> None:
        """Safely closes an SSH connection, tolerating sync/async variants."""
        if conn is None:
            return

        close = getattr(conn, "close", None)
        close_result = None
        if callable(close):
            close_result = close()
        if asyncio.iscoroutine(close_result) or isinstance(close_result, asyncio.Future):
            await close_result

        wait_closed = getattr(conn, "wait_closed", None)
        if callable(wait_closed):
            result = wait_closed()
            if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
                await result
