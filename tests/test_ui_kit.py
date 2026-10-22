# PURPOSE: Prove the ui_kit builders (menu/kv_panel/paged) emit exactly the
#          deterministic, clamped, fenced markup handlers will consume.
# OWNER: lane `ui-ux` of wave 3.3 (branch feat/3.3-ui-ux).
# CONSUMES: src.bot.ui_kit, src.bot.pagination.
# PROVIDES: tests/test_ui_kit.py.
# INVARIANTS: pure builders - every test below fails if the markup shape a
#          handler gets regresses (determinism, truncation, row clamp,
#          boundary-page disabled look with absolute callback data, or the
#          kv fence surviving hostile user data).
# EXTEND: markup-shape regression checks for new builders go here; page
#          math/clamping proofs live in tests/test_pagination.py.

from __future__ import annotations

import pytest

from src.bot import ui_kit
from src.bot.ui_kit import MENU_MAX_ROWS
from src.localization import translate


def buttons(markup) -> list[tuple[str, str]]:
    """Flattens an InlineKeyboardMarkup to (text, callback_data) pairs."""
    return [pair.to_dict() for row in markup.inline_keyboard for pair in row]  # type: ignore[union-attr]


def flat(markup) -> list[tuple[str, str]]:
    return [(b.text, b.callback_data or "") for row in markup.inline_keyboard for b in row]  # type: ignore[union-attr]


class TestMenu:
    def test_deterministic_order(self) -> None:
        """Same input must yield byte-identical markup twice, in given order."""
        items = [(f"Item {i}", f"cb_{i}") for i in range(5)]
        m1 = ui_kit.menu("T", items)[1]
        m2 = ui_kit.menu("T", items)[1]
        assert flat(m1) == flat(m2)
        assert [cb for _, cb in flat(m1)] == [f"cb_{i}" for i in range(5)]

    def test_truncation_at_24_with_ellipsis(self) -> None:
        long_label = "x" * 30
        short_label = "y" * 24
        m = ui_kit.menu("T", [(long_label, "a"), (short_label, "b")])[1]
        assert flat(m)[0][0] == "x" * 23 + "…"
        assert flat(m)[1][0] == short_label  # exactly at limit stays whole

    def test_more_than_8_rows_clamped(self) -> None:
        items = [(f"Item {i}", f"cb_{i}") for i in range(12)]
        _, m = ui_kit.menu("T", items)
        rows = m.inline_keyboard
        assert len(rows) == MENU_MAX_ROWS
        assert len(buttons(m)) == MENU_MAX_ROWS
        # The clamp keeps the FIRST rows, drops the tail.
        assert flat(m)[0][1] == "cb_0"
        assert flat(m)[-1][1] == f"cb_{MENU_MAX_ROWS - 1}"

    def test_back_row_appended_after_clamp(self) -> None:
        items = [(f"Item {i}", f"cb_{i}") for i in range(10)]
        _, m = ui_kit.menu("T", items, back="menu:root")
        assert m.inline_keyboard[-1][0].callback_data == "menu:root"
        assert m.inline_keyboard[-1][0].text == translate("ui_back")
        assert len(m.inline_keyboard) == MENU_MAX_ROWS + 1

    def test_no_back_when_absent(self) -> None:
        _, m = ui_kit.menu("T", [("A", "a")])
        assert len(m.inline_keyboard) == 1


class TestKvPanel:
    def test_aligned_labels_and_fence(self) -> None:
        text, markup = ui_kit.kv_panel("Status", [("IP", "1.2.3.4"), ("Hostname", "web")])
        assert tuple(markup.inline_keyboard) == ()
        body = text.split("```\n", 1)[1].rsplit("\n```", 1)[0]
        lines = body.split("\n")
        assert lines == ["IP       : 1.2.3.4", "Hostname : web"]
        assert text.startswith("Status\n```")

    def test_fence_survives_hostile_values(self) -> None:
        """Backticks/quotes/fence-substrings in user data cannot break the fence."""
        hostile = [
            ("Name", "we`b 'quoted'"),
            ("Evil", "```"),
            ("Other", "value with ' and \" quotes"),
        ]
        text, _ = ui_kit.kv_panel("P", hostile)
        # One opening and one closing fence only: the fence triple in user
        # data is defanged, so nothing inside the block can close it early.
        assert text.count("```") == 2
        assert text.endswith("\n```")
        body = text.split("```\n", 1)[1].rsplit("\n```", 1)[0]
        assert "we`b 'quoted'" in body
        assert "```" not in body  # defanged, no real fence inside
        assert "Evil" in body

    def test_empty_pairs_render_localized_placeholder(self) -> None:
        text, _ = ui_kit.kv_panel("P", [])
        assert translate("ui_kv_empty") in text


