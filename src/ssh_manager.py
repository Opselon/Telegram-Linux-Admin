import asyncio
import asyncssh
import inspect
import logging
import async_timeout
import contextlib
from typing import Any
from asyncssh import PermissionDenied
from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_exception
from .database import get_server

# --- Constants ---
# The default timeout for a command to complete.
COMMAND_TIMEOUT = 60.0  # 60 seconds

# --- Logging ---
logger = logging.getLogger(__name__)


def _is_retryable_exception(e: Exception) -> bool:
    """
    Determines if an exception is retryable.

    Returns True for transient network errors and False for permanent errors
    like authentication failure.
    """
    # Don't retry on authentication errors
    if isinstance(e, PermissionDenied):
        return False
    # Retry on common transient network and SSH errors
    if isinstance(e, (ConnectionRefusedError, asyncssh.TimeoutError, OSError, asyncssh.Error)):
        return True
    return False


class SSHManager:
    """
    Manages SSH connections and command execution on remote servers.

    This manager uses a "just-in-time" connection model. Connections are established
    when a command needs to be run and are closed immediately afterward. This
    approach minimizes the bot's idle RAM and CPU usage at the cost of slightly
    higher latency per command.

    For interactive shell sessions, a persistent connection is maintained but is
    tied to the user's session and cleaned up on exit.
    """

    def __init__(self):
        """Initializes the SSHManager."""
        # active_shells: keyed by (owner_id, alias)
        self.active_shells: dict[tuple[int, str], asyncssh.SSHClientConnection] = {}

    # Use a retry decorator to handle transient network errors during connection.
    # The _is_retryable_exception function provides fine-grained control over
    # which exceptions should trigger a retry.
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_fixed(2),
        retry=retry_if_exception(_is_retryable_exception)
    )
    async def _create_connection(self, owner_id: int, alias: str):
        """
        Establishes a new SSH connection with retry logic for transient errors.
        """
        config = get_server(owner_id, alias)
        if not config:
            raise ValueError(f"Server alias '{alias}' not found for this user.")
        connect_args = {
            'username': config.get('user'),
            'password': config.get('password'),
            'client_keys': [config['key_path']] if config.get('key_path') else [],
            'known_hosts': None  # For simplicity; in production, consider verifying hosts
        }

        try:
            return await asyncssh.connect(config['hostname'], **connect_args)
        except Exception as e:
            logger.error(f"Failed to connect to {alias}: {e}")
            raise  # Re-raise the exception to be handled by the caller

    async def run_command(
        self,
        owner_id: int,
        alias: str,
        command: str,
        timeout: float = COMMAND_TIMEOUT,
    ):
        """
        Connects to a server, runs a single command with a timeout, and disconnects.

        This method streams the output of the command in real-time.

        Yields:
            tuple[str, str]: A tuple containing the output line and the stream name ('stdout' or 'stderr').
        """
        conn = None
        try:
            conn = await self._create_connection(owner_id, alias)
            async with async_timeout.timeout(timeout):
                # Emit remote PID as first stdout line for reliable cancel support
                process = await conn.create_process(f"bash -lc 'echo $$; exec {command}'")
                # Read first line as PID
                pid_line = await process.stdout.readline()
                pid_value = pid_line.strip() if isinstance(pid_line, str) else ""
                if pid_value:
                    yield pid_value, 'pid'
                # Stream remaining stdout then stderr (simple streaming model)
                async for line in process.stdout:
                    yield line, 'stdout'
                async for line in process.stderr:
                    yield line, 'stderr'
        except asyncio.TimeoutError:
            yield "Error: Command timed out.", 'stderr'
        except Exception:
            # Re-raise the exception to be handled by the global error handler
            raise
        finally:
            await self._close_conn(conn)

    async def kill_process(self, owner_id: int, alias: str, pid: int) -> None:
        """Kills a process on a remote server."""
        conn = None
        try:
            conn = await self._create_connection(owner_id, alias)
            await conn.run(f"kill -9 {pid}")
        except Exception:
            raise
        finally:
            await self._close_conn(conn)

    async def start_shell_session(self, owner_id: int, alias: str) -> None:
        """
        Starts a persistent interactive shell for a user.
        If a shell for the alias already exists, it will be closed and replaced.
        """
        # If a shell already exists for this alias, close it before creating a new one.
        key = (owner_id, alias)
        if key in self.active_shells:
            conn_prev = self.active_shells[key]
            with contextlib.suppress(Exception):
                conn_prev.close()
                if hasattr(conn_prev, "wait_closed"):
                    await conn_prev.wait_closed()

        conn = await self._create_connection(owner_id, alias)
        self.active_shells[key] = conn
        logger.info(f"Interactive shell session started for {alias}.")

    async def run_command_in_shell(self, owner_id: int, alias: str, command: str) -> str:
        """
        Runs a command within an existing interactive shell.
        If no shell is active, it will raise an exception.
        """
        key = (owner_id, alias)
        if key not in self.active_shells or self.active_shells[key].is_closed():
            raise ConnectionError(f"No active shell session for {alias}. Please start a new one.")

        conn = self.active_shells[key]
        try:
            # Execute the command and read the output.
            # This is a simplified approach; real interactive shells are complex.
            result = await conn.run(command, check=True, timeout=COMMAND_TIMEOUT)
            return result.stdout
        except asyncssh.ProcessError as e:
            return e.stderr
        except asyncio.TimeoutError:
            return "Error: Command timed out."
        except Exception as e:
            logger.error(f"Error in shell for {alias}: {e}", exc_info=True)
            return f"An unexpected error occurred: {e}"

    async def disconnect(self, owner_id: int, alias: str):
        """
        Safely closes a persistent shell connection.
        """
        key = (owner_id, alias)
        conn = self.active_shells.get(key)
        if not conn:
            return  # No active connection to close

        logger.info(f"Closing interactive shell for {alias}...")
        try:
            # AsyncSSH close() is synchronous, but wait_closed() is awaitable.
            conn.close()
            if hasattr(conn, "wait_closed"):
                await conn.wait_closed()
        except Exception as e:
            logger.warning(f"Error while closing SSH session for {alias}: {e}", exc_info=True)
        finally:
            self.active_shells.pop(key, None)

    async def close_all_connections(self):
        """Closes all active shell connections."""
        logger.info("Closing all persistent SSH shell connections...")
        for owner_id, alias in list(self.active_shells.keys()):
            await self.disconnect(owner_id, alias)

    async def download_file(self, owner_id: int, alias: str, remote_path: str, local_path: str) -> None:
        """Downloads a file from a remote server."""
        conn = None
        try:
            conn = await self._create_connection(owner_id, alias)
            async with conn.start_sftp_client() as sftp:
                await sftp.get(remote_path, local_path)
        except Exception:
            raise
        finally:
            await self._close_conn(conn)

    async def upload_file(self, owner_id: int, alias: str, local_path: str, remote_path: str) -> None:
        """Uploads a file to a remote server."""
        conn = None
        try:
            conn = await self._create_connection(owner_id, alias)
            async with conn.start_sftp_client() as sftp:
                await sftp.put(local_path, remote_path)
        except Exception:
            raise
        finally:
            await self._close_conn(conn)

    # --- Health Check (No longer needed) ---
    # The start_health_check and stop_health_check methods are removed as they
    # are not required with the new just-in-time connection model.

    async def _close_conn(self, conn: Any) -> None:
        """
        Safely closes an SSH connection, handling various library patterns.
        """
        if conn is None:
            return

        close = getattr(conn, "close", None)
        close_result = None
        if callable(close):
            close_result = close()

        if inspect.isawaitable(close_result):
            # AsyncSSH close() is sync; this branch is defensive
            await close_result

        wait_closed = getattr(conn, "wait_closed", None)
        if callable(wait_closed):
            await wait_closed()
