"""Routing manager tests: one API, two target kinds, policy enforced."""

from __future__ import annotations

import os

import pytest

from src.domain.errors import TargetAccessDenied
from src.domain.policies import TargetAccessPolicy
from src.domain.targets import LOCAL_ALIAS
from src.infrastructure.local_shell import LocalShellPool
from src.infrastructure.local_transport import LocalTransport
from src.infrastructure.routing_manager import RoutingCommandManager


class FakeSSH:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def run_command(self, owner_id, alias, command, **kwargs):
        self.calls.append(("run", owner_id, alias, command, kwargs))
        yield ("ssh-out", "stdout")

    async def kill_process(self, owner_id, alias, pid):
        self.calls.append(("kill", alias, pid))

    async def start_shell_session(self, owner_id, alias):
        self.calls.append(("shell", alias))

    async def run_command_in_shell(self, owner_id, alias, command):
        self.calls.append(("shell-run", alias, command))
        return "ssh-shell-out"

    async def download_file(self, owner_id, alias, remote, local):
        self.calls.append(("download", alias, remote, local))

    async def upload_file(self, owner_id, alias, local, remote):
        self.calls.append(("upload", alias, local, remote))

    async def disconnect(self, owner_id, alias):
        self.calls.append(("disconnect", alias))

    async def close_all_connections(self):
        self.calls.append(("close-all",))

    async def pool_stats(self):
        return {"connections": 0}


def manager(*, admin: bool = True, local_enabled: bool = True) -> tuple[RoutingCommandManager, FakeSSH]:
    ssh = FakeSSH()
    routed = RoutingCommandManager(
        ssh,
        local=LocalTransport(timeout=10),
        shell_pool=LocalShellPool(),
        policy=TargetAccessPolicy(local_enabled=local_enabled),
        is_admin=lambda _uid: admin,
    )
    return routed, ssh


@pytest.mark.asyncio
async def test_ssh_alias_goes_to_ssh_manager():
    routed, ssh = manager()
    chunks = [item async for item in routed.run_command(1, "web", "uptime")]
    assert chunks == [("ssh-out", "stdout")]
    assert ssh.calls[0][:4] == ("run", 1, "web", "uptime")


@pytest.mark.asyncio
async def test_self_host_alias_runs_locally_without_touching_ssh():
    routed, ssh = manager()
    out, err, code = await routed.execute(1, LOCAL_ALIAS, "printf local-hello")
    assert out == "local-hello"
    assert code == 0
    assert ssh.calls == []


@pytest.mark.asyncio
async def test_non_admin_cannot_reach_self_host():
    routed, ssh = manager(admin=False)
    with pytest.raises(TargetAccessDenied):
        await routed.execute(1, LOCAL_ALIAS, "id")
    assert ssh.calls == []


@pytest.mark.asyncio
async def test_disabled_feature_raises_before_execution():
    routed, _ = manager(local_enabled=False)
    with pytest.raises(Exception) as info:
        await routed.execute(1, LOCAL_ALIAS, "id")
    assert info.value.__class__.__name__ == "TargetDisabled"


@pytest.mark.asyncio
async def test_kill_process_local_terminates_a_real_process():
    routed, ssh = manager()
    out, _, _ = await routed.execute(1, LOCAL_ALIAS, "sleep 30 & echo $!")
    pid = int(out.strip())
    await routed.kill_process(1, LOCAL_ALIAS, pid)
    assert ssh.calls == []
    assert not os.path.exists(f"/proc/{pid}"), "process group should be gone"


@pytest.mark.asyncio
async def test_local_shell_session_is_persistent_and_separate_from_ssh():
    routed, ssh = manager()
    await routed.start_shell_session(1, LOCAL_ALIAS)
    await routed.run_command_in_shell(1, LOCAL_ALIAS, "cd /tmp")
    pwd = await routed.run_command_in_shell(1, LOCAL_ALIAS, "pwd")
    assert "/tmp" in pwd
    assert ssh.calls == []
    await routed.close_shell(1)


@pytest.mark.asyncio
async def test_file_transfer_is_a_copy_for_local_target(tmp_path):
    routed, ssh = manager()
    source = tmp_path / "source.txt"
    source.write_text("payload", encoding="utf-8")
    destination = tmp_path / "destination.txt"

    await routed.upload_file(1, LOCAL_ALIAS, str(source), str(destination))
    assert destination.read_text(encoding="utf-8") == "payload"

    copied_back = tmp_path / "copied.txt"
    await routed.download_file(1, LOCAL_ALIAS, str(destination), str(copied_back))
    assert copied_back.read_text(encoding="utf-8") == "payload"
    assert ssh.calls == []


@pytest.mark.asyncio
async def test_disconnect_local_closes_shell_and_ssh_delegates():
    routed, ssh = manager()
    await routed.start_shell_session(2, LOCAL_ALIAS)
    await routed.disconnect(2, LOCAL_ALIAS)
    await routed.disconnect(2, "web")
    assert ("disconnect", "web") in ssh.calls


@pytest.mark.asyncio
async def test_ssh_file_transfer_is_delegated():
    routed, ssh = manager()
    await routed.upload_file(1, "web", "/tmp/a", "/tmp/b")
    await routed.download_file(1, "web", "/tmp/b", "/tmp/c")
    assert ("upload", "web", "/tmp/a", "/tmp/b") in ssh.calls
    assert ("download", "web", "/tmp/b", "/tmp/c") in ssh.calls


@pytest.mark.asyncio
async def test_pool_stats_include_local_shells():
    routed, _ = manager()
    await routed.start_shell_session(4, LOCAL_ALIAS)
    stats = await routed.pool_stats()
    assert stats["local_shells"] == 1
    assert stats["connections"] == 0
    await routed.close_all_connections()
    stats = await routed.pool_stats()
    assert stats["local_shells"] == 0
