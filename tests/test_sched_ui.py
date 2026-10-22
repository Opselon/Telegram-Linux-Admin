# PURPOSE: Cover the /backups admin surface (src.sched.ui) with deterministic
#          fakes: panel rendering, ownership-guarded toggle/delete/run-now,
#          the alias → hour → keep add flow, degraded storage, and the
#          admin-only gate.  Style mirrors tests/test_jobs_ui.py.
# OWNER: lane `backup` of wave 3.4 (branch feat/3.4-backup).
# CONSUMES: src.sched.ui, src.sched.models, src.sched runner port injection,
#           src.bot.state, unittest.mock fakes of the PTB transport only.
# PROVIDES: tests/test_sched_ui.py.
# INVARIANTS: no asyncio sleep anywhere — the fake run_cmd resolves inline;
#             METRICS are restored after each test; storage is real sqlite in
#             tmp_path via the storage module's lazy _connect seam.
# EXTEND: new callback prefixes belong next to the scenario that pins them.

"""UI handler tests for the scheduled-backups admin surface."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src import database
from src.bot import state
from src.metrics import METRICS
from src.sched import storage, ui
from src.sched.models import BackupPlan

USER_A = 4242
USER_B = 4343


class _FakeUpdate:
    """A stand-in Update whose callback_query carries the exact routed data."""

    def __init__(self, data: str | None = None, user_id: int = USER_A) -> None:
        self.callback_query = MagicMock()
        self.callback_query.data = data
        self.callback_query.answer = _async_recorder("answer")
        self.callback_query.edit_message_text = _async_recorder("edit_message_text")
        self.callback_query.from_user = self.callback_query
        self.effective_user = MagicMock()
        self.effective_user.id = user_id
        self.effective_message = None
        self.effective_chat = None
        self.message = None


def _async_recorder(name: str):
    """Async mock recording calls without touching any PTB type."""
    calls: list[tuple] = []

    async def call(*args, **kwargs):
        calls.append((args, kwargs))
        return None

    call.calls = calls  # type: ignore[attr-defined]
    call.__name__ = name
    return call


def _unescaped(text: str) -> str:
    """Reverses the MarkdownV2 escaping ``_edit_message_safely`` applies."""
    special = "\\_*[]()~`>#+-=|{}.!"
    out = []
    index = 0
    while index < len(text):
        if text[index] == "\\" and index + 1 < len(text) and text[index + 1] in special:
            out.append(text[index + 1])
            index += 2
        else:
            out.append(text[index])
            index += 1
    return "".join(out)


@pytest.fixture(autouse=True)
def _storage_state(tmp_path: Path, monkeypatch):
    """Fresh sqlite per test, wired through storage._connect."""
    conn = sqlite3.connect(tmp_path / "schedules.db")
    conn.execute(storage.SCHEMA_SQL)
    monkeypatch.setattr(storage, "_connect", lambda: conn)
    state.user_language_cache.pop(USER_A, None)
    state.user_language_cache.pop(USER_B, None)
    yield conn
    conn.close()
    state.user_language_cache.pop(USER_A, None)
    state.user_language_cache.pop(USER_B, None)
    ui.set_run_cmd(None)
    ui.set_backup_base(None)


@pytest.fixture(autouse=True)
def _preserve_metrics():
    """Same registry-guard pattern as test_jobs_ui."""
    before_counters = dict(METRICS._counters)
    before_gauges = dict(METRICS._gauges)
    yield
    METRICS._counters.clear()
    METRICS._counters.update(before_counters)
    METRICS._gauges.clear()
    METRICS._gauges.update(before_gauges)


@pytest.fixture(autouse=True)
def _open_permissions(monkeypatch):
    """Admin USER_A, non-admin USER_B for every test."""
    from src.access_control import permissions

    monkeypatch.setattr(permissions, "check", lambda user_id: MagicMock(allowed=True, reason=""))
    monkeypatch.setattr(permissions, "is_admin", lambda user_id: user_id == USER_A)
    return permissions


def _edit_text(update: _FakeUpdate) -> str:
    assert update.callback_query.edit_message_text.calls, "the panel must have been edited"
    return _unescaped(update.callback_query.edit_message_text.calls[-1][0][0])


def _edit_markup(update: _FakeUpdate):
    return update.callback_query.edit_message_text.calls[-1][1]["reply_markup"]


def _callbacks(markup) -> list[str]:
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
    ]


def _drive(coro):
    return asyncio.run(coro)  # every handler awaits inline in the test loop


# -- panel ----------------------------------------------------------------------


class TestBackupsPanel:
    def test_empty_panel_shows_empty_line_and_add_button(self) -> None:
        update = _FakeUpdate(ui.MENU_CALLBACK)
        _drive(ui.backups_menu(update, None))

        text = _edit_text(update)
        assert "Scheduled backups" in text
        assert "No plans yet" in text
        assert ui.ADD_CALLBACK in _callbacks(_edit_markup(update))
        assert update.callback_query.answer.calls, "the callback must be answered first"

    def test_panel_lists_each_plan_with_action_buttons(self) -> None:
        first = storage.add_plan(BackupPlan(alias="web", hour=3, keep=7, owner_id=USER_A))
        second = storage.add_plan(
            BackupPlan(alias="db", hour=22, keep=3, enabled=False, owner_id=USER_A)
        )
        update = _FakeUpdate(ui.MENU_CALLBACK)
        _drive(ui.backups_menu(update, None))

        text = _edit_text(update)
        assert "web" in text and "db" in text
        assert "03:00" in text and "22:00" in text
        callback_data = _callbacks(_edit_markup(update))
        for plan_id in (first.id, second.id):
            assert f"{ui.TOGGLE_PREFIX}{plan_id}" in callback_data
            assert f"{ui.DELETE_PREFIX}{plan_id}" in callback_data
            assert f"{ui.RUN_PREFIX}{plan_id}" in callback_data
        assert "id=" not in text, "no plan id leaks into the visible text"

    def test_panel_hides_other_admins_plans(self) -> None:
        storage.add_plan(BackupPlan(alias="secret", hour=5, owner_id=USER_B))
        update = _FakeUpdate(ui.MENU_CALLBACK)
        _drive(ui.backups_menu(update, None))

        assert "secret" not in _edit_text(update)

    def test_enabled_mark_tracks_the_flag(self) -> None:
        plan = storage.add_plan(BackupPlan(alias="web", hour=3, owner_id=USER_A, enabled=False))
        update = _FakeUpdate(ui.MENU_CALLBACK)
        _drive(ui.backups_menu(update, None))
        text = _edit_text(update)
        assert "⏸️" in text and "✅ web" not in text
        saved = storage.get_plan(plan.id, USER_A)
        assert saved is not None and saved.enabled is False

    def test_missing_table_renders_the_empty_panel(self, monkeypatch) -> None:
        conn = sqlite3.connect(":memory:")  # no schema
        monkeypatch.setattr(storage, "_connect", lambda: conn)
        update = _FakeUpdate(ui.MENU_CALLBACK)
        _drive(ui.backups_menu(update, None))  # must not raise (law L5)

        assert "No plans yet" in _edit_text(update)
        conn.close()


# -- toggle / delete ---------------------------------------------------------------


class TestToggleAndDelete:
    def test_toggle_flips_enabled_and_updates_storage(self) -> None:
        plan = storage.add_plan(BackupPlan(alias="web", hour=3, owner_id=USER_A))
        update = _FakeUpdate(f"{ui.TOGGLE_PREFIX}{plan.id}")
        _drive(ui.sched_toggle(update, None))

        text = _edit_text(update)
        assert "disabled" in text.lower() or "⏸️" in text
        saved = storage.get_plan(plan.id, USER_A)
        assert saved is not None and saved.enabled is False
        assert METRICS.get("sched_plan_toggle_total", outcome="ok") == 1.0

    def test_toggle_twice_returns_to_enabled(self) -> None:
        plan = storage.add_plan(BackupPlan(alias="web", hour=3, owner_id=USER_A))
        _drive(ui.sched_toggle(_FakeUpdate(f"{ui.TOGGLE_PREFIX}{plan.id}"), None))
        _drive(ui.sched_toggle(_FakeUpdate(f"{ui.TOGGLE_PREFIX}{plan.id}"), None))
        saved = storage.get_plan(plan.id, USER_A)
        assert saved is not None and saved.enabled is True

    def test_toggle_is_ownership_guarded(self) -> None:
        plan = storage.add_plan(BackupPlan(alias="web", hour=3, owner_id=USER_B))
        update = _FakeUpdate(f"{ui.TOGGLE_PREFIX}{plan.id}")
        _drive(ui.sched_toggle(update, None))

        assert "no longer exists" in _edit_text(update)
        saved = storage.get_plan(plan.id, USER_B)
        assert saved is not None and saved.enabled is True
        assert METRICS.get("sched_plan_toggle_total", outcome="miss") == 1.0

    def test_delete_removes_the_plan(self) -> None:
        plan = storage.add_plan(BackupPlan(alias="web", hour=3, owner_id=USER_A))
        update = _FakeUpdate(f"{ui.DELETE_PREFIX}{plan.id}")
        _drive(ui.sched_delete(update, None))

        assert "deleted" in _edit_text(update).lower()
        assert storage.get_plan(plan.id, USER_A) is None
        assert METRICS.get("sched_plan_delete_total", outcome="ok") == 1.0

    def test_delete_of_a_foreign_or_unknown_plan_is_degraded(self) -> None:
        plan = storage.add_plan(BackupPlan(alias="web", hour=3, owner_id=USER_B))
        update = _FakeUpdate(f"{ui.DELETE_PREFIX}{plan.id}")
        _drive(ui.sched_delete(update, None))

        assert "no longer exists" in _edit_text(update)
        assert storage.get_plan(plan.id, USER_B) is not None, "the owner's plan survives"


# -- run now -----------------------------------------------------------------------


class TestRunNow:
    def test_run_now_reports_success_with_the_archive_path(self, monkeypatch, tmp_path) -> None:
        plan = storage.add_plan(BackupPlan(alias="web", hour=3, owner_id=USER_A))

        async def run_cmd(alias: str) -> Path:
            archive = tmp_path / f"{alias}.zip"
            archive.write_text("bytes")
            return archive

        ui.set_run_cmd(run_cmd)
        ui.set_backup_base(tmp_path / "scheduled")
        update = _FakeUpdate(f"{ui.RUN_PREFIX}{plan.id}")
        _drive(ui.sched_run(update, None))

        text = _edit_text(update)
        assert "completed" in text
        stored = list((tmp_path / "scheduled" / "web").iterdir())
        assert len(stored) == 1 and str(stored[0].name) in text, (
            "the stored archive path is reported"
        )
        assert "bytes" not in text
        assert METRICS.get("sched_backup_total", status="ok") == 1.0

    def test_run_now_reports_failure_without_raising(self, monkeypatch) -> None:
        plan = storage.add_plan(BackupPlan(alias="web", hour=3, owner_id=USER_A))

        async def run_cmd(alias: str) -> Path | None:
            return None

        ui.set_run_cmd(run_cmd)
        update = _FakeUpdate(f"{ui.RUN_PREFIX}{plan.id}")
        _drive(ui.sched_run(update, None))

        assert "failed" in _edit_text(update).lower()
        assert METRICS.get("sched_backup_total", status="retrieval_failed") == 1.0

    def test_run_now_without_a_wired_producer_degrades(self) -> None:
        plan = storage.add_plan(BackupPlan(alias="web", hour=3, owner_id=USER_A))
        ui.set_run_cmd(None)
        update = _FakeUpdate(f"{ui.RUN_PREFIX}{plan.id}")
        _drive(ui.sched_run(update, None))  # must not raise

        assert "failed" in _edit_text(update).lower()

    def test_run_now_is_ownership_guarded(self) -> None:
        plan = storage.add_plan(BackupPlan(alias="web", hour=3, owner_id=USER_B))
        update = _FakeUpdate(f"{ui.RUN_PREFIX}{plan.id}")
        _drive(ui.sched_run(update, None))

        assert "no longer exists" in _edit_text(update)


# -- add flow: alias -> hour -> keep -> confirm --------------------------------------


class TestAddFlow:
    def test_no_servers_yet_explains_itself(self, monkeypatch) -> None:
        monkeypatch.setattr(database, "get_all_servers", lambda owner_id=None: [])
        update = _FakeUpdate(ui.ADD_CALLBACK)
        _drive(ui.sched_add_start(update, None))

        text = _edit_text(update)
        assert "server" in text.lower()
        assert ui.ADD_ALIAS_PREFIX not in _callbacks(_edit_markup(update))

    def test_start_lists_the_admins_servers(self, monkeypatch) -> None:
        monkeypatch.setattr(
            database,
            "get_all_servers",
            lambda owner_id=None: [
                {"alias": "web"},
                {"alias": "db"},
                {"alias": ""},  # a malformed row must be skipped, not crash
            ],
        )
        update = _FakeUpdate(ui.ADD_CALLBACK)
        _drive(ui.sched_add_start(update, None))

        data = _callbacks(_edit_markup(update))
        assert f"{ui.ADD_ALIAS_PREFIX}web" in data
        assert f"{ui.ADD_ALIAS_PREFIX}db" in data

    def test_hour_step_offers_12_choices_in_rows(self) -> None:
        update = _FakeUpdate(f"{ui.ADD_ALIAS_PREFIX}web")
        _drive(ui.sched_add_alias(update, None))

        markup = _edit_markup(update)
        data = _callbacks(markup)
        hours = [int(entry.rpartition("_")[2]) for entry in data if entry.startswith(ui.ADD_HOUR_PREFIX)]
        assert hours == list(ui.HOUR_CHOICES)
        assert len(hours) == 12
        assert len(markup.inline_keyboard) >= 2, "12 buttons split onto several rows"
        assert ui.ADD_CALLBACK in data, "the flow can step back to the alias picker"

    def test_keep_step_carries_alias_and_hour(self) -> None:
        update = _FakeUpdate(f"{ui.ADD_HOUR_PREFIX}web_14")
        _drive(ui.sched_add_hour(update, None))

        data = _callbacks(_edit_markup(update))
        assert f"{ui.ADD_KEEP_PREFIX}web_14_7" in data
        assert f"{ui.ADD_KEEP_PREFIX}web_14_30" in data

    def test_keep_step_rejects_a_non_choice_hour(self) -> None:
        update = _FakeUpdate(f"{ui.ADD_HOUR_PREFIX}web_37")
        _drive(ui.sched_add_hour(update, None))
        assert "no longer exists" in _edit_text(update)

    def test_confirm_persists_the_plan_with_the_pressing_owner(self) -> None:
        update = _FakeUpdate(f"{ui.ADD_KEEP_PREFIX}web_14_14")
        _drive(ui.sched_add_keep(update, None))

        text = _edit_text(update)
        assert "saved" in text.lower() or "keep" in text.lower()
        assert ui.CONFIRM_PREFIX + "web_14_14" in _callbacks(_edit_markup(update))

    def test_save_persists_ownership_and_defaults(self) -> None:
        update = _FakeUpdate(f"{ui.CONFIRM_PREFIX}web_14_14")
        _drive(ui.sched_add_confirm(update, None))

        plans = storage.list_plans(USER_A)
        assert len(plans) == 1
        saved = plans[0]
        assert saved.alias == "web" and saved.hour == 14 and saved.keep == 14
        assert saved.owner_id == USER_A and saved.enabled is True
        assert "saved" in _edit_text(update).lower()
        assert METRICS.get("sched_plan_add_total", outcome="ok") == 1.0

    def test_save_rejects_a_path_escape_alias(self) -> None:
        update = _FakeUpdate(f"{ui.CONFIRM_PREFIX}../evil_14_14")
        _drive(ui.sched_add_confirm(update, None))  # must not raise

        assert "no longer exists" in _edit_text(update)
        assert storage.list_plans(USER_A) == []
        assert METRICS.get("sched_plan_add_total", outcome="invalid") == 1.0

    def test_save_of_a_foreign_alias_is_not_shown_to_others(self) -> None:
        """Two admins can hold their own plan for the same alias."""
        storage.add_plan(BackupPlan(alias="web", hour=5, owner_id=USER_B))
        update = _FakeUpdate(f"{ui.CONFIRM_PREFIX}web_14_14")
        _drive(ui.sched_add_confirm(update, None))

        assert len(storage.list_plans(USER_B)) == 1
        assert len(storage.list_plans(USER_A)) == 1
        assert storage.list_plans(USER_A)[0].owner_id == USER_A


# -- admin gate ----------------------------------------------------------------------


class TestAdminGate:
    def test_non_admin_cannot_open_or_mutate_anything(self, monkeypatch) -> None:
        from src.access_control import permissions

        plan = storage.add_plan(BackupPlan(alias="web", hour=3, owner_id=USER_A))
        monkeypatch.setattr(permissions, "check", lambda user_id: MagicMock(allowed=True, reason="x"))
        monkeypatch.setattr(permissions, "is_admin", lambda user_id: False)

        for handler, data in (
            (ui.backups_menu, ui.MENU_CALLBACK),
            (ui.sched_toggle, f"{ui.TOGGLE_PREFIX}{plan.id}"),
            (ui.sched_delete, f"{ui.DELETE_PREFIX}{plan.id}"),
            (ui.sched_run, f"{ui.RUN_PREFIX}{plan.id}"),
            (ui.sched_add_start, ui.ADD_CALLBACK),
            (ui.sched_add_alias, f"{ui.ADD_ALIAS_PREFIX}web"),
            (ui.sched_add_hour, f"{ui.ADD_HOUR_PREFIX}web_14"),
            (ui.sched_add_keep, f"{ui.ADD_KEEP_PREFIX}web_3_7"),
            (ui.sched_add_confirm, f"{ui.CONFIRM_PREFIX}web_14_7"),
        ):
            update = _FakeUpdate(data, user_id=USER_B)
            _drive(handler(update, None))
            saved = storage.get_plan(plan.id, USER_A)
            assert saved is not None and saved.enabled is True, "state untouched"

        assert METRICS.get("admin_access_denied_total") == 9.0


def test_set_and_get_run_cmd_roundtrip() -> None:
    async def producer(alias: str) -> Path | None:
        return None

    assert ui.get_run_cmd() is None
    ui.set_run_cmd(producer)
    assert ui.get_run_cmd() is producer
    ui.set_run_cmd(None)
    assert ui.get_run_cmd() is None
