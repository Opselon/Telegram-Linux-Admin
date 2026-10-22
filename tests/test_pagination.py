# PURPOSE: Prove the pagination helpers (window math, clamping, callback
#          strings) behave statelessly and in range under adversarial pages.
# OWNER: lane `ui-ux` of wave 3.3 (branch feat/3.3-ui-ux).
# CONSUMES: src.bot.pagination.
# PROVIDES: tests/test_pagination.py.
# INVARIANTS: every clamp/marketing claim in the module docstring gets a
#          failing test if it regresses; boundaries (0, last page, past
#          last, negative, huge) are all covered.
# EXTEND: new numeric invariants go here; keyboard shape checks stay in
#          tests/test_ui_kit.py.

from __future__ import annotations

import pytest

from src.bot import pagination


class TestClampPage:
    def test_zero_and_negative_fold_to_first_page(self) -> None:
        assert pagination.clamp_page(-7, 5) == 0
        assert pagination.clamp_page(0, 5) == 0

    def test_within_range_is_identity(self) -> None:
        for p in range(5):
            assert pagination.clamp_page(p, 5) == p

    def test_last_true_page_is_valid(self) -> None:
        assert pagination.clamp_page(4, 5) == 4

    def test_beyond_range_folds_to_last_populated_page(self) -> None:
        assert pagination.clamp_page(400, 5) == 4

    def test_empty_total_returns_zero(self) -> None:
        assert pagination.clamp_page(3, 0) == 0
        assert pagination.clamp_page(0, 0) == 0
        assert pagination.clamp_page(-1, 0) == 0


class TestPageCount:
    def test_exact_multiples(self) -> None:
        assert pagination.page_count(18, 6) == 3

    def test_partial_last_page_counts(self) -> None:
        assert pagination.page_count(14, 6) == 3
        assert pagination.page_count(1, 6) == 1

    def test_empty_is_one_page(self) -> None:
        assert pagination.page_count(0, 6) == 1

    def test_zero_per_page_never_zero_pages(self) -> None:
        assert pagination.page_count(10, 0) == 1


class TestComputeWindow:
    def test_window_bounds(self) -> None:
        assert pagination.compute_window(20, 0, 6) == (0, 6)
        assert pagination.compute_window(20, 3, 6) == (18, 20)
        assert pagination.compute_window(20, 2, 6) == (12, 18)

    def test_out_of_range_page_yields_last_populated_window(self) -> None:
        assert pagination.compute_window(14, 999, 6) == (12, 14)
        assert pagination.compute_window(14, -3, 6) == (0, 6)

    def test_empty_total_window_is_empty(self) -> None:
        assert pagination.compute_window(0, 0, 6) == (0, 0)


class TestNavFlags:
    def test_flags_straddle_boundaries(self) -> None:
        assert pagination.has_prev(0) is False
        assert pagination.has_prev(1) is True
        assert pagination.has_next(3, 5) is True
        assert pagination.has_next(4, 5) is False
        assert pagination.has_next(4, 1) is False  # single page: no motion

    def test_single_page_has_no_motion(self) -> None:
        assert pagination.has_prev(0) is False
        assert pagination.has_next(0, 1) is False


class TestCallbackStrings:
    def test_prev_next_strings(self) -> None:
        assert pagination.prev_callback("srv", 2) == "srv_prev_2"
        assert pagination.next_callback("srv", 3) == "srv_next_3"

    def test_negative_target_never_negative_in_string(self) -> None:
        assert pagination.prev_callback("srv", -1) == "srv_prev_0"

    def test_parse_roundtrip(self) -> None:
        assert pagination.parse_target_page("srv_prev_4") == 4
        assert pagination.parse_target_page("srv_next_12") == 12

    def test_parse_rejects_non_nav_strings(self) -> None:
        assert pagination.parse_target_page("srv_prev_x") == -1
        assert pagination.parse_target_page("srv") == -1
        assert pagination.parse_target_page("") == -1

    def test_stale_press_stays_in_range(self) -> None:
        """Following any nav string emitted on any page re-clamps, in range."""
        total, per_page = 14, 6
        for page_no in range(pagination.page_count(total, per_page)):
            page = pagination.clamp_page(page_no, pagination.page_count(total, per_page))
            assert pagination.clamp_page(pagination.next_callback("srv", page + 1).rsplit("_", 1)[1] and page + 1, pagination.page_count(total, per_page)) == pagination.clamp_page(page + 1, pagination.page_count(total, per_page))
            assert pagination.clamp_page(max(0, page - 1), pagination.page_count(total, per_page)) == max(0, page - 1)


@pytest.mark.parametrize(
    ("total", "per_page", "page", "expected"),
    [
        (20, 6, 0, (0, 6)),
        (20, 6, 3, (18, 20)),
        (20, 6, 50, (18, 20)),
        (0, 6, 5, (0, 0)),
    ],
)
def test_window_invariants_table(total: int, per_page: int, page: int, expected: tuple[int, int]) -> None:
    assert pagination.compute_window(total, page, per_page) == expected
