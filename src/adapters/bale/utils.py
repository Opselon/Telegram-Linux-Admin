"""Shared formatting helpers for the Bale adapter.

Message length: Bale caps a text message at **4096 characters** — verified in
the installed SDK docstrings (``bale/bot.py`` ``send_message`` ("Max 4096
characters after entities parsing") and ``edit_message`` ("1- 4096
characters")).  Every outbound text goes through :func:`truncate`.
"""

from __future__ import annotations

BALE_MESSAGE_LIMIT = 4096

_GREEN = {"running", "active", "up", "online", "done", "exited (0)"}
_RED = {"failed", "stopped", "down", "offline", "dead", "cancelled"}


def truncate(text: str, limit: int = BALE_MESSAGE_LIMIT,
             suffix: str = "\n…[truncated]") -> str:
    """Clamps *text* to the Bale message limit, appending a marker when cut."""
    text = text or ""
    if len(text) <= limit:
        return text
    keep = limit - len(suffix)
    if keep <= 0:
        return text[:limit]
    return text[:keep] + suffix


def short(value: object, limit: int = 24) -> str:
    """Button-label-safe short form of *value*."""
    text = str(value or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def status_icon(state: object) -> str:
    """A small glyph for a lifecycle state string."""
    normalized = str(state or "").strip().lower()
    if normalized in _GREEN:
        return "🟢"
    if normalized in _RED:
        return "🔴"
    return "⚪"
