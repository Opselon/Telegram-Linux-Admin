import asyncio
import asyncssh
import logging
import time
from .database import get_all_servers

# --- Constants ---
IDLE_TIMEOUT = 300  # 5 minutes
HEALTH_CHECK_INTERVAL = 60 # 1 minute
SHELL_PROMPT_PATTERN = r'\[.*@.* ~\]\$ $' # Example prompt, adjust if needed

# --- Logging ---
logger = logging.getLogger(__name__)

class SSHConnection:
    def __init__(self, config):
        self.config = config
        self.conn = None
        self.shell_process = None
        self.last_activity = time.time()

    async def connect(self):
        if self.is_connected():
            return

        logger.info(f"Attempting to connect to {self.config['alias']}...")
        attempts = 3
        delay = 2
        for i in range(attempts):
            try:
                connect_args = {
                    'username': self.config['user'],
                    'password': self.config.get('password'),
                    'client_keys': [self.config['key_path']] if self.config.get('key_path') else None,
                    'known_hosts': None
                }
                # Remove None values
                connect_args = {k: v for k, v in connect_args.items() if v is not None}

                self.conn = await asyncssh.connect(self.config['hostname'], **connect_args)
                logger.info(f"Successfully connected to {self.config['alias']}.")
                self.last_activity = time.time()
                return

            except Exception as e:
                logger.error(f"Connection attempt {i+1}/{attempts} to {self.config['alias']} failed: {e}")
                if i < attempts - 1:
                    await asyncio.sleep(delay * (i + 1))
                else:
                    self.conn = None
                    raise ConnectionError(f"Failed to connect to {self.config['alias']} after {attempts} attempts: {e}")

    def is_connected(self):
        return self.conn is not None and not self.conn.is_closing()

    def update_activity(self):
        self.last_activity = time.time()

    async def run_command(self, command):
        self.update_activity()
        if not self.is_connected():
            await self.connect()

        async with self.conn.create_process(command) as process:
            async for line in process.stdout:
                yield line, 'stdout'
            async for line in process.stderr:
                yield line, 'stderr'

    async def start_shell(self):
        """Starts a persistent shell process."""
        self.update_activity()
        if not self.is_connected():
            await self.connect()
        if self.shell_process and not self.shell_process.is_closing():
            return

        logger.info(f"Starting interactive shell for {self.config['alias']}...")
        self.shell_process = await self.conn.create_process(term_type='xterm')
        # Read the initial prompt to clear the buffer
        try:
            await asyncio.wait_for(self.shell_process.stdout.readuntil(SHELL_PROMPT_PATTERN), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("Timeout waiting for initial shell prompt.")


    async def run_in_shell(self, command):
        """Runs a command in the persistent shell and reads output until the next prompt."""
        self.update_activity()
        if not self.shell_process or self.shell_process.is_closing():
            await self.start_shell()

        self.shell_process.stdin.write(command + '\n')

        try:
            # Read until the next shell prompt appears
            output = await asyncio.wait_for(self.shell_process.stdout.readuntil(SHELL_PROMPT_PATTERN), timeout=15.0)
            # The output includes the command and the next prompt, so we should clean it up.
            # This part can be tricky and may need adjustment based on shell behavior.
            lines = output.split('\n')
            if len(lines) > 1:
                return '\n'.join(lines[1:-1]) # Exclude the command echo and the new prompt
            return ""

        except asyncio.TimeoutError:
            logger.error(f"Timeout waiting for command output on {self.config['alias']}.")
            return "Error: Command timed out."
        except Exception as e:
            logger.error(f"Error running command in shell: {e}", exc_info=True)
            return f"Error: {e}"

    async def close(self):
        logger.info(f"Closing connection to {self.config['alias']}...")
        if self.shell_process:
            self.shell_process.terminate()
        if self.conn:
            self.conn.close()
            try:
                await self.conn.wait_closed()
                logger.info(f"Connection to {self.config['alias']} closed successfully.")
            except Exception as e:
                logger.error(f"Error while closing connection to {self.config['alias']}: {e}", exc_info=True)


class SSHManager:
    def __init__(self):
        self.connections = {}
        self.server_configs = {}
        self.health_check_task = None
        self.refresh_server_configs()

    def get_server_config(self, alias):
        return self.server_configs.get(alias)

    def refresh_server_configs(self):
        logger.info("Refreshing server configurations from database...")
        try:
            self.server_configs = {s['alias']: s for s in get_all_servers()}
            logger.info(f"Loaded {len(self.server_configs)} server configs.")
        except Exception as e:
            logger.error(f"Failed to refresh server configs: {e}", exc_info=True)


    async def get_connection(self, alias):
        conn = self.connections.get(alias)

        if not conn:
            server_config = self.get_server_config(alias)
            if not server_config:
                raise ValueError(f"Server with alias '{alias}' not found in the database.")
            conn = SSHConnection(server_config)
            self.connections[alias] = conn

        if not conn.is_connected():
            await conn.connect()

        conn.update_activity()
        return conn

    async def disconnect(self, alias):
        if alias in self.connections:
            logger.info(f"Disconnecting from {alias} as requested.")
            await self.connections[alias].close()
            del self.connections[alias]

    async def close_all_connections(self):
        logger.info("Closing all SSH connections...")
        for alias in list(self.connections.keys()):
            await self.disconnect(alias)

    async def run_command(self, alias, command):
        conn = await self.get_connection(alias)
        async for line, stream in conn.run_command(command):
            yield line, stream

    async def start_shell_session(self, alias):
        conn = await self.get_connection(alias)
        await conn.start_shell()

    async def run_command_in_shell(self, alias, command):
        conn = await self.get_connection(alias)
        return await conn.run_in_shell(command)

    async def _health_check(self):
        """Periodically checks for idle or disconnected connections."""
        while True:
            await asyncio.sleep(HEALTH_CHECK_INTERVAL)
            logger.info("Running SSH connection health check...")

            disconnected_aliases = []
            for alias, conn in self.connections.items():
                if not conn.is_connected():
                    logger.warning(f"Connection to {alias} found to be closed. Removing from pool.")
                    disconnected_aliases.append(alias)
                    continue

                if time.time() - conn.last_activity > IDLE_TIMEOUT:
                    logger.info(f"Connection to {alias} has been idle for too long. Closing.")
                    await conn.close()
                    disconnected_aliases.append(alias)

            for alias in disconnected_aliases:
                if alias in self.connections:
                    del self.connections[alias]

    def start_health_check(self):
        if self.health_check_task is None:
            logger.info("Starting SSH connection health checker.")
            self.health_check_task = asyncio.create_task(self._health_check())

    def stop_health_check(self):
        if self.health_check_task:
            logger.info("Stopping SSH connection health checker.")
            self.health_check_task.cancel()
            self.health_check_task = None
