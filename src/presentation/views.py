"""Declarative views: every screen is data, rendered once by one renderer.

Handlers used to assemble text and keyboards inline, which is why each menu
looked slightly different and English leaked into translated flows.  A view is
now a title, a body, and a button grid built by :mod:`src.presentation.keyboards`;
:func:`render` turns it into the exact string Telegram gets, escaping through
the project's parse-mode helpers so bold works in MarkdownV2 *and* HTML.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from src.parse_mode import format_bold, escape_text
from src.presentation.keyboards import ButtonGrid
from src.presentation.i18n import get_language_label, translate


@dataclass(slots=True)
class View:
    """A screen: heading, supporting lines, and its button grid."""

    title: str
    lines: list[str] = field(default_factory=list)
    buttons: ButtonGrid = field(default_factory=list)

    def add(self, *lines: str) -> "View":
        self.lines.extend(line for line in lines if line is not None)
        return self

    def section(self, heading: str) -> "View":
        """Adds a blank line plus a small heading before a button group."""
        if self.lines:
            self.lines.append("")
        self.lines.append(heading)
        return self


def render(view: View, parse_mode: str | None) -> str:
    """Renders *view* to a Telegram-safe string for the given parse mode."""
    parts = [format_bold(view.title, parse_mode)]
    for line in view.lines:
        parts.append(escape_text(line, parse_mode))
    return "\n".join(parts)


def label(key: str, language: str, **kwargs: str) -> str:
    return translate(key, language, **kwargs)


def language_label(code: str) -> str:
    return get_language_label(code)


def join_buttons(pairs: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    return list(pairs)
