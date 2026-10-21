"""Keyboard and theme kit for the Telegram UI.

One builder for every menu so button grids stop being hand-rolled inside each
handler.  Grids are built from a flat list of buttons (row-major), which keeps
menus consistent, and every callback payload carries a fixed ``action:alias``
shape that the callback router can parse and validate.

The kit is pure data — no Telegram objects are constructed here so it stays
unit-testable without a bot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

InlineButton = tuple[str, str]
#: (label, callback_data) pairs, grouped row by row.
ButtonGrid = Sequence[Sequence[InlineButton]]

#: Never let a menu grow wider than this; Telegram rows wrap awkwardly past 2.
DEFAULT_GRID_WIDTH = 2


@dataclass(slots=True)
class MenuSpec:
    """Declarative description of a menu message."""

    text: str
    buttons: list[list[InlineButton]] = field(default_factory=list)
    parse_mode: str | None = None
    #: When True the message is sent as a caption-stripped status line.
    compact: bool = False

    def row(self, *buttons: InlineButton) -> "MenuSpec":
        self.buttons.append(list(buttons))
        return self

    def grid(self, buttons: Iterable[InlineButton], width: int = DEFAULT_GRID_WIDTH) -> "MenuSpec":
        """Arranges *buttons* into rows of *width* (row-major)."""
        row: list[InlineButton] = []
        for button in buttons:
            row.append(button)
            if len(row) >= width:
                self.buttons.append(row)
                row = []
        if row:
            self.buttons.append(row)
        return self

    def back(self, label: str, callback_data: str) -> "MenuSpec":
        self.buttons.append([(label, callback_data)])
        return self


def format_alias_label(alias: str, *, local_label: str | None = None) -> str:
    """Renders a target alias as a button label (already localized by caller).

    The reserved self-host alias renders as a signed label, so the button never
    leaks the internal alias string to end users.
    """
    if local_label is not None and alias == "self-host":
        return local_label
    return alias


def parse_callback(payload: str) -> tuple[str, str] | None:
    """Splits ``action_alias...`` into ``(action, alias)``.

    Handles both the legacy ``run_command_<alias>`` shape (three segments) and
    the two-segment ``action_alias`` shape introduced with the router.
    """
    if not payload or "_" not in payload:
        return None
    head, _, rest = payload.partition("_")
    if not head or not rest:
        return None
    return head, rest


def action_of(payload: str) -> str:
    parsed = parse_callback(payload)
    return parsed[0] if parsed else ""


def alias_of(payload: str) -> str:
    parsed = parse_callback(payload)
    return parsed[1] if parsed else ""


def make_callback(action: str, alias: str) -> str:
    """Builds a callback payload, keeping the reserved alias stable."""
    if alias == "self-host":
        return f"{action}_self-host"
    return f"{action}_{alias}" if alias else action
