"""Tests for :func:`src.observability.middleware.instrument`.

The wrapper must behave exactly like the handler it wraps for PTB:

* binds ``correlation_id``/``user_id`` and times the call;
* emits exactly one JSON access log line carrying ``duration_ms``;
* re-raises the *same* exception object (no swallowing, no rewrap);
* works for sync and async handlers;
* never calls Telegram APIs (no send/reply/answer).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging

import pytest

from src.observability import bind_context, configure_logging, current_context
from src.observability.middleware import instrument


class _FakeUser:
    def __init__(self, user_id: int | None = 4242) -> None:
        self.id = user_id


class _FakeChat:
    def __init__(self, chat_id: int | None = 99) -> None:
        self.id = chat_id


class _FakeUpdate:
    """Duck-typed PTB ``Update``: only the attributes the wrapper reads."""

    def __init__(
        self,
        *,
        user_id: int | None = 4242,
        chat_id: int | None = 99,
        kind: str = "message",
    ) -> None:
        self.effective_user = _FakeUser(user_id) if user_id is not None else None
        self.effective_chat = _FakeChat(chat_id) if chat_id is not None else None
        self.message = object() if kind == "message" else None
        self.callback_query = object() if kind == "callback_query" else None
        self.inline_query = object() if kind == "inline_query" else None


class _RecordingContext:
    """Stands in for ``telegram.ext.ContextTypes.DEFAULT_TYPE``."""

    def __init__(self) -> None:
        self.calls: list = []


@pytest.fixture(autouse=True)
def _restore_root_logging():
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield
    for handler in list(root.handlers):
        if handler not in saved_handlers:
            handler.close()
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)


def _flush() -> None:
    for handler in logging.getLogger().handlers:
        handler.flush()


def _json_lines(path) -> list[dict]:
    _flush()
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# Correlation id / context binding
# ---------------------------------------------------------------------------
class TestContextBinding:
    @pytest.mark.asyncio
    async def test_binds_correlation_id_and_user_id(self):
        seen = {}

        @instrument
        async def handler(update, context):
            seen.update(current_context())

        await handler(_FakeUpdate(), _RecordingContext())

        assert seen["correlation_id"]
        assert seen["user_id"] == "4242"
        assert len(seen["correlation_id"]) >= 16

    @pytest.mark.asyncio
    async def test_correlation_id_unique_per_call(self):
        @instrument
        async def handler(update, context):
            return current_context()["correlation_id"]

        first = await handler(_FakeUpdate(), _RecordingContext())
        second = await handler(_FakeUpdate(), _RecordingContext())

        assert first != second

    @pytest.mark.asyncio
    async def test_context_restored_after_call(self):
        with bind_context(correlation_id="outer"):

            @instrument
            async def handler(update, context):
                return current_context()

            inner = await handler(_FakeUpdate(), _RecordingContext())
            assert inner["correlation_id"] != "outer"
            assert current_context()["correlation_id"] == "outer"

    @pytest.mark.asyncio
    async def test_record_request_type_and_chat(self):
        seen = {}

        @instrument
        async def handler(update, context):
            seen.update(current_context())

        await handler(_FakeUpdate(kind="callback_query"), _RecordingContext())

        assert seen["request_type"] == "callback_query"
        assert seen["chat_id"] == "99"

    @pytest.mark.asyncio
    async def test_missing_user_is_tolerated(self):
        seen = {}

        @instrument
        async def handler(update, context):
            seen.update(current_context())

        await handler(_FakeUpdate(user_id=None), _RecordingContext())

        assert "user_id" not in seen
        assert seen["correlation_id"]


# ---------------------------------------------------------------------------
# Access log line
# ---------------------------------------------------------------------------
class TestAccessLogLine:
    @pytest.mark.asyncio
    async def test_emits_one_json_line_with_duration_ms(self, tmp_path):
        log_file = tmp_path / "access.json.log"
        configure_logging(level=logging.INFO, log_file=log_file)

        @instrument
        async def handler(update, context):
            await asyncio.sleep(0.005)
            return "done"

        result = await handler(_FakeUpdate(), _RecordingContext())

        assert result == "done"
        lines = _json_lines(log_file)
        assert len(lines) == 1
        payload = lines[0]
        assert payload["event"] == "handler_call"
        assert isinstance(payload["duration_ms"], float)
        assert payload["duration_ms"] >= 1.0
        assert payload["user_id"] == 4242
        assert payload["correlation_id"]
        assert payload["outcome"] == "ok"

    @pytest.mark.asyncio
    async def test_handler_label_from_explicit_name(self, tmp_path):
        log_file = tmp_path / "access.json.log"
        configure_logging(level=logging.INFO, log_file=log_file)

        async def handler(update, context):
            return None

        await instrument(handler, name="start_cmd")(
            _FakeUpdate(), _RecordingContext()
        )

        assert _json_lines(log_file)[0]["handler"] == "start_cmd"

    @pytest.mark.asyncio
    async def test_handler_label_defaults_to_function_name(self, tmp_path):
        log_file = tmp_path / "access.json.log"
        configure_logging(level=logging.INFO, log_file=log_file)

        @instrument
        async def my_named_handler(update, context):
            return None

        await my_named_handler(_FakeUpdate(), _RecordingContext())

        assert _json_lines(log_file)[0]["handler"] == "my_named_handler"


# ---------------------------------------------------------------------------
# Exception behaviour
# ---------------------------------------------------------------------------
class TestExceptionBehaviour:
    @pytest.mark.asyncio
    async def test_reraises_same_exception_object_async(self):
        boom = ValueError("async boom")

        @instrument
        async def handler(update, context):
            raise boom

        with pytest.raises(ValueError) as excinfo:
            await handler(_FakeUpdate(), _RecordingContext())

        assert excinfo.value is boom

    def test_reraises_same_exception_object_sync(self):
        boom = KeyError("sync boom")

        @instrument
        def handler(update, context):
            raise boom

        with pytest.raises(KeyError) as excinfo:
            handler(_FakeUpdate(), _RecordingContext())

        assert excinfo.value is boom

    @pytest.mark.asyncio
    async def test_error_path_logs_error_line_with_fields(self, tmp_path):
        log_file = tmp_path / "access.json.log"
        configure_logging(level=logging.INFO, log_file=log_file)

        @instrument
        async def handler(update, context):
            raise RuntimeError("disk on fire")

        with pytest.raises(RuntimeError):
            await handler(_FakeUpdate(), _RecordingContext())

        lines = _json_lines(log_file)
        assert len(lines) == 1
        payload = lines[0]
        assert payload["outcome"] == "error"
        assert payload["error_type"] == "RuntimeError"
        assert payload["error"] == "disk on fire"
        assert payload["level"] == "ERROR"
        assert isinstance(payload["duration_ms"], float)

    def test_handler_type_preserved(self):
        async def handler(update, context):
            return "ok"

        wrapped = instrument(handler)
        assert inspect.iscoroutinefunction(wrapped)

    @pytest.mark.asyncio
    async def test_base_exception_propagates_with_log(self, tmp_path):
        log_file = tmp_path / "access.json.log"
        configure_logging(level=logging.INFO, log_file=log_file)

        @instrument
        async def handler(update, context):
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await handler(_FakeUpdate(), _RecordingContext())

        assert _json_lines(log_file)[0]["outcome"] == "error"


# ---------------------------------------------------------------------------
# Sync + async parity, no Telegram calls
# ---------------------------------------------------------------------------
class TestSyncAndAsync:
    @pytest.mark.asyncio
    async def test_async_handler_return_value_passes_through(self):
        @instrument
        async def handler(update, context):
            return {"chat": update.effective_chat.id}

        result = await handler(_FakeUpdate(), _RecordingContext())
        assert result == {"chat": 99}

    def test_sync_handler_return_value_passes_through(self):
        @instrument
        def handler(update, context):
            return update.effective_user.id

        assert handler(_FakeUpdate(), _RecordingContext()) == 4242

    def test_sync_handler_logs_duration_ms(self, tmp_path):
        log_file = tmp_path / "access.json.log"
        configure_logging(level=logging.INFO, log_file=log_file)

        @instrument
        def handler(update, context):
            return sum(range(1000))

        handler(_FakeUpdate(), _RecordingContext())

        assert _json_lines(log_file)[0]["duration_ms"] >= 0.0

    @pytest.mark.asyncio
    async def test_works_with_positional_and_keyword_args(self):
        @instrument
        async def handler(update, context, extra=None, *, flag=False):
            return extra, flag

        assert await handler(_FakeUpdate(), _RecordingContext(), "x", flag=True) == (
            "x",
            True,
        )

    def test_preserves_metadata_for_introspection(self):
        @instrument
        async def my_handler(update, context):
            """Docs."""

        assert my_handler.__name__ == "my_handler"
        assert my_handler.__doc__ == "Docs."


class TestNeverCallsTelegram:
    @pytest.mark.asyncio
    async def test_wrapper_does_not_touch_update_methods(self):
        """Only reads attributes; calling any method would be a Telegram API."""

        class Strict(_FakeUpdate):
            def __getattribute__(self, name):
                if name in {"send_message", "reply_text", "answer", "answer_callback_query"}:
                    raise AssertionError(f"wrapper called Telegram API: {name}")
                return object.__getattribute__(self, name)

        @instrument
        async def handler(update, context):
            return None

        await handler(Strict(), _RecordingContext())

    @pytest.mark.asyncio
    async def test_wrapper_does_not_call_bot_or_context(self):
        calls: list = []

        class Guard:
            def __getattr__(self, name):
                calls.append(name)
                raise AssertionError(f"context attribute touched: {name}")

        @instrument
        async def handler(update, context):
            return None

        await handler(_FakeUpdate(), Guard())

        assert not calls


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------
class TestConcurrency:
    @pytest.mark.asyncio
    async def test_concurrent_handlers_keep_separate_correlation_ids(self):
        @instrument
        async def handler(update, context):
            await asyncio.sleep(0.005)
            return current_context()["correlation_id"]

        results = await asyncio.gather(
            *(handler(_FakeUpdate(), _RecordingContext()) for _ in range(5))
        )

        assert len(set(results)) == 5
        assert current_context() == {}

    @pytest.mark.asyncio
    async def test_nested_instrumented_handlers(self, tmp_path):
        @instrument
        async def inner(update, context):
            return current_context()["correlation_id"]

        @instrument
        async def outer(update, context):
            inner_id = await inner(update, context)
            return current_context()["correlation_id"], inner_id

        outer_id, inner_id = await outer(_FakeUpdate(), _RecordingContext())

        assert outer_id != inner_id
