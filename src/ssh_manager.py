from __future__ import annotations

import asyncio
import inspect
import logging
import os
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Dict, Optional

import asyncssh
import async_timeout
from asyncssh import PermissionDenied, ProcessError
from tenacity import (
    retry,
    stop_after_attempt,
    wait_random_exponential,
    retry_if_exception,
)

from .database import get_all_servers

# ────────────────────────────────────────────────────────────────────────────────
# Constants & Defaults
# ────────────────────────────────────────────────────────────────────────────────

COMMAND_TIMEOUT_DEFAULT = float(os.getenv("TLA_COMMAND_TIMEOUT", "120"))  # sec
CONNECT_TIMEOUT = float(os.getenv("TLA_CONNECT_TIMEOUT", "15"))           # sec
SFTP_TIMEOUT = float(os.getenv("TLA_SFTP_TIMEOUT", "60"))                  # sec
KEEPALIVE_INTERVAL = int(os.getenv("TLA_KEEPALIVE_INTERVAL", "15"))       # sec
KEEPALIVE_COUNT_MAX = int(os.getenv("TLA_KEEPALIVE_COUNT", "3"))

# Streaming/queue behavior
HEARTBEAT_INTERVAL = float(os.getenv("TLA_HEARTBEAT_INTERVAL", "5"))      # sec
MAX_QUEUE_SIZE = int(os.getenv("TLA_MAX_QUEUE", "1000"))                  # items
QUEUE_OVERFLOW_SAMPLE_BYTES = 4096

# Retry/backoff
RETRY_ATTEMPTS = int(os.getenv("TLA_RETRY_ATTEMPTS", "3"))

# Concurrency & breaker
MAX_CONCURRENT_PER_ALIAS = int(os.getenv("TLA_CONCURRENT_PER_ALIAS", "3"))
BREAKER_FAIL_THRESHOLD = int(os.getenv("TLA_BREAKER_FAILS", "5"))
BREAKER_COOL_OFF = float(os.getenv("TLA_BREAKER_COOL", "30"))             # sec

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────────
# Utilities
# ────────────────────────────────────────────────────────────────────────────────

def _is_retryable_exception(e: Exception) -> bool:
    """Decide if we should retry connecting."""
    if isinstance(e, PermissionDenied):
        return False
    # Retry on typical transient issues
    if isinstance(e, (asyncssh.TimeoutError, ConnectionRefusedError, OSError, asyncssh.Error)):
        return True
    return False


def _shell_quote(s: str) -> str:
    """Single-quote for POSIX shell safely."""
    return "'" + s.replace("'", "'\"'\"'") + "'"


def _wrap_with_pid_echo(command: str) -> str:
    """
    Wrap command so the first stdout line is the shell PID, then exec the command.
    This lets us provide a usable PID for cancel/kill.
    """
    # Use bash -lc: echo $$; exec <cmd>
    return f"bash -lc 'echo $$; exec {command}'"


@dataclass
class _BreakerState:
    fails: int = 0
    opened_at: float = 0.0


# ────────────────────────────────────────────────────────────────────────────────
# SSH Manager
# ────────────────────────────────────────────────────────────────────────────────

