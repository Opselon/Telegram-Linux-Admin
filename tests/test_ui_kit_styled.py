# PURPOSE: Prove the styled UI kit additions: deterministic icons, column
#          layout, and back-row behavior of ``styled_menu``/``section_row``.
# OWNER: ui lane, 3.5.
# CONSUMES: src.bot.ui_kit (ICONS, styled_menu, section_row).
# PROVIDES: tests/test_ui_kit_styled.py.
# INVARIANTS: same input yields byte-identical markup; labels still
#          truncate at LABEL_MAX; existing ``menu()`` behavior is untouched.
# EXTEND: new visual invariants go here.

from __future__ import annotations

from src.bot.ui_kit import ICONS, LABEL_MAX, menu, section_row, styled_menu

ITEMS = [("Status", "host_status"), ("Security", "sec_menu"), ("Files", "file_manager_menu_")]


class TestStyledMenu:
    def test_deterministic_markup(self) -> None:
        _, first = styled_menu("T", ITEMS, back="main_menu")
        _, second = styled_menu("T", ITEMS, back="main_menu")
        assert str(first) == str(second)

    def test_icons_added_when_label_has_none(self) -> None:
        text, markup = styled_menu("T", [("Status", "host_status")])
        button = markup.inline_keyboard[0][0]
        assert button.text.startswith(f"{ICONS['status']} ")
        assert button.callback_data == "host_status"

    def test_no_double_icon_when_label_has_emoji(self) -> None:
        _, markup = styled_menu("T", [(f"{ICONS['ok']} Done", "sec_menu")])
        assert markup.inline_keyboard[0][0].text == f"{ICONS['ok']} Done"

    def test_back_row_appended_last(self) -> None:
        _, markup = styled_menu("T", ITEMS, back="main_menu")
        back_button = markup.inline_keyboard[-1][0]
        assert back_button.callback_data == "main_menu"
        assert back_button.text.startswith(ICONS["back"])

    def test_single_column_default(self) -> None:
        _, markup = styled_menu("T", ITEMS)
        assert len(markup.inline_keyboard) == len(ITEMS)

    def test_two_column_layout(self) -> None:
        four = [(f"item{i}", f"cb{i}") for i in range(4)]
        _, markup = styled_menu("T", four, columns=2)
        rows = markup.inline_keyboard
        assert len(rows) == 2
        assert len(rows[0]) == 2
        assert rows[0][0].callback_data == "cb0"
        assert rows[0][1].callback_data == "cb1"

    def test_two_column_odd_item_occupies_single_cell(self) -> None:
        three = [(f"item{i}", f"cb{i}") for i in range(3)]
        _, markup = styled_menu("T", three, columns=2)
        rows = markup.inline_keyboard
        assert len(rows) == 2
        assert len(rows[1]) == 1

    def test_invalid_columns_falls_back_to_one(self) -> None:
        _, markup = styled_menu("T", ITEMS, columns=3)
        assert len(markup.inline_keyboard) == len(ITEMS)

    def test_long_labels_truncate(self) -> None:
        long_label = "x" * (LABEL_MAX + 20)
        _, markup = styled_menu("T", [(long_label, "cb")])
        assert len(markup.inline_keyboard[0][0].text) <= LABEL_MAX

    def test_row_clamp_matches_menu(self) -> None:
        ten = [(f"item{i}", f"cb{i}") for i in range(10)]
        _, markup = styled_menu("T", ten)
        assert len(markup.inline_keyboard) == 8  # MENU_MAX_ROWS


class TestSectionRow:
    def test_section_row_one_row(self) -> None:
        rows = section_row([("System", "system"), ("Security", "security")])
        assert len(rows) == 1
        assert len(rows[0]) == 2

    def test_section_row_icons(self) -> None:
        rows = section_row([("System", "system")])
        assert rows[0][0].text.startswith(ICONS["system"])


class TestMenuUnchanged:
    """The 3.4 ``menu()`` wire format must not drift (old chats still send it)."""

    def test_menu_still_plain_labels(self) -> None:
        text, markup = menu("T", [("Plain", "cb")], back="back_cb")
        assert markup.inline_keyboard[0][0].text == "Plain"
        assert markup.inline_keyboard[-1][0].callback_data == "back_cb"

    def test_icons_mapping_complete_for_used_keys(self) -> None:
        for key in ("security", "status", "home", "back", "ok", "warn", "denied", "add", "remove"):
            assert key in ICONS
