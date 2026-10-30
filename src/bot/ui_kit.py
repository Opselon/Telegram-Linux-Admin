"""PURPOSE: Pure Telegram UI kit - (text, InlineKeyboardMarkup) builders.

OWNER: lane `ui-ux` of wave 3.3 (branch feat/3.3-ui-ux).
CONSUMES: src.localization.translate (all user-visible text) and
  src.bot.pagination (window math + callback strings); the ONLY PTB
  imports allowed here are ``InlineKeyboardButton`` /
  ``InlineKeyboardMarkup``.  No ``Application``, no ``Update``, no
  send/edit - these functions perform zero I/O; the orchestrator's
  migration commit wires them into handlers.
PROVIDES: ``menu``, ``kv_panel``, ``paged`` and the ``PagedPage`` payload
  (text + markup + window bounds) handlers render directly.
INVARIANTS:
  * Deterministic: same input yields byte-identical markup; ``menu``
    renders items in the order given, never re-sorted.
  * All user-visible strings route through ``translate()`` with keys
    added en-only per contract law 6 (``ui_back``, ``ui_kv_empty``,
    ``ui_nav_prev``, ``ui_nav_next``, ``ui_nav_disabled``,
    ``ui_page_indicator``, ``ui_page_of``).
  * Pagination semantics: pages are 0-based at this kit's boundary;
    ``paged`` clamps via ``pagination.clamp_page`` so a request past the
    last real page folds onto the final populated page (never an empty
    window when ``rows`` is non-empty; empty input lands on page 0).
  * Documented disabled-look choice: a boundary page renders its
    cannot-move button with the ``ui_nav_disabled`` label while the
    CallbackData still points at the real adjacent page - a stale
    keyboard press therefore re-clamps into range instead of dying.
  * Code fences emitted by ``kv_panel`` are LITERAL blocks opened and
    closed by this function alone: user data inside them cannot break
    the fence (a value containing backticks/quotes renders verbatim,
    safe under MarkdownV2).
EXTEND: add a builder here; the migration commit wires it into the
  owning handler modules - never attach Update/Application I/O here.
"""

from __future__ import annotations

from dataclasses import dataclass

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ..localization import translate
from . import pagination

__all__ = [
    "menu",
    "kv_panel",
    "paged",
    "PagedPage",
    "LABEL_MAX",
    "MENU_MAX_ROWS",
    "ICONS",
    "styled_menu",
    "section_row",
]

#: Menu button label limit; longer labels truncate with an ellipsis.
LABEL_MAX = 24

#: Hard clamp on keyboard rows in ``menu``; longer lists page via ``paged``.
MENU_MAX_ROWS = 8

#: per_page floor used when a caller passes a non-positive page size.
_FALLBACK_PER_PAGE = 6

#: Canonical icon set.  Deterministic, emoji-only (no image assets), applied
#: by ``styled_menu`` so every menu inherits the same visual language.
ICONS: dict[str, str] = {
    "system": "🖥",
    "server": "🌐",
    "security": "🛡",
    "tools": "🧰",
    "settings": "⚙️",
    "status": "📊",
    "shell": "⌨️",
    "files": "📁",
    "docker": "🐳",
    "packages": "📦",
    "firewall": "🔥",
    "processes": "⚙️",
    "services": "🧩",
    "backup": "💾",
    "restore": "♻️",
    "update": "🔄",
    "language": "🌍",
    "back": "🔙",
    "add": "➕",
    "remove": "➖",
    "connect": "🔌",
    "warn": "⚠️",
    "ok": "✅",
    "denied": "⛔",
    "home": "🏠",
}

#: Callback-data → icon-key aliases for the established wire-format names
#: (those callbacks predate the icon system and cannot be renamed).
CALLBACK_ICON_ALIASES: dict[str, str] = {
    "host_status": "status",
    "sec_menu": "security",
    "connect_server_menu": "connect",
    "add_server_start": "add",
    "remove_server_menu": "remove",
    "language_menu": "language",
    "backup": "backup",
    "restore": "restore",
    "service_management_menu_": "services",
    "package_management_menu_": "packages",
    "docker_management_menu_": "docker",
    "firewall_management_menu_": "firewall",
    "process_management_menu_": "processes",
    "file_manager_menu_": "files",
    "system_commands_menu_": "tools",
}


