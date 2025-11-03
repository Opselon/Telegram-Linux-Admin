import asyncio
import asyncssh
import json

class SSHManager:
    def __init__(self, config_path='config.json'):
        with open(config_path, 'r') as f:
            self.config = json.load(f)
        self.connections = {}

    def get_server_config(self, alias):
        for server in self.config['servers']:
            if server['alias'] == alias:
                return server
        return None

    async def connect(self, alias):
        if alias in self.connections:
            return self.connections[alias]

        server_config = self.get_server_config(alias)
        if not server_config:
            raise ValueError(f"Server with alias '{alias}' not found in config.")

        try:
            conn = await asyncssh.connect(
                server_config['hostname'],
                username=server_config['user'],
                client_keys=[server_config['key_path']]
            )
            self.connections[alias] = conn
            return conn
        except Exception as e:
            raise ConnectionError(f"Failed to connect to {alias}: {e}")

    async def disconnect(self, alias):
        if alias in self.connections:
            self.connections[alias].close()
            await self.connections[alias].wait_closed()
            del self.connections[alias]

    async def run_command(self, alias, command):
        if alias not in self.connections:
            await self.connect(alias)

        conn = self.connections[alias]
        async with conn.create_process(command) as process:
            async for line in process.stdout:
                yield line, 'stdout'
            async for line in process.stderr:
                yield line, 'stderr'

    def get_connected_servers(self):
        return list(self.connections.keys())
