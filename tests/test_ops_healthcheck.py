"""Unit tests for src/ops/healthcheck.py (wave 3.3 lane ops).

Each behavioral claim gets its own test: the ok/explanations shape, one
failure mode per check (database unreachable, audit dir unwritable, bad
token shape) each flipping ``ok`` to False and adding one explanation,
and the Telegram HEAD probe being skipped by default (no network in
tests — contract hard law 4 and the lane's "no network in tests" rule).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import src.database as database
from src.config import Config
from src.ops.healthcheck import Check, DeepReadiness, deep_readiness


@pytest.fixture(autouse=True)
def _isolated_world(tmp_path: Any) -> Any:
    """Gives each test a fresh DB and audit dir under tmp_path."""
    old_db = database._conn_path
    old_conn = database._conn
    database._conn = None
    database._conn_path = None
    import os

    os.environ["TLA_DB_FILE"] = str(tmp_path / "probe.db")
    database.close_db_connection()
    yield tmp_path
    database.close_db_connection()
    database._conn = old_conn
    database._conn_path = old_db


class _FakeAuditLogger:
    """Duck-typed audit logger with controllable writability."""

    def __init__(self, writable: bool) -> None:
        self._writable = writable

    def is_writable(self) -> bool:
        return self._writable


class _ConfigLike:
    """Minimal duck-typed Config the probe can read a token off."""

    def __init__(self, token: str) -> None:
        self.telegram_token = token


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _check_names(result: DeepReadiness) -> list[str]:
    return [c.name for c in result.checks]


# -- shape -----------------------------------------------------------------
def test_result_shape_ok_and_explanations() -> None:
    result = _run(
        deep_readiness(
            _ConfigLike("123456:ABCdefGhIJKlmNoPQRstUVwxyZ"),
            audit_logger=_FakeAuditLogger(True),
        )
    )
    # JSON-safe projection exists
    assert isinstance(_check_names(result), list)
    assert _check_names(result) == ["database", "audit_dir", "token", "telegram_probe"]
    assert result.ok is True
    assert result.explanations == []  # ok => nothing to explain
    for c in result.checks:
        assert isinstance(c, Check)
        assert set(c.__dataclass_fields__) == {"name", "ok", "detail"}


def test_all_checks_run_even_when_db_fails(tmp_path: Any) -> None:
    """One broken check must not short-circuit the rest."""
    import os

    database.close_db_connection()
    # A DB path whose PARENT is an existing FILE: mkdir(parent) fails on
    # every OS, so the database check is the failing one on linux AND
    # windows (a windows-reserved "nul" prefix is inert on POSIX).
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    impossible = str(blocker / "sub" / "probe.db")
    os.environ["TLA_DB_FILE"] = impossible
    database._conn = None
    database._conn_path = None

    result = _run(
        deep_readiness(
            _ConfigLike("123456:BAD"),
            audit_logger=_FakeAuditLogger(True),
        )
    )
    names = _check_names(result)
    assert names == ["database", "audit_dir", "token", "telegram_probe"]
    assert result.ok is False
    assert any(c.name == "database" and not c.ok for c in result.checks)
    assert any(c.name == "audit_dir" and c.ok for c in result.checks)
    # "123456:BAD" is valid token shape, so the database is the only
    # failing check and the explanation list has exactly one entry.
    assert len(result.explanations) == 1


# -- per-check failure flipping ok -----------------------------------------
def test_db_failure_flips_ok(tmp_path: Any) -> None:
    import os

    database.close_db_connection()
    # Point the db into a directory we can never create on Windows.
    impossible = "\\\\.\\nul\\cannot\\mkdir\\here\\probe.db" if os.name == "nt" else "/dev/null/impossible/probe.db"
    os.environ["TLA_DB_FILE"] = impossible
    database._conn = None
    database._conn_path = None
    result = _run(
        deep_readiness(
            _ConfigLike("123456:ABCdefGhIJKlmNoPQRstUVwxyZ"),
            audit_logger=_FakeAuditLogger(True),
        )
    )
    db = next(c for c in result.checks if c.name == "database")
    assert db.ok is False
    assert db.detail
    assert result.ok is False
    assert any("database" in e for e in result.explanations)


def test_audit_dir_failure_flips_ok() -> None:
    result = _run(
        deep_readiness(
            _ConfigLike("123456:ABCdefGhIJKlmNoPQRstUVwxyZ"),
            audit_logger=_FakeAuditLogger(False),
        )
    )
    audit_check = next(c for c in result.checks if c.name == "audit_dir")
    assert audit_check.ok is False
    assert result.ok is False
    assert any("audit_dir" in e for e in result.explanations)


def test_bad_token_shape_flips_ok_without_echoing_secret() -> None:
    secret = "ABCdefGhIJKlmNoPQRstUVwxyZ-secret-part"
    result = _run(
        deep_readiness(
            _ConfigLike(f"not-a-token {secret}"),
            audit_logger=_FakeAuditLogger(True),
        )
    )
    token_check = next(c for c in result.checks if c.name == "token")
    assert token_check.ok is False
    assert result.ok is False
    assert any("token" in e for e in result.explanations)
    for c in result.checks:
        assert secret not in c.detail, "detail must never echo secret material"
    for e in result.explanations:
        assert secret not in e, "explanations must never echo secret material"


def test_missing_token_flips_ok() -> None:
    result = _run(
        deep_readiness(Config.__new__(Config), audit_logger=_FakeAuditLogger(True))
    )
    token_check = next(c for c in result.checks if c.name == "token")
    assert token_check.ok is False
    assert "no telegram token" in token_check.detail
    assert result.ok is False


# -- probe gating ----------------------------------------------------------
def test_telegram_probe_skipped_by_default() -> None:
    """Default: no network. The probe check records it as skipped, not failed."""
    result = _run(
        deep_readiness(
            _ConfigLike("123456:ABCdefGhIJKlmNoPQRstUVwxyZ"),
            audit_logger=_FakeAuditLogger(True),
        )  # telegram_probe left at its False default
    )
    probe = next(c for c in result.checks if c.name == "telegram_probe")
    assert probe.ok is True
    assert "skipped" in probe.detail
    assert "disabled by default" in probe.detail


def test_telegram_probe_without_token_fails_cleanly() -> None:
    result = _run(
        deep_readiness(
            Config.__new__(Config),
            audit_logger=_FakeAuditLogger(True),
            telegram_probe=True,
        )
    )
    probe = next(c for c in result.checks if c.name == "telegram_probe")
    assert probe.ok is False
    assert "no token" in probe.detail
