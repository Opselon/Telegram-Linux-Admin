"""Local transport tests: real subprocesses, no mocks.

These run actual commands on the test host — the point is that the adapter is
verified against a real shell, including exit codes, timeouts and the output
cap, rather than against a fake that cannot fail the way a real host does.
"""

from __future__ import annotations

import asyncio

import pytest

from src.domain.targets import local_target
from src.infrastructure.local_transport import LocalTransport

TARGET = local_target(1)


async def collect(transport: LocalTransport, command: str) -> tuple[str, str, list[str]]:
    out: list[str] = []
    err: list[str] = []
    streams: list[str] = []
    async for chunk, stream in transport.run(TARGET, command):
        streams.append(stream)
        if stream == "stdout":
            out.append(chunk)
        elif stream == "stderr":
            err.append(chunk)
    return "".join(out), "".join(err), streams


@pytest.mark.asyncio
async def test_execute_returns_real_output_and_exit_code():
    transport = LocalTransport(timeout=10)
    out, err, code = await transport.execute("printf 'alpha'; printf 'beta' >&2; exit 7")
    assert out == "alpha"
    assert "beta" in err
    assert code == 7


@pytest.mark.asyncio
async def test_stream_yields_pid_then_output_then_exit():
    transport = LocalTransport(timeout=10)
    _, _, streams = await collect(transport, "printf stamped")
    assert streams[0] == "pid"
    assert "stdout" in streams
    assert "exit" in streams


@pytest.mark.asyncio
async def test_quotes_and_unicode_survive_without_shell_injection():
    transport = LocalTransport(timeout=10)
    out, _, code = await transport.execute("printf '%s' \"it's \\\"quoted\\\" — فارسی\"")
    assert "it's" in out and "فارسی" in out
    assert code == 0


@pytest.mark.asyncio
async def test_stderr_is_streamed_separately_from_stdout():
    transport = LocalTransport(timeout=10)
    out, err, _ = await collect(transport, "echo out; echo err >&2")
    assert "out" in out and "err" not in out
    assert "err" in err


@pytest.mark.asyncio
async def test_timeout_kills_the_process_group():
    transport = LocalTransport(timeout=0.5)
    started = asyncio.get_running_loop().time()
    out, err, _ = await collect(transport, "sleep 30")
    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed < 8, "timeout must not wait for the child"
    assert "timed out" in (out + err).lower()


@pytest.mark.asyncio
async def test_output_cap_stops_a_flood():
    transport = LocalTransport(timeout=20, max_bytes=4096)
    with pytest.raises(RuntimeError):
        async for _ in transport.run(TARGET, "yes flood | head -c 200000"):
            pass


@pytest.mark.asyncio
async def test_disconnect_is_a_noop():
    transport = LocalTransport()
    assert await transport.disconnect(TARGET) is None
