import asyncio
import asyncssh
import logging
import async_timeout
from asyncssh import PermissionDenied
from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_exception
from .database import get_all_servers

# --- Constants ---
# The default timeout for a command to complete.
COMMAND_TIMEOUT = 60.0 # 60 seconds

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
        self.server_configs = {}
        # active_shells: A dictionary to store persistent connections for interactive shells.
        # Format: { "alias": asyncssh.SSHClientConnection }
        self.active_shells = {}
        self.refresh_server_configs()

    def refresh_server_configs(self):
        """Reloads server configurations from the database."""
        logger.info("Refreshing server configurations from database...")
        try:
            self.server_configs = {s['alias']: s for s in get_all_servers()}
            logger.info(f"Loaded {len(self.server_configs)} server configs.")
        except Exception as e:
            logger.error(f"Failed to refresh server configs: {e}", exc_info=True)

    # Use a retry decorator to handle transient network errors during connection.
    # The _is_retryable_exception function provides fine-grained control over
    # which exceptions should trigger a retry.
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_fixed(2),
        retry=retry_if_exception(_is_retryable_exception)
    )
    async def _create_connection(self, alias: str):
        """
        Establishes a new SSH connection with retry logic for transient errors.
        """
        if alias not in self.server_configs:
            raise ValueError(f"Server alias '{alias}' not found.")

        config = self.server_configs[alias]
        connect_args = {
            'username': config.get('user'),
            'password': config.get('password'),
            'client_keys': [config['key_path']] if config.get('key_path') else None,
            'known_hosts': None  # For simplicity; in production, consider verifying hosts
        }

        try:
            return await asyncssh.connect(config['hostname'], **connect_args)
        except Exception as e:
            logger.error(f"Failed to connect to {alias}: {e}")
            raise  # Re-raise the exception to be handled by the caller

    async def run_command(self, alias: str, command: str, timeout: float = COMMAND_TIMEOUT):
        """
        Connects to a server, runs a single command with a timeout, and disconnects.

        This method streams the output of the command in real-time.

        Yields:
            tuple[str, str]: A tuple containing the output line and the stream name ('stdout' or 'stderr').
        """
        conn = None
        try:
            conn = await self._create_connection(alias)
            async with async_timeout.timeout(timeout):
                process = await conn.create_process(command)
                yield process, 'pid'
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
            # --- Definitive Crash Fix ---
            # A simple `if conn:` check is the safest way to prevent an `await`
            # on a `None` object, regardless of how the connection failed.
            if conn:
                await conn.close()

    async def kill_process(self, alias: str, pid: int) -> None:
        """Kills a process on a remote server."""
        conn = None
        try:
            conn = await self._create_connection(alias)
            await conn.run(f"kill -9 {pid}")
        except Exception:
            raise
        finally:
            logger.debug(f"In finally block for kill_process. Connection object is: {conn}")
            if conn and not conn.is_closed():
                logger.debug("Connection is valid and not closed, closing now.")
                await conn.close()
            elif conn:
                logger.debug("Connection is already closed or closing.")
            else:
                logger.debug("Connection is None, nothing to close.")

    async def start_shell_session(self, alias: str) -> None:
        """
        Starts a persistent interactive shell for a user.
        If a shell for the alias already exists, it will be closed and replaced.
        """
        # If a shell already exists for this alias, close it before creating a new one.
        if alias in self.active_shells:
            await self.active_shells[alias].close()

        conn = await self._create_connection(alias)
        self.active_shells[alias] = conn
        logger.info(f"Interactive shell session started for {alias}.")

    async def run_command_in_shell(self, alias: str, command: str) -> str:
        """
        Runs a command within an existing interactive shell.
        If no shell is active, it will raise an exception.
        """
        if alias not in self.active_shells or self.active_shells[alias].is_closed():
            raise ConnectionError(f"No active shell session for {alias}. Please start a new one.")

        conn = self.active_shells[alias]
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

    async def disconnect(self, alias: str):
        """
        Closes a persistent shell connection.
        """
        if alias in self.active_shells:
            logger.info(f"Closing interactive shell for {alias}.")
            await self.active_shells[alias].close()
            del self.active_shells[alias]

    async def close_all_connections(self):
        """Closes all active shell connections."""
        logger.info("Closing all persistent SSH shell connections...")
        for alias in list(self.active_shells.keys()):
            await self.disconnect(alias)

    async def download_file(self, alias: str, remote_path: str, local_path: str) -> None:
        """Downloads a file from a remote server."""
        conn = None
        try:
            conn = await self._create_connection(alias)
            async with conn.start_sftp_client() as sftp:
                await sftp.get(remote_path, local_path)
        except Exception:
            raise
        finally:
            logger.debug(f"In finally block for download_file. Connection object is: {conn}")
            if conn and not conn.is_closed():
                logger.debug("Connection is valid and not closed, closing now.")
                await conn.close()
            elif conn:
                logger.debug("Connection is already closed or closing.")
            else:
                logger.debug("Connection is None, nothing to close.")

    async def upload_file(self, alias: str, local_path: str, remote_path: str) -> None:
        """Uploads a file to a remote server."""
        conn = None
        try:
            conn = await self._create_connection(alias)
            async with conn.start_sftp_client() as sftp:
                await sftp.put(local_path, remote_path)
        except Exception:
            raise
        finally:
            logger.debug(f"In finally block for upload_file. Connection object is: {conn}")
            if conn and not conn.is_closed():
                logger.debug("Connection is valid and not closed, closing now.")
                await conn.close()
            elif conn:
                logger.debug("Connection is already closed or closing.")
            else:
                logger.debug("Connection is None, nothing to close.")

    # --- Health Check (No longer needed) ---
    # The start_health_check and stop_health_check methods are removed as they
    # are not required with the new just-in-time connection model.