class SSHManager:
    """
    Robust SSH manager with:
      • just-in-time connections (non-shell) + persistent connections (interactive shell)
      • concurrent, backpressured streaming
      • PID capture for cancel/kill flows
      • strong anti-hang guarantees
    """

    def __init__(self) -> None:
        self.server_configs: Dict[str, dict] = {}
        self.active_shells: Dict[str, asyncssh.SSHClientConnection] = {}
        self.alias_semaphores: Dict[str, asyncio.Semaphore] = {}
        self.breaker: Dict[str, _BreakerState] = {}
        self.refresh_server_configs()

    # ── Config & limits ────────────────────────────────────────────────────────

    def refresh_server_configs(self) -> None:
        """Reload server definitions from DB."""
        try:
            servers = {s["alias"]: s for s in get_all_servers()}
            self.server_configs = servers
            # Ensure semaphores exist for all aliases
            for alias in servers:
                self.alias_semaphores.setdefault(alias, asyncio.Semaphore(MAX_CONCURRENT_PER_ALIAS))
            logger.info("Loaded %d server configs.", len(servers))
        except Exception as e:
            logger.error("Failed to refresh server configs: %s", e, exc_info=True)

    # ── Circuit breaker ────────────────────────────────────────────────────────

    def _breaker_allows(self, alias: str) -> bool:
        st = self.breaker.get(alias)
        if not st:
            return True
        if st.fails < BREAKER_FAIL_THRESHOLD:
            return True
        # if in cool-off, block
        now = asyncio.get_event_loop().time()
        if now - st.opened_at < BREKER_COOL := BREAKER_COOL_OFF:
            return False
        # Reset after cool-off
        self.breaker[alias] = _BreakerState()
        return True

    def _breaker_record_success(self, alias: str) -> None:
        self.breaker[alias] = _BreakerState()

    def _breaker_record_failure(self, alias: str) -> None:
        st = self.breaker.get(alias) or _BreakerState()
        st.fails += 1
        if st.fails >= BREAKER_FAIL_THRESHOLD and st.opened_at == 0.0:
            st.opened_at = asyncio.get_event_loop().time()
        self.breaker[alias] = st

    # ── Connection lifecycle ───────────────────────────────────────────────────

    @retry(
        stop=stop_after_attempt(RETRY_ATTEMPTS),
        wait=wait_random_exponential(multiplier=1, max=10),
        retry=retry_if_exception(_is_retryable_exception),
        reraise=True,
    )
    async def _create_connection(self, alias: str) -> asyncssh.SSHClientConnection:
        """Establish a connection with jittered exponential backoff & keepalives."""
        if alias not in self.server_configs:
            raise ValueError(f"Server alias '{alias}' not found.")

        if not self._breaker_allows(alias):
            raise RuntimeError(f"Circuit breaker open for '{alias}'. Please try again later.")

        cfg = self.server_configs[alias]
        connect_args = dict(
            username=cfg.get("user"),
            password=cfg.get("password"),
            client_keys=[cfg["key_path"]] if cfg.get("key_path") else None,
            known_hosts=None,  # you may enforce host key verification in production
            connect_timeout=CONNECT_TIMEOUT,
            keepalive_interval=KEEPALIVE_INTERVAL,
            keepalive_count_max=KEEPALIVE_COUNT_MAX,
        )

        logger.debug("Connecting to %s (%s) ...", alias, cfg.get("hostname"))
        conn = await asyncssh.connect(cfg["hostname"], **connect_args)
        logger.debug("Connected to %s", alias)
        return conn

    async def _close_conn(self, conn: Optional[asyncssh.SSHClientConnection]) -> None:
        """Close conn safely (no awaiting .close(); await .wait_closed() if present)."""
        if not conn:
            return
        try:
            conn.close()
            if hasattr(conn, "wait_closed"):
                await conn.wait_closed()
        except Exception as e:
            logger.warning("Error during connection close: %s", e, exc_info=True)

    # ── Public API: single command (streaming) ─────────────────────────────────

    async def run_command(
        self,
        alias: str,
        command: str,
        *,
        timeout: float = COMMAND_TIMEOUT_DEFAULT,
        capture_pid: bool = True,
        allocate_pty: bool = False,
        env: Optional[Dict[str, str]] = None,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> AsyncGenerator[tuple[str, str], None]:
        """
        Stream a remote command's output.

        Yields (item, stream) where stream ∈ {'stdout','stderr','pid','meta'}.
        'pid' is yielded once (stringified int) when capture_pid=True.

        Anti-hang guarantees:
          • global timeout (timeout)
          • heartbeat 'meta' every HEARTBEAT_INTERVAL seconds if no output
          • safe teardown on cancel/timeout

        NOTE: For backward compatibility with your bot, extra 'meta' events are safe to ignore.
        """
        if alias not in self.alias_semaphores:
            self.alias_semaphores[alias] = asyncio.Semaphore(MAX_CONCURRENT_PER_ALIAS)

        sem = self.alias_semaphores[alias]
        await sem.acquire()
        conn: Optional[asyncssh.SSHClientConnection] = None

        # Queue & overflow guards
        queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue(MAX_QUEUE_SIZE)
        dropped_bytes = 0

        async def _reader(stream: asyncssh.SSHReader, name: str) -> None:
            nonlocal dropped_bytes
            try:
                async for chunk in stream:
                    if queue.qsize() >= MAX_QUEUE_SIZE:
                        # Drop to avoid OOM; account and sample later
                        dropped_bytes += len(chunk.encode() if isinstance(chunk, str) else chunk)
                        continue
                    await queue.put((chunk, name))
            except Exception as e:
                logger.warning("Reader %s aborted: %s", name, e, exc_info=True)

        try:
            conn = await self._create_connection(alias)
            self._breaker_record_success(alias)

            # Wrap command to emit PID as first stdout line
            cmd = _wrap_with_pid_echo(command) if capture_pid else command

            # Create process
            create_kwargs = dict(term_type="xterm-256color") if allocate_pty else {}
            async with async_timeout.timeout(timeout):
                process = await conn.create_process(cmd, env=env, **create_kwargs)

            # Start readers concurrently
            task_out = asyncio.create_task(_reader(process.stdout, "stdout"))
            task_err = asyncio.create_task(_reader(process.stderr, "stderr"))

            # Heartbeat/watchdog loop
            pid_emitted = False
            pending_readers = {task_out, task_err}
            last_activity = asyncio.get_event_loop().time()
            pid_buffer = ""  # capture first stdout line as PID

            try:
                async with async_timeout.timeout(timeout):
                    while True:
                        try:
                            item, stream = await asyncio.wait_for(
                                queue.get(), timeout=HEARTBEAT_INTERVAL
                            )
                            last_activity = asyncio.get_event_loop().time()

                            # Handle PID capture from the very first stdout line
                            if capture_pid and not pid_emitted and stream == "stdout":
                                pid_buffer += item
                                if "\n" in pid_buffer:
                                    first_line, remainder = pid_buffer.split("\n", 1)
                                    if first_line.strip().isdigit():
                                        await self._safe_yield((first_line.strip(), "pid"))
                                        pid_emitted = True
                                        if remainder:
                                            await self._safe_yield((remainder, "stdout"))
                                    else:
                                        # Not a PID; pass through and disable PID capture
                                        await self._safe_yield((pid_buffer, "stdout"))
                                        pid_emitted = True
                                continue

                            # Normal streaming
                            await self._safe_yield((item, stream))

                            # Queue overflow notice (once per loop if happened)
                            if dropped_bytes:
                                sample = f"... [dropped ~{dropped_bytes} bytes due to backpressure] ..."
                                await self._safe_yield((sample, "stderr"))
                                dropped_bytes = 0

                        except asyncio.TimeoutError:
                            # Heartbeat
                            now = asyncio.get_event_loop().time()
                            if now - last_activity >= HEARTBEAT_INTERVAL:
                                await self._safe_yield(("heartbeat", "meta"))

                        # Cancellation check
                        if cancel_event and cancel_event.is_set():
                            await self._graceful_terminate(conn, process, alias)
                            await self._safe_yield((f"Command cancelled on '{alias}'", "stderr"))
                            break

                        # Exit when both readers finished and queue drained
                        if all(t.done() for t in pending_readers) and queue.empty():
                            break

            except asyncio.TimeoutError:
                # Global command timeout
                await self._safe_yield((f"Error: Command timed out after {timeout:.0f}s.", "stderr"))
                await self._force_close_connection(conn)
                return

            finally:
                # Ensure readers are stopped
                for t in (task_out, task_err):
                    t.cancel()
                    with contextlib.suppress(Exception):
                        await t

            # Final overflow note if any
            if dropped_bytes:
                sample = f"... [dropped ~{dropped_bytes} bytes due to backpressure] ..."
                await self._safe_yield((sample, "stderr"))

        except PermissionDenied as e:
            self._breaker_record_failure(alias)
            raise e
        except Exception as e:
            self._breaker_record_failure(alias)
            # Bubble up to the bot's global error handler
            raise
        finally:
            await self._close_conn(conn)
            sem.release()

    async def _graceful_terminate(self, conn: Optional[asyncssh.SSHClientConnection], process: Any, alias: str) -> None:
        """Try TERM first, then force close the channel/connection."""
        try:
            # Best-effort soft terminate: close stdin and channel
            if hasattr(process, "stdin") and process.stdin:
                with contextlib.suppress(Exception):
                    process.stdin.write_eof()
            # Force channel close by closing connection (most reliable cross-platform)
            await self._force_close_connection(conn)
        except Exception as e:
            logger.warning("Error during graceful terminate on %s: %s", alias, e, exc_info=True)

    async def _force_close_connection(self, conn: Optional[asyncssh.SSHClientConnection]) -> None:
        """Hard-close the connection (kills remote process)."""
        try:
            if conn:
                conn.close()
                if hasattr(conn, "wait_closed"):
                    await conn.wait_closed()
        except Exception as e:
            logger.warning("Force close error: %s", e, exc_info=True)

    async def _safe_yield(self, item: tuple[str, str]) -> None:
        """Helper to make future instrumentation easier."""
        yield item  # type: ignore[misc]

    # ── Convenience: collect command (non-stream) ──────────────────────────────

    async def run_command_collect(
        self,
        alias: str,
        command: str,
        *,
        timeout: float = COMMAND_TIMEOUT_DEFAULT,
        env: Optional[Dict[str, str]] = None,
    ) -> tuple[str, str]:
        """
        Run a command and return (stdout, stderr) fully collected.
        Uses the same protections as the streaming variant.
        """
        out_chunks: list[str] = []
        err_chunks: list[str] = []
        async for chunk, stream in self.run_command(
            alias,
            command,
            timeout=timeout,
            capture_pid=False,
            env=env,
        ):
            if stream == "stdout":
                out_chunks.append(chunk)
            elif stream == "stderr":
                err_chunks.append(chunk)
        return ("".join(out_chunks), "".join(err_chunks))

    # ── Kill helpers (when you already know the PID) ───────────────────────────

    async def kill_process(self, alias: str, pid: int, *, grace: float = 3.0) -> None:
        """Try SIGTERM then SIGKILL if still alive after 'grace' seconds."""
        conn: Optional[asyncssh.SSHClientConnection] = None
        try:
            conn = await self._create_connection(alias)
            self._breaker_record_success(alias)

            async with async_timeout.timeout(COMMAND_TIMEOUT_DEFAULT):
                await conn.run(f"kill -TERM {pid}", check=False)
                # Wait a bit; if still alive, KILL
                await asyncio.sleep(grace)
                await conn.run(f"kill -0 {pid}", check=False)  # check exists
                await conn.run(f"kill -KILL {pid}", check=False)
        except ProcessError:
            # kill -0 failed → already gone; treat as success
            return
        finally:
            await self._close_conn(conn)

    # ── Interactive shell (persistent) ─────────────────────────────────────────

    async def start_shell_session(self, alias: str) -> None:
        """Start (or replace) a persistent shell connection for interactive mode."""
        # Close any existing one first, safely
        await self.disconnect(alias)

        conn = await self._create_connection(alias)
        self._breaker_record_success(alias)
        self.active_shells[alias] = conn
        logger.info("Interactive shell session started for %s.", alias)

    async def run_command_in_shell(self, alias: str, command: str) -> str:
        """Run within an active shell connection."""
        conn = self.active_shells.get(alias)
        if not conn or conn.is_closed():
            raise ConnectionError(f"No active shell session for {alias}. Start one first.")

        try:
            result = await conn.run(command, check=False, timeout=COMMAND_TIMEOUT_DEFAULT)
            return (result.stdout or "") + (result.stderr or "")
        except asyncio.TimeoutError:
            return "Error: Command timed out."
        except Exception as e:
            logger.error("Shell error on %s: %s", alias, e, exc_info=True)
            return f"An unexpected error occurred: {e}"

    async def disconnect(self, alias: str) -> None:
        """Close the persistent shell connection for an alias, if any."""
        conn = self.active_shells.get(alias)
        if not conn:
            return
        logger.info("Closing interactive shell for %s...", alias)
        try:
            conn.close()
            if hasattr(conn, "wait_closed"):
                await conn.wait_closed()
        except Exception as e:
            logger.warning("Error while closing SSH session for %s: %s", alias, e, exc_info=True)
        finally:
            self.active_shells.pop(alias, None)

    async def close_all_connections(self) -> None:
        """Close all persistent shells."""
        logger.info("Closing all persistent SSH shell connections...")
        for alias in list(self.active_shells.keys()):
            await self.disconnect(alias)

    # ── SFTP (with timeouts) ───────────────────────────────────────────────────

    async def download_file(self, alias: str, remote_path: str, local_path: str) -> None:
        conn: Optional[asyncssh.SSHClientConnection] = None
        try:
            conn = await self._create_connection(alias)
            self._breaker_record_success(alias)

            async with async_timeout.timeout(SFTP_TIMEOUT):
                async with conn.start_sftp_client() as sftp:
                    await sftp.get(remote_path, local_path)
        finally:
            await self._close_conn(conn)

    async def upload_file(self, alias: str, local_path: str, remote_path: str) -> None:
        conn: Optional[asyncssh.SSHClientConnection] = None
        try:
            conn = await self._create_connection(alias)
            self._breaker_record_success(alias)

            async with async_timeout.timeout(SFTP_TIMEOUT):
                async with conn.start_sftp_client() as sftp:
                    await sftp.put(local_path, remote_path)
        finally:
            await self._close_conn(conn)
