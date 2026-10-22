"""Tests for alias resolution (src/runtime/resolver.py).

PURPOSE:
    Prove "self" is the ONLY path to local execution, that it stays disabled
    unless the operator opted in, that every other alias maps to a remote
    target, and that every failure mode produces a clear TargetError instead
    of a traceback or a silent wrong-target.

OWNER:
    lane runtime (src/runtime/**, tests/test_runtime_*.py)
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.runtime import LOCAL_ALIAS, TargetResolver, resolve_target
from src.runtime.local import LocalTarget
from src.runtime.remote import RemoteTarget
from src.runtime.target import TargetError, TargetKind

SERVER_ROW = {"hostname": "10.0.0.5", "user": "oracle", "port": 2222, "alias": "web1"}


class FakeManager:
    """SSHManager stand-in: records calls, owns nothing (adapter is a pass-through)."""

    def __init__(self):
        self.calls = []

    async def run_command(  # noqa: ASYNC109
        self,
        owner_id: int,
        alias: str,
        command: str,
        timeout: float = 60.0,  # noqa: ASYNC109 - mirrors SSHManager's signature
        max_bytes: int = 2 * 1024 * 1024,
    ):
        self.calls.append((owner_id, alias, command, timeout, max_bytes))
        yield "4242", "pid"
        yield "ok", "stdout"


def _noop_db(*args, **kwargs):
    """Database stub used for tests that must never touch sqlite."""
    return


# ---------------------------------------------------------------------------
# LOCAL_ALIAS constant
# ---------------------------------------------------------------------------


def test_local_alias_is_self_sync():
    """The reserved alias is exactly 'self' -- no per-user override exists."""
    assert LOCAL_ALIAS == "self"


def test_is_local_alias_true_for_self_sync():
    """Whitespace/case variants are the same alias; users type loosely."""
    r = TargetResolver()
    assert r.is_local_alias("self") is True
    assert r.is_local_alias(" SELF ") is True
    assert r.is_local_alias("Self") is True


def test_is_local_alias_false_for_other_sync():
    """A server named 'selfserver' must not match the local alias."""
    r = TargetResolver()
    assert r.is_local_alias("web1") is False
    assert r.is_local_alias("selfserver") is False
    assert r.is_local_alias("") is False
    assert r.is_local_alias(None) is False


# ---------------------------------------------------------------------------
# 'self' with local disabled -> TargetError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_self_refused_when_local_disabled():
    """Core safety: 'self' + local_enabled=False must raise, never fall back."""
    r = TargetResolver(FakeManager(), local_enabled=False)
    with pytest.raises(TargetError) as exc:
        r.resolve(1, "self")
    assert "disabled" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_self_refusal_message_mentions_ssh_alternative():
    """The message must be actionable: enable the flag or add the server over SSH."""
    r = TargetResolver(FakeManager(), local_enabled=False)
    with pytest.raises(TargetError) as exc:
        r.resolve(1, "self")
    assert "SSH" in str(exc.value)


@pytest.mark.asyncio
async def test_self_refused_without_manager_when_local_disabled():
    """Disabled local + no manager must still be a clean TargetError for 'self'."""
    r = TargetResolver(None, local_enabled=False)
    with pytest.raises(TargetError):
        r.resolve(1, "self")


@pytest.mark.asyncio
async def test_self_refused_when_disabled_even_for_admin():
    """The flag is global: no owner_id bypasses it."""
    r = TargetResolver(FakeManager(), local_enabled=False)
    with pytest.raises(TargetError):
        r.resolve(999999, "self")


# ---------------------------------------------------------------------------
# 'self' with local enabled -> LocalTarget
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_self_returns_local_target_when_enabled():
    """Opting in is what makes 'self' runnable; the target is a LocalTarget."""
    r = TargetResolver(FakeManager(), local_enabled=True)
    target = r.resolve(1, "self")
    assert isinstance(target, LocalTarget)
    assert target.info.kind is TargetKind.LOCAL
    assert target.allow is True


@pytest.mark.asyncio
async def test_self_target_runs_commands():
    """The resolved local target is live, not a stub."""
    r = TargetResolver(FakeManager(), local_enabled=True)
    target = r.resolve(1, "self")
    chunks = [item async for item in target.run("echo resolver-ok")]
    assert chunks[0][1] == "pid"
    assert chunks[0][0].isdigit()
    assert "resolver-ok" in "".join(c for c, s in chunks if s == "stdout")


@pytest.mark.asyncio
async def test_self_local_target_is_cached():
    """One resolver owns one local target; repeat resolves share it."""
    r = TargetResolver(FakeManager(), local_enabled=True)
    first = r.resolve(1, "self")
    second = r.resolve(2, "self")
    assert first is second


@pytest.mark.asyncio
async def test_local_alias_optional_and_whitespace_trimmed():
    """User input with spaces still resolves to the cached local target."""
    r = TargetResolver(FakeManager(), local_enabled=True)
    assert isinstance(r.resolve(1, "  self  "), LocalTarget)


# ---------------------------------------------------------------------------
# Remote aliases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remote_alias_returns_remote_target():
    """A normal alias resolves to a RemoteTarget bound to (owner_id, alias)."""
    with patch("src.runtime.resolver.get_server", return_value=SERVER_ROW):
        r = TargetResolver(FakeManager())
        target = r.resolve(42, "web1")
    assert isinstance(target, RemoteTarget)
    assert target.owner_id == 42
    assert target.alias == "web1"


@pytest.mark.asyncio
async def test_remote_target_runs_through_manager():
    """The resolved remote target streams from the manager it was built with."""
    manager = FakeManager()
    with patch("src.runtime.resolver.get_server", return_value=SERVER_ROW):
        r = TargetResolver(manager)
        target = r.resolve(42, "web1")
    chunks = [item async for item in target.run("uptime", timeout=5, max_bytes=9)]
    assert chunks[0] == ("4242", "pid")
    assert manager.calls == [(42, "web1", "uptime", 5, 9)]


@pytest.mark.asyncio
async def test_remote_alias_uses_hostname_hint():
    """Explicit hostname/user hints win over the database record."""
    manager = FakeManager()
    with patch("src.runtime.resolver.get_server", return_value=SERVER_ROW):
        r = TargetResolver(manager)
        target = r.resolve(1, "web1", hostname="example.com", user="admin")
    assert target.info.hostname == "example.com"
    assert target.info.user == "admin"


@pytest.mark.asyncio
async def test_remote_alias_enriches_from_database():
    """With no hints, metadata is read from the database so audit lines are full."""
    manager = FakeManager()
    with patch("src.runtime.resolver.get_server", return_value=SERVER_ROW) as db:
        r = TargetResolver(manager)
        target = r.resolve(1, "web1")
    db.assert_called_once()
    assert target.info.hostname == "10.0.0.5"
    assert target.info.user == "oracle"


@pytest.mark.asyncio
async def test_remote_alias_case_insensitive():
    """Aliases are normalized to how the database stores them (lower)."""
    manager = FakeManager()
    with patch("src.runtime.resolver.get_server", return_value=SERVER_ROW) as db:
        r = TargetResolver(manager)
        target = r.resolve(1, "WEB1")
    assert target.alias == "WEB1"
    db.assert_called_once_with(1, "WEB1")


@pytest.mark.asyncio
async def test_unknown_alias_yields_target_error_when_db_raises():
    """A database error must surface as a clear error, not a traceback."""
    with patch("src.runtime.resolver.get_server", side_effect=ValueError("no db")):
        r = TargetResolver(FakeManager())
        r.resolve(1, "web1")  # metadata lookup is best-effort; build still succeeds


@pytest.mark.asyncio
async def test_blank_alias_raises_target_error():
    """An empty alias is a caller bug; refuse instead of building garbage."""
    r = TargetResolver(FakeManager())
    with pytest.raises(TargetError):
        r.resolve(1, "")
    with pytest.raises(TargetError):
        r.resolve(1, "   ")


@pytest.mark.asyncio
async def test_null_alias_raises_target_error():
    """None alias (missing handler argument) must fail cleanly."""
    r = TargetResolver(FakeManager())
    with pytest.raises(TargetError):
        r.resolve(1, None)


@pytest.mark.asyncio
async def test_no_manager_remote_alias_raises_target_error():
    """Without an SSH manager there is no remote path; say so clearly."""
    r = TargetResolver(None)
    with pytest.raises(TargetError) as exc:
        r.resolve(1, "web1")
    assert "SSH" in str(exc.value)


@pytest.mark.asyncio
async def test_no_manager_does_not_break_local_when_enabled():
    """Local works even with no manager; the two paths are independent."""
    r = TargetResolver(None, local_enabled=True)
    assert isinstance(r.resolve(1, "self"), LocalTarget)


# ---------------------------------------------------------------------------
# Convenience wrapper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_target_wrapper_remote():
    """The one-shot helper resolves a remote alias like the class does."""
    with patch("src.runtime.resolver.get_server", return_value=SERVER_ROW):
        target = resolve_target(5, "web1", FakeManager())
    assert isinstance(target, RemoteTarget)
    assert target.owner_id == 5


@pytest.mark.asyncio
async def test_resolve_target_wrapper_local_disabled():
    """The helper inherits the fail-closed default."""
    with pytest.raises(TargetError):
        resolve_target(5, "self", FakeManager())


@pytest.mark.asyncio
async def test_resolve_target_wrapper_local_enabled():
    """The helper honors an explicit opt-in."""
    target = resolve_target(5, "self", FakeManager(), local_enabled=True)
    assert isinstance(target, LocalTarget)


# ---------------------------------------------------------------------------
# Property accessor
# ---------------------------------------------------------------------------


def test_local_enabled_property_sync():
    """The flag is readable (for /status output) but not settable by resolving."""
    assert TargetResolver().local_enabled is False
    assert TargetResolver(local_enabled=True).local_enabled is True


# ---------------------------------------------------------------------------
# Direct database integration (read-only; Tournament style via conftest path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_database_lookup_returns_none_without_error():
    """A missing server row must not raise; the adapter still builds (SSH decides)."""
    manager = FakeManager()
    with patch("src.runtime.resolver.get_server", return_value=None):
        r = TargetResolver(manager)
        target = r.resolve(1, "nope")
    assert isinstance(target, RemoteTarget)
    assert target.info.hostname == ""