def _icon_for(callback_data: str) -> str:
    """Best-effort icon for a callback name: exact key, alias, first segment."""
    if callback_data in ICONS:
        return ICONS[callback_data]
    alias = CALLBACK_ICON_ALIASES.get(callback_data)
    if alias:
        return ICONS.get(alias, "")
    return ICONS.get(callback_data.split("_", 1)[0], "")


def _truncate(label: str, limit: int = LABEL_MAX) -> str:
    """Truncates ``label`` to ``limit`` chars, appending an ellipsis when cut."""
    if len(label) <= limit:
        return label
    return label[: limit - 1] + "…"


def styled_menu(
    title: str,
    items: list[tuple[str, str]],
    back: str | None = None,
    *,
    columns: int = 1,
    icons: bool = True,
) -> tuple[str, InlineKeyboardMarkup]:
    """Builds a styled menu: icon-labelled buttons in one or two columns.

    Deterministic like :func:`menu`: ``items`` render in the given order,
    labels truncate identically, and the trailing ``back`` row is appended
    last when given.  ``columns`` may only be 1 or 2; with 2, items fill
    left-to-right per row and a lone trailing item occupies a single cell.
    ``icons`` prefixes each label with the icon registered in ``ICONS``
    under the item's icon key — items may carry their own icon inline
    instead, in which case no prefix is added.
    """
    if columns not in (1, 2):
        columns = 1
    keyboard: list[list[InlineKeyboardButton]] = []

    def _button(label: str, cb: str) -> InlineKeyboardButton:
        rendered = label
        if icons and not any(emoji in label for emoji in ICONS.values()):
            icon = _icon_for(cb)
            if icon:
                rendered = f"{icon} {label}"
        return InlineKeyboardButton(_truncate(rendered), callback_data=cb)

    if columns == 1:
        keyboard = [[_button(label, cb)] for label, cb in items[:MENU_MAX_ROWS]]
    else:
        window = items[: MENU_MAX_ROWS * 2]
        for index in range(0, len(window), 2):
            row = [_button(window[index][0], window[index][1])]
            if index + 1 < len(window):
                row.append(_button(window[index + 1][0], window[index + 1][1]))
            keyboard.append(row)
    if back is not None:
        back_icon = ICONS["back"]
        keyboard.append(
            [InlineKeyboardButton(f"{back_icon} {translate('ui_back')}", callback_data=back)]
        )
    return (title, InlineKeyboardMarkup(keyboard))


def section_row(
    sections: list[tuple[str, str]],
) -> list[list[InlineKeyboardButton]]:
    """Renders one row of section buttons (2-3 per row, icon-prefixed)."""
    row: list[InlineKeyboardButton] = []
    for label, cb in sections:
        icon = _icon_for(cb)
        text = f"{icon} {label}" if icon and icon not in label else label
        row.append(InlineKeyboardButton(_truncate(text), callback_data=cb))
    return [row]


