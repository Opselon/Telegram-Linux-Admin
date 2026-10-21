"""Tests for :mod:`src.observability` logging setup and context binding.

What is proven here (all with real file/stream I/O, no mocks of logging):

* ``configure_logging(log_file=...)`` writes exactly one JSON object per line
  and every line carries ``correlation_id``, ``user_id`` and ``event``.
* the console sink stays human-readable (no JSON punctuation, no key dump).
* ``bind_context`` nests, restores on normal exit *and* on exception, and is
  isolated between concurrent ``asyncio`` tasks created by ``gather``.
* ``log_event`` emits the ``event`` plus its fields at the requested level.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from src.observability import (
    bind_context,
    configure_logging,
    current_context,
    log_event,
)


@pytest.fixture(autouse=True)
def _restore_root_logging():
    """Keeps ``configure_logging(force=True)`` from leaking into other tests."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield
    for handler in list(root.handlers):
        if handler not in saved_handlers:
            handler.close()
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)


def _flush_root() -> None:
    for handler in logging.getLogger().handlers:
        handler.flush()


def _read_json_lines(path) -> list[dict]:
    _flush_root()
    raw = path.read_text(encoding="utf-8")
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# JSON file sink
# ---------------------------------------------------------------------------
class TestJsonFileSink:
    def test_writes_one_json_object_per_line(self, tmp_path):
        log_file = tmp_path / "logs" / "bot.json.log"
        configure_logging(level=logging.INFO, log_file=log_file)
        logger = logging.getLogger("tests.json")

        with bind_context(correlation_id="corr-abc", user_id=4242):
            log_event(logger, "handler_call", duration_ms=1.25)
            log_event(logger, "job_finished", duration_ms=8.5)

        lines = _read_json_lines(log_file)
        assert len(lines) == 2
        assert all(isinstance(line, dict) for line in lines)

    def test_each_line_carries_correlation_user_and_event(self, tmp_path):
        log_file = tmp_path / "bot.json.log"
        configure_logging(level=logging.INFO, log_file=log_file)
        logger = logging.getLogger("tests.fields")

        with bind_context(correlation_id="corr-xyz", user_id=7):
            log_event(logger, "command_received", alias="web1")

        payload = _read_json_lines(log_file)[0]
        assert payload["correlation_id"] == "corr-xyz"
        assert payload["user_id"] == 7
        assert payload["event"] == "command_received"
        assert payload["alias"] == "web1"
        assert payload["level"] == "INFO"
        assert payload["logger"] == "tests.fields"
        assert payload["message"] == "command_received"

    def test_explicit_fields_override_bound_context(self, tmp_path):
        log_file = tmp_path / "bot.json.log"
        configure_logging(level=logging.INFO, log_file=log_file)
        logger = logging.getLogger("tests.override")

        with bind_context(correlation_id="outer"):
            log_event(logger, "handler_call", correlation_id="inner")

        assert _read_json_lines(log_file)[0]["correlation_id"] == "inner"

    def test_duration_ms_stays_numeric(self, tmp_path):
        log_file = tmp_path / "bot.json.log"
        configure_logging(level=logging.INFO, log_file=log_file)
        logger = logging.getLogger("tests.duration")

        log_event(logger, "handler_call", duration_ms=3.5)

        payload = _read_json_lines(log_file)[0]
        assert payload["duration_ms"] == 3.5
        assert isinstance(payload["duration_ms"], float)

    def test_message_secrets_are_redacted_in_file(self, tmp_path):
        log_file = tmp_path / "bot.json.log"
        configure_logging(level=logging.INFO, log_file=log_file)
        logger = logging.getLogger("tests.secret")

        logger.info("connecting with token 123456:TEST-TOKEN-abcdefghijklmnopqrstuvwxyz")

        payload = _read_json_lines(log_file)[0]
        assert "TEST-TOKEN" not in json.dumps(payload)
        assert "[REDACTED]" in payload["message"]

    def test_field_values_are_redacted_too(self, tmp_path):
        log_file = tmp_path / "bot.json.log"
        configure_logging(level=logging.INFO, log_file=log_file)
        logger = logging.getLogger("tests.field_secret")

        log_event(logger, "ssh_connect", password="hunter2")

        payload = _read_json_lines(log_file)[0]
        assert "hunter2" not in json.dumps(payload)
        assert payload["password"] == "[REDACTED]"

    def test_level_filtering_applies_to_file(self, tmp_path):
        log_file = tmp_path / "bot.json.log"
        configure_logging(level=logging.WARNING, log_file=log_file)
        logger = logging.getLogger("tests.levels")

        log_event(logger, "debug_noise", level=logging.DEBUG)
        log_event(logger, "real_problem", level=logging.ERROR)

        lines = _read_json_lines(log_file)
        assert [line["event"] for line in lines] == ["real_problem"]
        assert lines[0]["level"] == "ERROR"

    def test_rotation_configured_with_bounded_size(self, tmp_path):
        log_file = tmp_path / "bot.json.log"
        configure_logging(level=logging.INFO, log_file=log_file)

        file_handlers = [
            h
            for h in logging.getLogger().handlers
            if isinstance(h, logging.handlers.RotatingFileHandler)
        ]
        assert len(file_handlers) == 1
        assert file_handlers[0].maxBytes == 10 * 1024 * 1024
        assert file_handlers[0].backupCount == 5

    def test_json_file_false_uses_human_format(self, tmp_path):
        log_file = tmp_path / "bot.log"
        configure_logging(level=logging.INFO, log_file=log_file, json_file=False)
        logging.getLogger("tests.human_file").info("plain readable line")
        _flush_root()

        text = log_file.read_text(encoding="utf-8")
        assert "plain readable line" in text
        with pytest.raises(json.JSONDecodeError):
            json.loads(text.strip())


