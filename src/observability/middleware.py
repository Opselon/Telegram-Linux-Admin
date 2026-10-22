"""PTB handler instrumentation: correlation ids, timing and access logs.

PURPOSE
    Wrap a python-telegram-bot handler so every incoming update gets a
    ``correlation_id`` and ``user_id`` bound to the log context, is timed, and
    emits exactly one structured access log line — without changing handler
    behaviour or the exception that reaches PTB's error handler.

OWNER
    Lane ``observability`` of the 3.2 remaster wave
    (branch ``feat/3.2-observability``).

CONSUMES
    * :mod:`src.observability` (``bind_context``, ``log_event``).
    * PTB objects only duck-typed (``update.effective_user`` /
      ``update.callback_query``) so tests need no real Telegram client.

PROVIDES
    * :func:`instrument` — ``instrument(handler, *, name=None)`` returning a
      PTB-compatible wrapper for sync *and* async handlers.

INVARIANTS
    * Never swallows or rewraps exceptions: the original object propagates.
    * Never calls Telegram APIs (no ``reply_text``, ``answer``, ``send_*``).
    * Always logs exactly one access line per invocation, even on failure.

EXTEND
    * Extra per-update fields belong in ``_update_fields`` so both the info
      and the error path stay in sync.
"""

from __future__ import annotations

import functools
import inspect
import logging
import time
import uuid
from typing import Any, Callable

from . import bind_context, log_event

__all__ = ["instrument"]

logger = logging.getLogger(__name__)


def _update_fields(update: object) -> dict[str, object]:
    """Extracts loggable identity fields from a PTB ``Update`` (duck-typed).

    Returns an empty dict for non-Update objects so the wrapper also works in
    tests and in future non-Telegram call sites.
    """
    fields: dict[str, object] = {}
    user = getattr(update, "effective_user", None)
    user_id = getattr(user, "id", None)
    if user_id is not None:
        fields["user_id"] = user_id

    if getattr(update, "callback_query", None) is not None:
        fields["request_type"] = "callback_query"
    elif getattr(update, "message", None) is not None:
        fields["request_type"] = "message"
    elif getattr(update, "inline_query", None) is not None:
        fields["request_type"] = "inline_query"
    else:
        fields["request_type"] = "other"

    chat = getattr(update, "effective_chat", None)
    chat_id = getattr(chat, "id", None)
    if chat_id is not None:
        fields["chat_id"] = chat_id
    return fields


def _handler_name(handler: Callable[..., Any], override: str | None) -> str:
    """Best stable label for the access log (explicit name wins).

    Falls back to the *unqualified* function name: PTB handlers are usually
    plain module-level functions, and ``<locals>`` qualified names from
    inline definitions are noise in aggregation queries.
    """
    if override:
        return override
    name = getattr(handler, "__name__", None)
    if name:
        return name
    return (
        getattr(handler, "__qualname__", None)
        or repr(handler)
    )


def _log_access(
    *,
    name: str,
    correlation_id: str,
    fields: dict[str, object],
    duration_ms: float,
    outcome: str,
    error: BaseException | None = None,
) -> None:
    """Emits the single access line for one handler invocation."""
    payload: dict[str, object] = {
        "handler": name,
        "correlation_id": correlation_id,
        "duration_ms": round(duration_ms, 3),
        "outcome": outcome,
        **fields,
    }
    if error is not None:
        payload["error_type"] = type(error).__name__
        payload["error"] = str(error)
    level = logging.ERROR if error is not None else logging.INFO
    log_event(logger, "handler_call", level=level, **payload)


def instrument(
    handler: Callable[..., Any], *, name: str | None = None
) -> Callable[..., Any]:
    """Wraps a PTB handler with correlation ids, timing and one access log.

    The wrapper keeps the handler's signature (``(update, context, ...)``),
    returns whatever the handler returns, and re-raises the *same* exception
    object so PTB's error handler still sees ``BadRequest``/``TimedOut``/etc.
    Async handlers yield a coroutine, sync handlers run inline — both are
    supported because ``main.py`` mixes callbacks and plain functions.
    """
    label = _handler_name(handler, name)

    if inspect.iscoroutinefunction(handler):

        @functools.wraps(handler)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            update = args[0] if args else kwargs.get("update")
            fields = _update_fields(update)
            correlation_id = uuid.uuid4().hex
            started = time.perf_counter()
            with bind_context(correlation_id=correlation_id, **fields):
                try:
                    result = await handler(*args, **kwargs)
                except BaseException as exc:
                    _log_access(
                        name=label,
                        correlation_id=correlation_id,
                        fields=fields,
                        duration_ms=(time.perf_counter() - started) * 1000.0,
                        outcome="error",
                        error=exc,
                    )
                    raise
                _log_access(
                    name=label,
                    correlation_id=correlation_id,
                    fields=fields,
                    duration_ms=(time.perf_counter() - started) * 1000.0,
                    outcome="ok",
                )
                return result

        return async_wrapper

    @functools.wraps(handler)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        update = args[0] if args else kwargs.get("update")
        fields = _update_fields(update)
        correlation_id = uuid.uuid4().hex
        started = time.perf_counter()
        with bind_context(correlation_id=correlation_id, **fields):
            try:
                result = handler(*args, **kwargs)
            except BaseException as exc:
                _log_access(
                    name=label,
                    correlation_id=correlation_id,
                    fields=fields,
                    duration_ms=(time.perf_counter() - started) * 1000.0,
                    outcome="error",
                    error=exc,
                )
                raise
            _log_access(
                name=label,
                correlation_id=correlation_id,
                fields=fields,
                duration_ms=(time.perf_counter() - started) * 1000.0,
                outcome="ok",
            )
            return result

    return sync_wrapper