def menu(
    title: str,
    items: list[tuple[str, str]],
    back: str | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Builds a menu: (title text, markup with one button per row).

    * ``items``: ``(label, callback_data)`` pairs rendered in the given
      order (deterministic; no re-sort).
    * Labels longer than 24 chars truncate with an ellipsis.
    * More than 8 rows: rows after the 8th are dropped by the clamp
      (the caller pages long lists through ``paged`` instead).
    * ``back``: when given, a trailing row with the localized back label
      and CallbackData ``back`` is appended; the back row is navigation,
      not content, so it never counts against the 8-row clamp.
    """
    keyboard: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(_truncate(label), callback_data=cb)]
        for label, cb in items[:MENU_MAX_ROWS]
    ]
    if back is not None:
        keyboard.append([InlineKeyboardButton(_truncate(translate("ui_back")), callback_data=back)])
    return (title, InlineKeyboardMarkup(keyboard))


def kv_panel(title: str, pairs: list[tuple[str, str]]) -> tuple[str, InlineKeyboardMarkup]:
    """Builds an aligned ``label : value`` panel inside a literal code fence.

    Each pair renders as ``label<padded> : value``; the pad aligns the
    value column across rows (widest label width wins).  Both labels and
    values are user data placed verbatim inside a fence opened and closed
    by this function alone: a value containing backticks, quotes or any
    other Markdown punctuation cannot terminate the block early, so the
    fence holds under MarkdownV2 (the tests prove this).

    Empty input renders the localized ``ui_kv_empty`` placeholder inside
    the fence rather than an empty block.
    """

    # A value/label carrying ``` would close the block early and let the
    # tail be parsed as MarkdownV2 - defang it to a non-breaking lookalike
    # (zero-width joined backtick) so user data can never exit the fence.
    def _defang(s: str) -> str:
        return s.replace("```", "\\`\u2062\\`\u2062\\`")

    width = max((len(_defang(label)) for label, _ in pairs), default=0)
    lines = [f"{_defang(label).ljust(width)} : {_defang(value)}" for label, value in pairs]
    body = "\n".join(lines) if lines else translate("ui_kv_empty")
    text = f"{title}\n```\n{body}\n```"
    return (text, InlineKeyboardMarkup([]))


@dataclass(frozen=True)
class PagedPage:
    """Payload of one ``paged()`` render.

    ``text`` is the title + page indicator; ``markup`` carries the
    window rows followed by the nav row.  ``start``/``stop`` are the
    slice bounds into the caller's full row list and ``page``/``pages``
    are the clamped page number and the page count - handlers that
    decorate beyond the basics read them instead of re-deriving.
    """

    text: str
    markup: InlineKeyboardMarkup
    start: int
    stop: int
    page: int
    pages: int


def paged(
    rows: list[tuple[str, str]],
    page: int,
    per_page: int = 6,
    prefix: str = "page",
    total_label: str | None = None,
    title: str = "",
) -> PagedPage:
    """Builds one page: title + ``per_page`` rows + a single nav row.

    * ``page`` is 0-based and clamped through ``pagination`` (see the
      module INVARIANTS): a page past the last real one folds onto the
      last populated page.
    * ``per_page`` gets the fallback floor (6) for a non-positive caller
      value, so no zero-size page math happens.
    * ``title`` prefixes the returned text (title line, then the page
      indicator line); it is UI copy the caller already localized.
    * ``prefix`` names the callback namespace.  Nav CallbackData is
      ``{prefix}_prev_<n>`` / ``{prefix}_next_<n>`` pointing at the real
      adjacent page even in the disabled look (see the module
      INVARIANTS block); the middle button is non-navigational
      (``{prefix}_cur_<n>``) showing ``ui_page_indicator``, or the
      caller's ``total_label``.
    """
    per_page = per_page if per_page > 0 else _FALLBACK_PER_PAGE
    pages = pagination.page_count(len(rows), per_page)
    page = pagination.clamp_page(page, pages)
    start = page * per_page
    window = rows[start : start + per_page]
    indicator = total_label or translate("ui_page_indicator", page=page + 1, pages=pages)
    keyboard: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(_truncate(label), callback_data=cb)] for label, cb in window
    ]
    keyboard.append(
        [
            InlineKeyboardButton(
                translate("ui_nav_prev" if pagination.has_prev(page) else "ui_nav_disabled"),
                callback_data=pagination.prev_callback(prefix, page - 1),
            ),
            InlineKeyboardButton(indicator, callback_data=f"{prefix}_cur_{page}"),
            InlineKeyboardButton(
                translate("ui_nav_next" if pagination.has_next(page, pages) else "ui_nav_disabled"),
                callback_data=pagination.next_callback(prefix, page + 1),
            ),
        ]
    )
    text = f"{title}\n{indicator}"
    return PagedPage(
        text=text,
        markup=InlineKeyboardMarkup(keyboard),
        start=start,
        stop=start + len(window),
        page=page,
        pages=pages,
    )