class TestPaged:
    ROWS = [(f"row{i}", f"cb{i}") for i in range(14)]  # 3 pages of 6,6,2

    def test_window_shape_and_nav_callbacks(self) -> None:
        page = ui_kit.paged(self.ROWS, 0, per_page=6, prefix="srv", title="Servers")
        assert page.page == 0 and page.pages == 3
        assert page.start == 0 and page.stop == 6
        rows = page.markup.inline_keyboard
        assert len(rows[:-1]) == 6  # window rows
        assert flat(page.markup)[:6] == [(f"row{i}", f"cb{i}") for i in range(6)]
        nav = [(b.text, b.callback_data) for b in rows[-1]]
        assert nav[0] == (translate("ui_nav_disabled"), "srv_prev_0")
        assert nav[1][1] == "srv_cur_0"
        assert nav[2] == (translate("ui_nav_next"), "srv_next_1")

    def test_last_page_next_disabled_prev_enabled(self) -> None:
        page = ui_kit.paged(self.ROWS, 99, per_page=6, prefix="srv", title="S")
        assert page.page == 2  # clamped onto last populated page
        assert page.stop == 14  # short window, never empty
        nav = [(b.text, b.callback_data) for b in page.markup.inline_keyboard[-1]]
        assert nav[0] == (translate("ui_nav_prev"), "srv_prev_1")
        assert nav[2] == (translate("ui_nav_disabled"), "srv_next_3")
        # Disabled look still carries the REAL adjacent page: a stale press
        # re-clamps into range instead of dying.
        assert int(nav[2][1].rsplit("_", 1)[1]) > page.page

    def test_stale_nav_press_reclamps(self) -> None:
        """Following prev/next callback data from any page stays in range."""
        for page_no in range(3):
            page = ui_kit.paged(self.ROWS, page_no, per_page=6, prefix="srv", title="S")
            nav = page.markup.inline_keyboard[-1]
            target = int(nav[2].callback_data.rsplit("_", 1)[1])  # next target
            follow = ui_kit.paged(self.ROWS, target, per_page=6, prefix="srv", title="S")
            assert 0 <= follow.page < follow.pages

    def test_empty_input_lands_on_page_zero(self) -> None:
        page = ui_kit.paged([], 5, prefix="srv", title="S")
        assert page.page == 0 and page.pages == 1 and page.start == page.stop == 0

    def test_nonpositive_per_page_falls_back(self) -> None:
        page = ui_kit.paged(self.ROWS, 0, per_page=0, prefix="srv", title="S")
        assert page.stop == 6

    def test_total_label_overrides_indicator(self) -> None:
        page = ui_kit.paged(self.ROWS, 1, per_page=6, prefix="srv", total_label="14 servers", title="S")
        marker = [b.text for b in page.markup.inline_keyboard[-1]]
        assert marker[1] == "14 servers"
        assert page.text.startswith("S\n14 servers")

    def test_text_carries_title_and_indicator(self) -> None:
        page = ui_kit.paged(self.ROWS, 0, per_page=6, prefix="srv", title="Servers")
        assert page.text == "Servers\n" + translate("ui_page_indicator", page=1, pages=3)


@pytest.mark.parametrize(
    ("markup_fn", "name"),
    [
        (lambda: ui_kit.menu("T", [("A", "a")])[1], "menu"),
        (lambda: ui_kit.kv_panel("T", [("a", "b")])[1], "kv_panel"),
        (lambda: ui_kit.paged([("A", "a")], 0).markup, "paged"),
    ],
)
def test_builders_only_return_inline_keyboards(markup_fn, name) -> None:
    """Every builder ships an InlineKeyboardMarkup; no reply keyboards leak."""
    import telegram

    assert isinstance(markup_fn(), telegram.InlineKeyboardMarkup), name
