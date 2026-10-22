"""Local shell tests: session persistence, marker isolation, teardown."""

from __future__ import annotations

import asyncio

import pytest

from src.infrastructure.local_shell import LocalShellPool


@pytest.mark.asyncio
async def test_shell_keeps_cwd_and_environment_between_commands():
    pool = LocalShellPool()
    try:
        first = await pool.run(1, "cd /tmp && export TLA_MARK=kept")
        assert first == ""
        second = await pool.run(1, "pwd; echo mark=$TLA_MARK")
        assert "/tmp" in second
        assert "mark=kept" in second
    finally:
        await pool.close_all()


@pytest.mark.asyncio
async def test_command_output_cannot_forge_the_end_marker():
    pool = LocalShellPool()
    try:
        # The command prints the marker prefix on purpose; only the real marker
        # (with its per-command random token) may terminate the read.
        out = await pool.run(1, "echo __TLA_END_ deadbeef:0")
        assert "__TLA_END_ deadbeef:0" in out
    finally:
        await pool.close_all()


@pytest.mark.asyncio
async def test_sessions_are_isolated_per_user():
    pool = LocalShellPool()
    try:
        await pool.run(1, "cd /tmp && export WHO=one")
        await pool.run(2, "cd / && export WHO=two")
        assert "/tmp" in await pool.run(1, "pwd")
        assert await pool.run(2, "pwd") == "/\n"
        assert "two" in await pool.run(2, "echo $WHO")
    finally:
        await pool.close_all()


@pytest.mark.asyncio
async def test_timeout_tears_the_session_down_so_output_cannot_leak():
    pool = LocalShellPool()
    try:
        session = await pool.start(5)
        with pytest.raises(asyncio.TimeoutError):
            await session.run("sleep 10", timeout=0.3)
        assert pool.active(5) is False
        # The next command gets a clean shell, never the abandoned output.
        assert (await pool.run(5, "echo fresh")).strip() == "fresh"
    finally:
        await pool.close_all()


@pytest.mark.asyncio
async def test_close_reaps_the_process_and_reports_no_active_sessions():
    pool = LocalShellPool()
    session = await pool.start(3)
    pid = session.process.pid
    assert pool.active_count() == 1
    await pool.close(3)
    assert pool.active_count() == 0
    assert session.process.returncode is not None
    with pytest.raises(ProcessLookupError):
        import os

        os.kill(pid, 0)


@pytest.mark.asyncio
async def test_exit_ends_the_session_and_next_command_gets_a_fresh_shell():
    pool = LocalShellPool()
    try:
        with pytest.raises(RuntimeError):
            await pool.run(9, "exit 0")
        assert pool.active(9) is False
        out = await pool.run(9, "echo back")
        assert "back" in out
    finally:
        await pool.close_all()
