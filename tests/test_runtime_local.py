"""Tests for local execution on the bot host (src/runtime/local.py).

PURPOSE:
    Prove the local adapter is a drop-in for the SSH path: pid-first streaming,
    concurrent stdout/stderr, a timeout that kills the whole process group, the
    identical byte-cap exception message, and -- the safety core -- refusal to
    run anything unless the operator opted in.

OWNER:
    lane runtime (src/runtime/**, tests/test_runtime_*.py)

INVARIANTS exercised:
    * First yield is (str(pid), "pid").
    * Disabled by default: LocalTarget() raises TargetError.
    * Timeout yields "Error: Command timed out." on stderr (not an exception).
    * Over-cap output raises CommandOutputTooLarge with the SSH message.
    * A timeout or an abandoned generator kills the process group (the shell
      AND its children) on both POSIX (killpg) and Windows (taskkill /T).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.metrics import METRICS
from src.runtime.local import LocalTarget
from src.runtime.target import TargetError, TargetKind
from src.ssh_manager import CommandOutputTooLarge

pytestmark = pytest.mark.asyncio


async def drain(gen):
    """Consumes *gen* fully, returning (pid, stdout, stderr, first_stream)."""
    pid = None
    out: list[str] = []
    err: list[str] = []
    first_stream = None
    async for chunk, stream in gen:
        if first_stream is None:
            first_stream = stream
        if stream == "pid":
            pid = chunk
        elif stream == "stdout":
            out.append(chunk)
        else:
            err.append(chunk)
    return pid, "".join(out), "".join(err), first_stream


def _pid_alive(pid: int) -> bool:
    """True when *pid* still exists. POSIX uses signal 0; Windows OpenProcess."""
    if os.name == "posix":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:  # pragma: no cover - exists but not ours
            return True
        return True
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        still_active = ctypes.c_int32()
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(still_active))
            return bool(ok) and still_active.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 - probe must never fail the test  # pragma: no cover
        return False


def _beater_script(beat_file: str, helper_file: str) -> str:
    """A helper command line that appends to *beat_file* forever.

    Used to prove a kill reached the command's children, not just its shell:
    a surviving grandchild keeps growing the file after the run ends. The
    helper program lives in its own file -- embedding a multi-line program in
    a shell command line mangles quoting differently on every platform.
    """
    program = (
        "import sys, time\n"
        "p = sys.argv[1]\n"
        "while True:\n"
        "    with open(p, 'a') as f:\n"
        "        f.write('x'); f.flush()\n"
        "    time.sleep(0.02)\n"
    )
    Path(helper_file).write_text(program, encoding="utf-8")
    return f"'{sys.executable}' '{helper_file}' '{beat_file}' & wait"


async def _wait_until_dead(pid: int, rounds: int = 60) -> bool:
    """Polls until *pid* is gone; returns True when it died within the budget."""
    for _ in range(rounds):
        if not _pid_alive(pid):
            return True
        await asyncio.sleep(0.05)
    return False


# ---------------------------------------------------------------------------
# Refusal / disabled by default
# ---------------------------------------------------------------------------


async def test_refuses_when_not_allowed():
    """A default-constructed LocalTarget must fail closed, not silently run."""
    target = LocalTarget()
    assert target.allow is False
    with pytest.raises(TargetError):
        async for _ in target.run("echo hi"):  # pragma: no cover - never yields
            pass


async def test_refusal_message_is_actionable():
    """The refusal must tell the operator exactly how to enable the feature."""
    target = LocalTarget()
    with pytest.raises(TargetError) as exc:
        async for _ in target.run("echo hi"):  # pragma: no cover - never yields
            pass
    assert "local_enabled" in str(exc.value)


async def test_allow_true_executes():
    """Opting in is what unlocks execution; the key is explicit, never inferred."""
    target = LocalTarget(allow=True)
    pid, out, _, first = await drain(target.run("echo tla-local-ok"))
    assert first == "pid"
    assert pid is not None and pid.isdigit()
    assert "tla-local-ok" in out


async def test_info_reports_local_kind():
    """The target must identify itself as LOCAL so audit lines stay honest."""
    target = LocalTarget()
    assert target.info.kind is TargetKind.LOCAL
    assert target.info.alias


async def test_supported_is_true_on_this_host():
    """supported() answers without raising and is true on a usable host."""
    assert LocalTarget.supported() is True


async def test_run_after_close_refuses():
    """A closed target is dead; a later run must not resurrect it."""
    target = LocalTarget(allow=True)
    await target.close()
    with pytest.raises(TargetError):
        async for _ in target.run("echo hi"):  # pragma: no cover
            pass


async def test_close_is_idempotent():
    """Shutdown races close twice; the second call must be a no-op."""
    target = LocalTarget(allow=True)
    await target.close()
    await target.close()


async def test_empty_command_refuses():
    """An empty command is a caller bug; fail loudly instead of spawning a shell."""
    target = LocalTarget(allow=True)
    with pytest.raises(TargetError):
        async for _ in target.run("   "):  # pragma: no cover
            pass


# ---------------------------------------------------------------------------
# pid-first parity
# ---------------------------------------------------------------------------


async def test_pid_is_first_yield():
    """Parity with SSHManager.run_command: pid arrives before any output."""
    target = LocalTarget(allow=True)
    gen = target.run("echo first-line")
    chunk, stream = await gen.__anext__()
    assert stream == "pid"
    assert chunk.isdigit()
    await gen.aclose()


async def test_pid_yielded_only_once():
    """Exactly one pid tuple: a second one would confuse cancellation logic."""
    target = LocalTarget(allow=True)
    count = 0
    async for _, stream in target.run("echo a; echo b >&2"):
        if stream == "pid":
            count += 1
    assert count == 1


# ---------------------------------------------------------------------------
# Output streaming
# ---------------------------------------------------------------------------


async def test_stdout_and_stderr_stream_separately():
    """Both streams must arrive tagged so handlers can render them distinctly."""
    target = LocalTarget(allow=True)
    _, out, err, _ = await drain(target.run("echo out-part; echo err-part >&2"))
    assert "out-part" in out
    assert "err-part" in err
    assert "err-part" not in out


async def test_multiline_output_is_complete():
    """Multi-line output must arrive whole -- no premature stream close."""
    target = LocalTarget(allow=True)
    _, out, _, _ = await drain(target.run("seq 1 200"))
    numbers = out.split()
    assert numbers[0] == "1"
    assert numbers[-1] == "200"
    assert len(numbers) == 200


async def test_output_larger_than_one_read_chunk():
    """A payload past a single 64 KiB read must be stitched back together."""
    target = LocalTarget(allow=True)
    _, out, _, _ = await drain(target.run("seq 1 20000"))
    assert out.count("\n") >= 20000


async def test_non_ascii_output_survives():
    """utf-8 replace decoding must keep non-ASCII output readable, not crash."""
    target = LocalTarget(allow=True)
    _, out, _, _ = await drain(target.run("printf 'caf\\303\\251'"))
    assert "café" in out


async def test_exit_status_appended_to_stderr():
    """A failing command must append the SSH-style '[exit status N]' chunk."""
    target = LocalTarget(allow=True)
    _, _, err, _ = await drain(target.run("exit 3"))
    assert "[exit status 3]" in err


async def test_exit_status_zero_is_silent():
    """Success must NOT emit an exit-status chunk (matches the SSH path)."""
    target = LocalTarget(allow=True)
    _, _, err, _ = await drain(target.run("true"))
    assert "exit status" not in err


async def test_shell_features_work():
    """Commands run through a shell: pipes, quoting and variables must work."""
    target = LocalTarget(allow=True)
    _, out, _, _ = await drain(target.run("printf '%s' 'a b' | tr ' ' '-' | tr 'a-z' 'A-Z'"))
    assert "A-B" in out


# ---------------------------------------------------------------------------
# Timeout kills the whole process group
# ---------------------------------------------------------------------------


async def test_timeout_yields_stderr_message_not_exception():
    """A timeout is reported as an stderr chunk so handler code stays uniform."""
    target = LocalTarget(allow=True)
    _, _, err, _ = await drain(target.run("sleep 30", timeout=0.4))
    assert "Error: Command timed out." in err


async def test_timeout_killed_process_is_reaped(tmp_path):
    """The shell itself must be dead once the timeout chunk has been delivered."""
    target = LocalTarget(allow=True)
    gen = target.run("sleep 40", timeout=0.3)
    shell_pid = int((await gen.__anext__())[0])
    rest = [item async for item in gen]
    assert any("timed out" in chunk for chunk, _ in rest)
    assert await _wait_until_dead(shell_pid), f"shell {shell_pid} survived the timeout kill"


async def test_timeout_kills_whole_process_group(tmp_path):
    """The shell AND its children must die: killing only the shell leaks them.

    The beater is a grandchild of the shell; if the kill were scoped to the
    direct child only, the beater would keep appending to the file forever.
    """
    beat = tmp_path / "beat.txt"
    target = LocalTarget(allow=True)
    helper = tmp_path / "beat.py"
    _, _, err, _ = await drain(
        target.run(_beater_script(str(beat), str(helper)), timeout=0.6)
    )
    assert "Error: Command timed out." in err

    assert beat.exists(), "the beater must have started (file written)"
    size = beat.stat().st_size
    for _ in range(60):
        await asyncio.sleep(0.05)
        current = beat.stat().st_size
        if current == size and current > 0:
            return
        size = current
    pytest.fail("a child of the command survived the timeout kill")


async def test_timeout_kill_is_scoped_to_the_command():
    """A kill must hit only the command's group; bystanders must survive."""
    bystander = subprocess.Popen(  # noqa: ASYNC220 - a deliberate bystander process
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        target = LocalTarget(allow=True)
        _, _, err, _ = await drain(target.run("sleep 10", timeout=0.3))
        assert "Error: Command timed out." in err
        assert bystander.poll() is None, "unrelated process was killed"
    finally:
        bystander.kill()
        bystander.wait()


async def test_zero_timeout_behaves_like_immediate_timeout():
    """timeout=0 means 'no budget left', i.e. a timeout right away."""
    target = LocalTarget(allow=True)
    _, _, err, _ = await drain(target.run("sleep 5", timeout=0.0))
    assert "Error: Command timed out." in err


# ---------------------------------------------------------------------------
# Byte cap parity
# ---------------------------------------------------------------------------


async def test_byte_cap_raises_command_output_too_large():
    """The cap must raise the SSH exception, not silently truncate output."""
    target = LocalTarget(allow=True)
    with pytest.raises(CommandOutputTooLarge):
        async for _ in target.run("yes capped", max_bytes=4096):
            pass


async def test_byte_cap_message_matches_ssh_path():
    """Identical message shape across targets is what callers rely on."""
    target = LocalTarget(allow=True)
    with pytest.raises(CommandOutputTooLarge) as exc:
        async for _ in target.run("yes capped", max_bytes=2048):
            pass
    assert str(exc.value) == "Command produced more than 2048 bytes of output."


async def test_under_cap_succeeds():
    """A command under the cap must complete normally, cap barely noticeable."""
    target = LocalTarget(allow=True)
    _, out, _, _ = await drain(target.run("echo fits", max_bytes=1024))
    assert "fits" in out


async def test_cap_counts_stderr_too():
    """The budget covers both streams: stderr must not become a bypass."""
    target = LocalTarget(allow=True)
    with pytest.raises(CommandOutputTooLarge):
        async for _ in target.run("yes to-stderr >&2", max_bytes=2048):
            pass


# ---------------------------------------------------------------------------
# Generator abandonment
# ---------------------------------------------------------------------------


async def test_early_break_kills_process_group():
    """A caller that stops reading must not leave a shell running forever."""
    target = LocalTarget(allow=True)
    gen = target.run("sleep 60")
    shell_pid = int((await gen.__anext__())[0])
    await gen.aclose()
    assert await _wait_until_dead(shell_pid), "abandoned command was left running"


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


async def test_run_increments_command_counter():
    """Each execution counts, so the dashboard can report local usage volume."""
    METRICS.reset()
    target = LocalTarget(allow=True)
    await drain(target.run("echo counted"))
    assert METRICS.get("runtime_local_commands_total") >= 1


async def test_timeout_increments_timeout_counter():
    """Timeouts get their own counter, separating slowness from volume."""
    METRICS.reset()
    target = LocalTarget(allow=True)
    await drain(target.run("sleep 5", timeout=0.3))
    assert METRICS.get("runtime_local_timeouts_total") >= 1


async def test_cap_increments_capped_counter():
    """Capped runs are counted, so noisy commands are visible in metrics."""
    METRICS.reset()
    target = LocalTarget(allow=True)
    with pytest.raises(CommandOutputTooLarge):
        async for _ in target.run("yes x", max_bytes=1024):
            pass
    assert METRICS.get("runtime_local_output_capped_total") >= 1
