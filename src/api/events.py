"""In-process event broker for the SSE stream (alerts, job completions)."""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Callable

Listener = Callable[[str, dict[str, Any]], None]
_listeners: list[Listener] = []
_lock = threading.Lock()
MAX_LISTENERS = 64


def subscribe(fn: Listener) -> Callable[[], None]:
    """Registers a sync listener; returns the unsubscribe callable."""
    with _lock:
        if len(_listeners) >= MAX_LISTENERS:
            raise RuntimeError("too many event listeners")
        _listeners.append(fn)

    def _unsubscribe() -> None:
        with _lock:
            if fn in _listeners:
                _listeners.remove(fn)

    return _unsubscribe


def publish(kind: str, payload: dict[str, Any]) -> None:
    """Fans an event out to every listener; listener failures are contained."""
    with _lock:
        listeners = list(_listeners)
    for fn in listeners:
        try:
            fn(kind, payload)
        except Exception:
            continue


def wire_notifications() -> None:
    """Connects NotificationService emissions into the SSE broker."""
    from ..services import get_services

    async def _subscriber(kind: str, payload: dict[str, Any]) -> None:
        publish(kind, payload)

    get_services().notifications.subscribe(_subscriber)
