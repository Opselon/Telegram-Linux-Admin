import asyncio
import asyncssh
from .database import get_all_servers

class SSHConnection:
    def __init__(self, config):
        self.config = config
        self.conn = None

    async def connect(self):
        if self.is_connected():
            return
        try:
            self.conn = await asyncssh.connect(
                self.config['hostname'],
                username=self.config['user'],
                client_keys=[self.config['key_path']]
            )
        except Exception as e:
            self.conn = None
            raise ConnectionError(f"Failed to connect to {self.config['alias']}: {e}")

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

    async def close(self):
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
