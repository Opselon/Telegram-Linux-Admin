"""Tests for remote execution (src/runtime/remote.py).

PURPOSE:
    Prove RemoteTarget is a thin, honest adapter: it delegates the whole
    streaming contract to the SSHManager-like object it is handed, forwards
    timeout and byte cap untouched, never swallows manager exceptions, and
    refuses to run after close.

OWNER:
    lane runtime (src/runtime/**, tests/test_runtime_*.py)
"""

from __future__ import annotations

import pytest

from src.runtime.remote import RemoteTarget
from src.runtime.target import TargetError, TargetInfo, TargetKind
from src.ssh_manager import CommandOutputTooLarge

pytestmark = pytest.mark.asyncio


class FakeSSHManager:
    """Minimal SSHManager stand-in: records calls, replays scripted tuples."""

    def __init__(self, script: list[tuple[str, str]] | None = None, error: BaseException | None = None):
        self.calls: list[tuple[int, str, str, float, int]] = []
        self.script = script or []
        self.error = error

    async def run_command(  # noqa: ASYNC109
        self,
        owner_id: int,
        alias: str,
        command: str,
        timeout: float = 60.0,  # noqa: ASYNC109 - mirrors SSHManager's signature
        max_bytes: int = 2 * 1024 * 1024,
    ):
        self.calls.append((owner_id, alias, command, timeout, max_bytes))
        if self.error is not None:
            raise self.error
        for item in self.script:
            yield item


def _target(script=None, error=None):
    manager = FakeSSHManager(script, error)
    return RemoteTarget(manager, 42, "web1", hostname="10.0.0.5", user="oracle"), manager


async def test_delegates_and_forwards_all_args():
    """owner_id, alias, command, timeout and max_bytes must reach the manager."""
    target, manager = _target()
    async for _ in target.run("uptime", timeout=9.5, max_bytes=11):
        pass
    assert manager.calls == [(42, "web1", "uptime", 9.5, 11)]


async def test_pid_first_parity_is_not_crept():
    """The pid-first tuple is the manager's contract; the adapter must not mutate it."""
    script = [("123", "pid"), ("out", "stdout"), ("err", "stderr")]
    target, _ = _target(script)
    got = [item async for item in target.run("x")]
    assert got == script
    assert got[0][1] == "pid"


async def test_named_chunks_pass_through_unchanged():
    """Every chunk/stream pair must arrive unchanged in order."""
    script = [("a", "stdout"), ("b", "stdout"), ("c", "stderr")]
    target, _ = _target(script)
    assert [item async for item in target.run("x")] == script


async def test_manager_exception_propagates():
    """Adapter is a delegator: manager errors (auth, transport) surface unchanged."""
    manager = FakeSSHManager(error=ConnectionRefusedError("refused"))
    target = RemoteTarget(manager, 1, "api")
    with pytest.raises(ConnectionRefusedError):
        async for _ in target.run("x"):
            pass


async def test_host_key_mismatch_propagates():
    """Security errors must never be translated into user-looking messages."""
    target, _ = _target(error=RuntimeError("Host key mismatch for 'web1': expected A, got B"))
    with pytest.raises(RuntimeError, match="Host key mismatch"):
        async for _ in target.run("x"):
            pass


async def test_command_output_too_large_propagates():
    """The byte-cap exception keeps its type and message through the adapter."""
    err = CommandOutputTooLarge("Command produced more than 2048 bytes of output.")
    target, _ = _target(error=err)
    with pytest.raises(CommandOutputTooLarge) as exc:
        async for _ in target.run("x", max_bytes=2048):
            pass
    assert str(exc.value) == "Command produced more than 2048 bytes of output."


async def test_empty_command_refuses():
    """An empty command is a caller bug; throw before touching the manager."""
    target, manager = _target()
    with pytest.raises(TargetError):
        async for _ in target.run("   "):
            pass
    assert manager.calls == []


async def test_run_after_close_refuses():
    """A closed remote target must not start a manager call."""
    target, manager = _target()
    await target.close()
    with pytest.raises(TargetError):
        async for _ in target.run("x"):
            pass
    assert manager.calls == []


async def test_close_does_not_touch_manager_pool():
    """Closing one target must not close the shared SSH pool for others."""
    target, _ = _target()
    await target.close()
    assert target.info.kind is TargetKind.REMOTE


async def test_manager_none_rejected():
    """A None manager is a wiring bug; raise at construction, not at use."""
    with pytest.raises(ValueError):
        RemoteTarget(None, 1, "web1")


async def test_blank_alias_rejected():
    """Alias-less targets cannot be audited or addressed; refuse at build time."""
    with pytest.raises(ValueError):
        RemoteTarget(FakeSSHManager(), 1, "  ")


async def test_info_preserves_alias_and_host():
    """Displayed identity comes from what was passed, nothing invented."""
    target, _ = _target()
    assert isinstance(target.info, TargetInfo)
    assert target.info.alias == "web1"
    assert target.info.hostname == "10.0.0.5"
    assert target.info.user == "oracle"


async def test_info_defaults_empty_when_unknown():
    """An adapter built without hints reports empty strings, not placeholder lies."""
    target = RemoteTarget(FakeSSHManager(), 7, "db1")
    assert target.info.hostname == ""
    assert target.info.user == ""


async def test_owner_id_and_alias_readable():
    """Callers wrap cancellation around (owner_id, alias); make it cheap to read."""
    target, _ = _target()
    assert target.owner_id == 42
    assert target.alias == "web1"


async def test_iterator_reuse_requires_new_call():
    """A second run() starts a fresh manager call, not a stale generator."""
    script = [("x", "stdout")]
    target, manager = _target(script)
    await drain_and_check(target, "one")
    await drain_and_check(target, "two")
    assert len(manager.calls) == 2


async def drain_and_check(target, command):
    """Consume one full run; asserts at least one chunk came back."""
    chunks = [item async for item in target.run(command)]
    assert chunks
