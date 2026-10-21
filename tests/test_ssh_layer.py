"""Tests for the hardened SSH layer: command envelope, host key pinning, pooling."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys

import pytest

from src import ssh_manager as ssh_module
from src.ssh_manager import HostKeyMismatch, SSHManager, wrap_remote_command

#: The envelope tests execute locally what the REMOTE host runs. That needs a
#: POSIX bash on the machine driving the test: Windows runners have none (the
#: bot itself is defined for remote Linux servers), so there they are skipped
#: with the reason visible instead of failing the matrix.
_bash_available = shutil.which("bash") is not None or shutil.which("sh") is not None
_envelope_skip = pytest.mark.skipif(
    os.name == "nt" and not _bash_available,
    reason="local bash required to execute the remote-command envelope",
)


# ---------------------------------------------------------------------------
# Command envelope (executed, not mocked)
# ---------------------------------------------------------------------------
def _run_envelope(command: str) -> str:
    """Runs the generated envelope locally through bash and returns stdout."""
    result = subprocess.run(
        ["bash", "-c", wrap_remote_command(command)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    # First line is the PID echoed by the envelope.
    assert lines and lines[0].strip().isdigit()
    return "\n".join(lines[1:])


@_envelope_skip
class TestRemoteCommandEnvelope:
    def test_plain_command(self):
        assert _run_envelope("echo hello world").strip() == "hello world"

    def test_single_quotes_survive(self):
        assert _run_envelope("echo \"it's fine\"").strip() == "it's fine"

    def test_shell_metacharacters_are_not_expanded_locally(self):
        out = _run_envelope("printf '%s\\n' 'a;b|c&d' '$HOME' '`id`'")
        assert "a;b|c&d" in out
        assert "$HOME" in out
        assert "`id`" in out

    def test_multiline_and_unicode_payload(self):
        out = _run_envelope("printf 'سلام\\nworld\\n'")
        assert "سلام" in out
        assert "world" in out

    def test_nested_quotes_survive(self):
        # The old envelope interpolated the raw command inside single quotes, so
        # any apostrophe produced a syntax error. The base64 envelope transports
        # balanced quotes exactly as written.
        assert _run_envelope("bash -c 'echo \"inner quote\"'").strip() == "inner quote"


# ---------------------------------------------------------------------------
# Host key pinning
# ---------------------------------------------------------------------------
class FakeKey:
    def __init__(self, fingerprint: str) -> None:
        self._fingerprint = fingerprint

    def get_fingerprint(self, hash_name: str = "sha256") -> str:
        return self._fingerprint


class FakeConnection:
    def __init__(self, fingerprint: str = "SHA256:pin") -> None:
        self._fingerprint = fingerprint
        self.closed = False

    def get_server_host_key(self):
        return FakeKey(self._fingerprint)

    def is_closed(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


@pytest.fixture()
def pinning(monkeypatch):
    """Isolates the pin store and records pins written by the manager."""
    store: dict[tuple[int, str], str | None] = {}
    writes: list[tuple[int, str, str]] = []

    monkeypatch.setattr(ssh_module, "get_server_host_key", lambda o, a: store.get((o, a)))
    monkeypatch.setattr(
        ssh_module,
        "set_server_host_key",
        lambda o, a, fp: (writes.append((o, a, fp)), store.__setitem__((o, a), fp)),
    )
    monkeypatch.setattr(ssh_module, "HOST_KEY_POLICY", "tofu")
    return store, writes


class TestHostKeyPinning:
    @pytest.mark.asyncio
    async def test_first_connection_pins_fingerprint(self, pinning):
        store, writes = pinning
        manager = SSHManager()
        conn = FakeConnection("SHA256:abc")
        await manager._verify_host_key(1, "web", conn)
        assert writes == [(1, "web", "SHA256:abc")]
        assert store[(1, "web")] == "SHA256:abc"

    @pytest.mark.asyncio
    async def test_matching_fingerprint_is_accepted(self, pinning):
        store, writes = pinning
        store[(1, "web")] = "SHA256:abc"
        manager = SSHManager()
        await manager._verify_host_key(1, "web", FakeConnection("SHA256:abc"))
        assert writes == []

    @pytest.mark.asyncio
    async def test_changed_fingerprint_is_refused(self, pinning):
        store, _ = pinning
        store[(1, "web")] = "SHA256:abc"
        manager = SSHManager()
        with pytest.raises(HostKeyMismatch) as exc:
            await manager._verify_host_key(1, "web", FakeConnection("SHA256:evil"))
        assert exc.value.expected == "SHA256:abc"
        assert exc.value.actual == "SHA256:evil"

    @pytest.mark.asyncio
    async def test_strict_mode_refuses_unknown_hosts(self, pinning, monkeypatch):
        monkeypatch.setattr(ssh_module, "HOST_KEY_POLICY", "strict")
        manager = SSHManager()
        with pytest.raises(HostKeyMismatch):
            await manager._verify_host_key(1, "web", FakeConnection("SHA256:new"))

    @pytest.mark.asyncio
    async def test_insecure_mode_skips_verification(self, pinning, monkeypatch):
        _, writes = pinning
        monkeypatch.setattr(ssh_module, "HOST_KEY_POLICY", "insecure")
        manager = SSHManager()
        await manager._verify_host_key(1, "web", FakeConnection("SHA256:whatever"))
        assert writes == []


# ---------------------------------------------------------------------------
# Connection pool
# ---------------------------------------------------------------------------
class TestConnectionPool:
    @pytest.mark.asyncio
    async def test_connections_are_reused(self, monkeypatch):
        manager = SSHManager()
        opened: list[str] = []

        async def fake_open(owner_id, alias):
            opened.append(alias)
            return FakeConnection()

        monkeypatch.setattr(manager, "_open_connection", fake_open)
        first = await manager._get_connection(1, "web")
        second = await manager._get_connection(1, "web")
        assert first is second
        assert opened == ["web"]

    @pytest.mark.asyncio
    async def test_connections_are_scoped_per_user(self, monkeypatch):
        manager = SSHManager()
        opened: list[tuple[int, str]] = []

        async def fake_open(owner_id, alias):
            opened.append((owner_id, alias))
            return FakeConnection()

        monkeypatch.setattr(manager, "_open_connection", fake_open)
        await manager._get_connection(1, "web")
        await manager._get_connection(2, "web")
        assert opened == [(1, "web"), (2, "web")]

    @pytest.mark.asyncio
    async def test_idle_connection_is_replaced(self, monkeypatch):
        manager = SSHManager()
        opened: list[str] = []

        async def fake_open(owner_id, alias):
            opened.append(alias)
            return FakeConnection()

        monkeypatch.setattr(manager, "_open_connection", fake_open)
        monkeypatch.setattr(ssh_module, "IDLE_TIMEOUT", 0.0)
        first = await manager._get_connection(1, "web")
        second = await manager._get_connection(1, "web")
        assert first is not second
        assert opened == ["web", "web"]

    @pytest.mark.asyncio
    async def test_disconnect_evicts_pool_entry(self, monkeypatch):
        manager = SSHManager()

        async def fake_open(owner_id, alias):
            return FakeConnection()

        monkeypatch.setattr(manager, "_open_connection", fake_open)
        pooled = await manager._get_connection(1, "web")
        await manager.disconnect(1, "web")
        assert pooled.conn.closed is True
        assert await manager.pool_stats() == {"pooled": 0, "usable": 0, "shells": 0}


# ---------------------------------------------------------------------------
# Fingerprint helper
# ---------------------------------------------------------------------------
def test_fingerprint_uses_key_object():
    assert ssh_module.fingerprint_host_key(FakeKey("SHA256:xyz")) == "SHA256:xyz"


def test_fingerprint_falls_back_to_hash_for_raw_keys():
    fingerprint = ssh_module.fingerprint_host_key(b"raw-key-bytes")
    assert fingerprint.startswith("SHA256:")
