"""PURPOSE: Stateless pagination helpers shared by every long list.

OWNER: lane `ui-ux` of wave 3.3 (branch feat/3.3-ui-ux).
CONSUMES: nothing (stdlib only). PTB objects are NOT touched here: only
  plain strings for ``callback_data`` and plain ints for windows.
PROVIDES: ``compute_window``, ``clamp_page``, ``has_prev``, ``has_next``,
  ``page_count``, ``prev_callback``, ``next_callback``,
  ``parse_target_page``, and ``Nav`` (the data payload ``ui_kit.paged``
  turns into the nav keyboard row).
INVARIANTS:
  * Pure and stateless: no I/O, no asyncio, no module state.
  * Pages are 0-indexed at the boundary. ``clamp_page`` is the single
    authority for "1 short of total pages" semantics: a page == the last
    true page is valid, anything above it is pulled back.
  * Callback-data strings here end with the page number they target.
    A consumer that receives ``prefix_prev_<n>`` / ``prefix_next_<n>``
    and re-runs ``paged()`` with ``clamp_page(n)`` stays in range
    forever - the nav button on the last page targets a page that
    ``clamp_page`` folds back to the last real one.
EXTEND: add a helper here first, then let ui_kit consume it; nothing
  under src/bot should re-derive window math inline.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "clamp_page",
    "page_count",
    "compute_window",
    "has_prev",
    "has_next",
    "prev_callback",
    "next_callback",
    "parse_target_page",
    "Nav",
]

#: ``paged`` disables the nav button that cannot move further by rendering
#: its label through translate('*nav_disabled*').  The CallbackData string is
#: KEPT pointing at the real adjacent page (documented choice in ui_kit):
#: Telegram grey-buttons still fire CallbackQuery, so a handler that parses
#: the suffix through parse_target_page and re-clamps stays correct even if
#: the disabled look is bypassed.


def clamp_page(page: int, total: int) -> int:
    """Clamps a 0-based page number into ``[0, <last real page>]``.

    The last real page is ``page_count - 1``; a request for exactly that
    page is valid, anything above is folded back onto the last page with
    content.  Empty input (``total == 0``) always yields page 0.
    """
    if total <= 0:
        return 0
    return max(0, min(page, total - 1))


def page_count(total: int, per_page: int) -> int:
    """Number of pages needed for ``total`` rows with ``per_page`` each."""
    if per_page <= 0 or total <= 0:
        return 1
    return (total + per_page - 1) // per_page


def compute_window(total: int, page: int, per_page: int) -> tuple[int, int]:
    """Returns the ``(start, stop)`` slice bounds for a pages window.

    Always in range: a page beyond the last real one is clamped, so the
    returned slice is never empty when ``total > 0``.
    """
    pages = page_count(total, per_page)
    clamped = clamp_page(page, pages)
    start = clamped * per_page
    stop = min(start + per_page, total)
    return (start, stop)


def has_prev(page_after_clamp: int) -> bool:
    """Whether the clamped page can move toward page 0."""
    return page_after_clamp > 0


def has_next(page_after_clamp: int, pages: int) -> bool:
    """Whether the clamped page can move toward the last real page."""
    return page_after_clamp < max(0, pages - 1)


def prev_callback(prefix: str, target_page: int) -> str:
    """CallbackData for a nav button moving one page back."""
    return f"{prefix}_prev_{max(0, target_page)}"


def next_callback(prefix: str, target_page: int) -> str:
    """CallbackData for a nav button moving one page forward."""
    return f"{prefix}_next_{max(0, target_page)}"


def parse_target_page(data: str) -> int:
    """Extracts the target page from a ``<prefix>_prev_<n>`` string.

    Returns the integer suffix, or -1 when the string is not a nav
    callback.  Callers must re-clamp the result with ``clamp_page``.
    """
    try:
        return int(data.rsplit("_", 1)[1])
    except (IndexError, ValueError):
        return -1


@dataclass(frozen=True)
class Nav:
    """The navigation payload for one rendered page.

    ``start``/``stop`` are the slice bounds into the full row list;
    ``prev_enabled`` / ``next_enabled`` decide whether each nav button
    moves or renders in the disabled look.  CallbackData strings stay
    absolute (real adjacent page) so a stale-button press re-clamps.
    """

    start: int
    stop: int
    page: int
    pages: int
    prev_enabled: bool
    next_enabled: bool
    prev_data: str
    next_data: str
