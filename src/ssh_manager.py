import asyncio
import asyncssh
from .database import get_all_servers

class SSHConnection:
    def __init__(self, config):
        self.config = config
        self.conn = None
        self.shell_process = None

    async def connect(self):
        if self.is_connected():
            return

        attempts = 3
        delay = 2
        for i in range(attempts):
            try:
                connect_args = {
                    'username': self.config['user'],
                    'password': self.config.get('password'),
                    'client_keys': [self.config['key_path']] if self.config.get('key_path') else None
                }
                # Remove None values
                connect_args = {k: v for k, v in connect_args.items() if v is not None}

                self.conn = await asyncssh.connect(self.config['hostname'], **connect_args)
                return

            except Exception as e:
                if i < attempts - 1:
                    await asyncio.sleep(delay * (i + 1))
                else:
                    self.conn = None
                    raise ConnectionError(f"Failed to connect to {self.config['alias']} after {attempts} attempts: {e}")

    def is_connected(self):
        return self.conn is not None and not self.conn.is_closing()

    async def run_command(self, command):
        if not self.is_connected():
            await self.connect()

        async with self.conn.create_process(command) as process:
            async for line in process.stdout:
                yield line, 'stdout'
            async for line in process.stderr:
                yield line, 'stderr'

    async def start_shell(self):
        """Starts a persistent shell process."""
        if not self.is_connected():
            await self.connect()
        if self.shell_process:
            return

        self.shell_process = await self.conn.create_process(term_type='xterm')

    async def run_in_shell(self, command):
        """Runs a command in the persistent shell."""
        if not self.shell_process:
            await self.start_shell()

        self.shell_process.stdin.write(command + '\n')

        output = ""
        while True:
            try:
                line = await asyncio.wait_for(self.shell_process.stdout.readline(), timeout=1.0)
                if not line:
                    break
                output += line
            except asyncio.TimeoutError:
                break
        return output

    async def close(self):
        if self.shell_process:
            self.shell_process.terminate()
            await self.shell_process.wait()
            self.shell_process = None
        if self.conn:
            self.conn.close()
            await self.conn.wait_closed()

class SSHManager:
    def __init__(self):
        self.connections = {}
        self.server_configs = {s['alias']: s for s in get_all_servers()}

    def get_server_config(self, alias):
        return self.server_configs.get(alias)

    def refresh_server_configs(self):
        self.server_configs = {s['alias']: s for s in get_all_servers()}

    async def get_connection(self, alias):
        if alias not in self.connections:
            server_config = self.get_server_config(alias)
            if not server_config:
                raise ValueError(f"Server with alias '{alias}' not found in the database.")
            self.connections[alias] = SSHConnection(server_config)

        conn = self.connections[alias]
        if not conn.is_connected():
            await conn.connect()
        return conn

    async def disconnect(self, alias):
        if alias in self.connections:
            await self.connections[alias].close()
            del self.connections[alias]

    async def disconnect_all(self):
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