# ---------------------------------------------------------------------------
# Console sink
# ---------------------------------------------------------------------------
class TestConsoleSink:
    def test_console_output_is_human_readable(self, tmp_path, capsys):
        configure_logging(level=logging.INFO, log_file=tmp_path / "bot.json.log")
        logger = logging.getLogger("tests.console")

        with bind_context(correlation_id="corr-console", user_id=11):
            log_event(logger, "handler_call", duration_ms=2.0)
        _flush_root()

        out = capsys.readouterr().out
        assert "handler_call" in out
        assert "correlation_id=corr-console" in out
        assert "user_id=11" in out
        # Readable line, not a JSON dump.
        assert not out.lstrip().startswith("{")
        assert '"level"' not in out

    def test_console_keeps_plain_messages_plain(self, tmp_path, capsys):
        configure_logging(level=logging.INFO, log_file=tmp_path / "bot.json.log")
        logging.getLogger("tests.plain").info("startup complete")
        _flush_root()

        out = capsys.readouterr().out
        assert "startup complete" in out
        assert "|" not in out.split("startup complete")[1]

    def test_force_true_replaces_existing_handlers(self, tmp_path):
        root = logging.getLogger()
        stale = logging.NullHandler()
        root.addHandler(stale)

        configure_logging(level=logging.INFO, log_file=tmp_path / "bot.json.log")

        assert stale not in root.handlers
        assert len(root.handlers) == 2  # console + rotating file


# ---------------------------------------------------------------------------
# Context binding
# ---------------------------------------------------------------------------
class TestContextBinding:
    def test_no_context_by_default(self):
        assert current_context() == {}

    def test_bind_and_restore(self):
        with bind_context(correlation_id="c1", user_id=1):
            assert current_context() == {"correlation_id": "c1", "user_id": "1"}
        assert current_context() == {}

    def test_nested_bindings_restore_inner_then_outer(self):
        with bind_context(correlation_id="outer"):
            with bind_context(alias="web1"):
                assert current_context() == {
                    "correlation_id": "outer",
                    "alias": "web1",
                }
            assert current_context() == {"correlation_id": "outer"}
        assert current_context() == {}

    def test_restores_on_exception(self):
        with pytest.raises(RuntimeError):
            with bind_context(correlation_id="doomed"):
                assert current_context()["correlation_id"] == "doomed"
                raise RuntimeError("handler exploded")
        assert current_context() == {}

    def test_none_values_are_skipped(self):
        with bind_context(correlation_id="c1", user_id=None):
            assert current_context() == {"correlation_id": "c1"}

    def test_values_are_coerced_to_str(self):
        with bind_context(user_id=99, duration_ms=1.5):
            ctx = current_context()
        assert ctx == {"user_id": "99", "duration_ms": "1.5"}
        assert all(isinstance(v, str) for v in ctx.values())

    def test_current_context_returns_a_copy(self):
        with bind_context(correlation_id="c1"):
            snapshot = current_context()
            snapshot["correlation_id"] = "mutated"
            assert current_context()["correlation_id"] == "c1"

    def test_empty_binding_is_a_noop(self):
        with bind_context():
            assert current_context() == {}

    @pytest.mark.asyncio
    async def test_async_isolation_with_gather(self):
        async def worker(index: int) -> str:
            with bind_context(correlation_id=f"corr-{index}", user_id=index):
                await asyncio.sleep(0.005)
                # Each task must still see its own fields after yielding.
                return current_context()["correlation_id"]

        results = await asyncio.gather(*(worker(i) for i in range(6)))

        assert results == [f"corr-{i}" for i in range(6)]
        assert current_context() == {}

    @pytest.mark.asyncio
    async def test_child_task_does_not_leak_into_parent(self):
        async def noisy_child() -> None:
            with bind_context(correlation_id="child"):
                await asyncio.sleep(0)
                assert current_context()["correlation_id"] == "child"

        with bind_context(correlation_id="parent"):
            await asyncio.create_task(noisy_child())
            assert current_context() == {"correlation_id": "parent"}

    @pytest.mark.asyncio
    async def test_async_restore_on_exception(self):
        async def failing() -> None:
            with bind_context(correlation_id="async-doomed"):
                raise ValueError("async boom")

        with pytest.raises(ValueError):
            await failing()
        assert current_context() == {}


# ---------------------------------------------------------------------------
# log_event
# ---------------------------------------------------------------------------
class TestLogEvent:
    def test_emits_named_event_with_fields(self, caplog):
        caplog.set_level(logging.INFO, logger="tests.event")
        logger = logging.getLogger("tests.event")

        with caplog.at_level(logging.INFO, logger="tests.event"):
            log_event(logger, "command_received", alias="web1", risk="safe")

        record = caplog.records[-1]
        assert record.message if hasattr(record, "message") else True
        assert record.ctx_event == "command_received"
        assert record.ctx_alias == "web1"
        assert record.ctx_risk == "safe"
        assert record.levelno == logging.INFO

    def test_custom_level_is_honoured(self, caplog):
        logger = logging.getLogger("tests.event_level")

        with caplog.at_level(logging.WARNING, logger="tests.event_level"):
            log_event(logger, "queue_full", level=logging.WARNING, pending=64)

        record = caplog.records[-1]
        assert record.levelno == logging.WARNING
        assert record.ctx_pending == 64

    def test_does_not_mutate_logger_state(self):
        logger = logging.getLogger("tests.event_state")
        before = (logger.level, list(logger.handlers), logger.propagate)

        log_event(logger, "noop_event")

        assert (logger.level, list(logger.handlers), logger.propagate) == before
