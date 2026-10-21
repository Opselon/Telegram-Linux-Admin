"""Tests for the runtime package surface (src/runtime/__init__.py, target.py).

PURPOSE:
    Lock the public contract: the names the orchestrator wires exist with the
    exact CONTRACT.md spelling, the protocol is structural, TargetInfo is
    immutable identity, and importing the package is side-effect free.

OWNER:
    lane runtime (src/runtime/**, tests/test_runtime_*.py)
"""

from __future__ import annotations

import dataclasses
from typing import get_type_hints

import pytest

import src.runtime as rt
from src.runtime import (
    DEFAULT_MAX_BYTES,
    DEFAULT_TIMEOUT,
    ExecutionTarget,
    LocalTarget,
    RemoteTarget,
    TargetError,
    TargetInfo,
    TargetKind,
    TargetResolver,
)


def test_public_names_match_contract():
    """Every name the orchestrator wires is exported with exact spelling."""
    expected = {
        "TargetKind",
        "TargetInfo",
        "ExecutionTarget",
        "TargetError",
        "LocalTarget",
        "RemoteTarget",
        "TargetResolver",
        "LOCAL_ALIAS",
    }
    assert expected <= set(rt.__all__)
    for name in expected:
        assert hasattr(rt, name), name


def test_target_kind_values():
    """kind values are the lowercase strings the UI serializes."""
    assert TargetKind.LOCAL.value == "local"
    assert TargetKind.REMOTE.value == "remote"
    assert TargetKind("local") is TargetKind.LOCAL


def test_target_info_is_frozen():
    """TargetInfo is identity: mutation must be refused."""
    info = TargetInfo(alias="web1", hostname="10.0.0.5", user="oracle", kind=TargetKind.REMOTE)
    with pytest.raises(dataclasses.FrozenInstanceError):
        info.alias = "other"  # type: ignore[misc]


def test_target_info_defaults_description_empty():
    """An absent description is an empty string, never None."""
    info = TargetInfo(alias="a", hostname="h", user="u", kind=TargetKind.REMOTE)
    assert info.description == ""


def test_execution_target_protocol_is_structural():
    """Both adapters satisfy ExecutionTarget without inheriting it."""
    manager = _FakeManager()
    assert isinstance(LocalTarget(allow=True), ExecutionTarget)
    assert isinstance(RemoteTarget(manager, 1, "web1"), ExecutionTarget)
    # A bare object does not.
    assert not isinstance(object(), ExecutionTarget)


def test_execution_target_protocol_signature():
    """run() keeps the CONTRACT.md keyword-only shape."""
    hints = get_type_hints(LocalTarget.run)
    assert hints.get("return") is not None
    assert "timeout" in hints
    assert "max_bytes" in hints


def test_default_max_bytes_is_two_mib():
    """The cap default equals the SSH manager's, so parity needs no extra config."""
    assert DEFAULT_MAX_BYTES == 2 * 1024 * 1024
    assert DEFAULT_TIMEOUT == 60.0


def test_import_is_idempotent():
    """Re-importing yields the same module object (no duplicate initialization)."""
    import importlib

    assert importlib.import_module("src.runtime") is rt


def test_package_file_layout():
    """The package exposes the three backend modules under stable names."""
    import src.runtime.local as local_mod
    import src.runtime.remote as remote_mod
    import src.runtime.resolver as resolver_mod

    assert local_mod.LocalTarget is LocalTarget
    assert remote_mod.RemoteTarget is RemoteTarget
    assert resolver_mod.TargetResolver is TargetResolver


def test_target_error_is_runtime_error():
    """TargetError subclasses RuntimeError so broad handlers still catch it."""
    assert issubclass(TargetError, RuntimeError)


class _FakeManager:
    async def run_command(  # noqa: ASYNC109
        self,
        owner_id: int,
        alias: str,
        command: str,
        timeout: float = 60.0,  # noqa: ASYNC109 - mirrors SSHManager's signature
        max_bytes: int = 2 * 1024 * 1024,
    ):
        yield "1", "pid"
