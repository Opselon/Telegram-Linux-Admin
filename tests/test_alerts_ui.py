# PURPOSE: Cover the 3.4 alerts Telegram panel end to end with deterministic
#          fakes (no PTB transport, no network): the /alerts server menu and
#          recent-events summary, the rules pane, the add flow's three steps
#          plus save, ownership on toggle/delete/add, and the degraded paths.
# OWNER: lane `alerts` of wave 3.4 (branch feat/3.4-alerts).
# CONSUMES: src.alerts.ui, src.alerts.storage, src.bot.state, src.metrics.
#           unittest.mock-free fakes of the PTB transport only.
# PROVIDES: tests/test_alerts_ui.py.
# INVARIANTS: handlers get an Update substitute whose callback_query carries
#             the exact data string application.py routes
#             ('alertsmenu_', 'alertsadd_', 'alertsconfirm_', 'alertstoggle_',
#             'alertsdel_'); every mutation is asserted against real sqlite
#             rows, never a mock; METRICS counters are restored afterwards so
#             the shared registry stays untouched for other test files.
# EXTEND: new panel behavior goes here next to the scenario that pins it.

"""UI tests for the wave 3.4 alerts panel (mirrors tests/test_jobs_ui.py)."""

from __future__ import annotations

import contextlib
import sqlite3

import pytest

from src.alerts.models import AlertEvent
from src.alerts.storage import (
    SCHEMA_INDEX_SQL,
    SCHEMA_SQL,
    add_rule,
    delete_rule,
    get_rule,
    list_rules,
    record_event,
)
from src.alerts.ui import (
    ADD_PREFIX,
    CONFIRM_PREFIX,
    DEL_PREFIX,
    MENU_CALLBACK,
    MENU_PREFIX,
    THRESHOLDS,
    TOGGLE_PREFIX,
    _parse_add_payload,
    alerts_add_callback,
    alerts_command,
    alerts_confirm_callback,
    alerts_delete_callback,
    alerts_menu_callback,
    alerts_toggle_callback,
)
from src.bot import state
from src.metrics import METRICS

pytestmark = pytest.mark.asyncio

USER_A = 4242
USER_B = 4343

#: pinned epoch so every rendered timestamp is deterministic.
T0 = 1_700_000_000.0


def _async_recorder(name: str):
    """Async callable that records calls without touching any PTB type."""
    calls: list[tuple] = []

    async def call(*args, **kwargs):
        calls.append((args, kwargs))
        return None

    call.calls = calls  # type: ignore[attr-defined]
    call.__name__ = name
    return call


class _SimpleUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class _SimpleChat:
    def __init__(self) -> None:
        self.send_message = _async_recorder("send_message")


class _FakeUpdate:
    """An Update stand-in: button presses carry data, commands carry a chat."""

    def __init__(self, data: str | None, user_id: int | None = USER_A) -> None:
        self.callback_query = None
        if data is not None:
            query = type("CallbackQuery", (), {})()
            query.data = data
            query.answer = _async_recorder("answer")
            query.edit_message_text = _async_recorder("edit_message_text")
            query.from_user = None if user_id is None else _SimpleUser(user_id)
            self.callback_query = query
        self.effective_user = None if user_id is None else _SimpleUser(user_id)
        self.effective_chat = None if user_id is None else _SimpleChat()
        self.effective_message = None
        self.message = None


@pytest.fixture(autouse=True)
def alerts_env(monkeypatch, tmp_path):
    """Isolated sqlite (schema from SCHEMA_SQL, as the contract requires)."""
    conn = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA_SQL)
    conn.execute(SCHEMA_INDEX_SQL)

    @contextlib.contextmanager
    def fake_transaction():
        yield conn

    servers = {USER_A: ["web-1", "db-2"], USER_B: ["other-1"]}
    monkeypatch.setattr(
        "src.alerts.ui.get_all_servers",
        lambda oid: [{"alias": a} for a in servers.get(oid, [])],
    )
    monkeypatch.setattr("src.alerts.storage.get_db_connection", lambda: conn)
    monkeypatch.setattr("src.alerts.storage.transaction", fake_transaction)
    # state._get_user_language() falls through to the real database module on a
    # cache miss, which would open (and cache) the production database.db and
    # break any later test that removes it.  Pin the language instead.
    monkeypatch.setattr(
        "src.bot.state.get_user_language_preference", lambda _uid: "en"
    )

    from src.audit import AuditLogger

    monkeypatch.setattr(
        "src.alerts.storage.audit", AuditLogger(tmp_path / "audit", filename="audit.log")
    )

    state.user_language_cache.pop(USER_A, None)
    state.user_language_cache.pop(USER_B, None)
    before_counters = dict(METRICS._counters)
    before_gauges = dict(METRICS._gauges)

    yield conn

    METRICS._counters.clear()
    METRICS._counters.update(before_counters)
    METRICS._gauges.clear()
    METRICS._gauges.update(before_gauges)
    conn.close()


_SPECIAL = "\\_*[]()~`>#+-=|{}.!"


def _unescape(text: str) -> str:
    """Reverses the MarkdownV2 escaping ``_edit_message_safely`` applies."""
    out: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text) and text[i + 1] in _SPECIAL:
            out.append(text[i + 1])
            i += 2
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _edit_text(update: _FakeUpdate) -> str:
    """The text the last panel edit left on the message (user-render form)."""
    calls = update.callback_query.edit_message_text.calls
    assert calls, "the panel must have been edited"
    return _unescape(calls[-1][0][0])


def _edit_markup(update: _FakeUpdate):
    calls = update.callback_query.edit_message_text.calls
    assert calls, "the panel must have been edited"
    return calls[-1][1]["reply_markup"]


def _buttons(update: _FakeUpdate) -> list[str]:
    markup = _edit_markup(update)
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def _sent_text(update: _FakeUpdate) -> str:
    calls = update.effective_chat.send_message.calls
    assert calls, "the command must reply"
    return _unescape(calls[-1][0][0])


# -- /alerts ------------------------------------------------------------------------


async def test_alerts_command_lists_only_owned_servers() -> None:
    update = _FakeUpdate(None)
    await alerts_command(update, None)  # type: ignore[arg-type]

    text = _sent_text(update)
    markup = update.effective_chat.send_message.calls[-1][1]["reply_markup"]
    data = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert f"{MENU_PREFIX}web-1" in data and f"{MENU_PREFIX}db-2" in data
    assert f"{MENU_PREFIX}other-1" not in data, "another user's server is not offered"
    assert "Recent alerts" in text, "the menu carries the events summary"


async def test_alerts_command_without_servers_degrades_to_hint() -> None:
    update = _FakeUpdate(None, user_id=USER_B)
    await alerts_command(update, None)  # type: ignore[arg-type]
    # A stranger owns no servers, so the menu degrades to the empty hint plus
    # the built-in local host row ("self" is always alertable: 3.4 wiring).
    # A stranger has no servers: the menu is the command path (sends, not
    # edits), showing the built-in local host row ("self" is always
    # alertable) but none of USER_B's servers.
    update = _FakeUpdate(None, user_id=999)
    await alerts_command(update, None)  # type: ignore[arg-type]
    assert "other-1" not in _sent_text(update)
    markup = update.effective_chat.send_message.calls[-1][1]["reply_markup"]
    sent_buttons = [
        b.callback_data for row in markup.inline_keyboard for b in row
    ] if markup else []
    assert f"{MENU_PREFIX}other-1" not in sent_buttons
    assert f"{MENU_PREFIX}self" in sent_buttons
    assert METRICS.get("alerts_panel_views_total") >= 1


async def test_alerts_command_shows_recent_events() -> None:
    record_event(
        AlertEvent(
            owner_id=USER_A, alias="web-1", kind="mem_high", threshold=80,
            value=91.5, fired_at=T0, message="m",
        )
    )
    update = _FakeUpdate(None)
    await alerts_command(update, None)  # type: ignore[arg-type]

    text = _sent_text(update)
    assert "web-1" in text
    assert "91.5" in text, "the events block must show the measured value"

    # USER_B never sees USER_A's fired event.
    other = _FakeUpdate(None, user_id=USER_B)
    await alerts_command(other, None)  # type: ignore[arg-type]
    assert "91.5" not in _sent_text(other)


# -- pane ---------------------------------------------------------------------------


async def test_menu_callback_renders_rows_for_owner() -> None:
    add_rule(USER_A, "web-1", "mem_high", 80)
    update = _FakeUpdate(f"{MENU_PREFIX}web-1")
    await alerts_menu_callback(update, None)  # type: ignore[arg-type]

    assert update.callback_query.answer.calls, "every callback is answered first"
    data = _buttons(update)
    assert any(d.startswith(TOGGLE_PREFIX) for d in data)
    assert any(d.startswith(DEL_PREFIX) for d in data)
    assert f"{ADD_PREFIX}web-1" in data
    assert "mem" in _edit_text(update).lower()


async def test_menu_callback_empty_pane_still_offers_add() -> None:
    update = _FakeUpdate(f"{MENU_PREFIX}web-1")
    await alerts_menu_callback(update, None)  # type: ignore[arg-type]
    assert f"{ADD_PREFIX}web-1" in _buttons(update)


async def test_menu_callback_rejects_foreign_alias() -> None:
    """USER_B pressing USER_A's alias must not see that server's rules."""
    add_rule(USER_A, "web-1", "mem_high", 80)
    update = _FakeUpdate(f"{MENU_PREFIX}web-1", user_id=USER_B)
    await alerts_menu_callback(update, None)  # type: ignore[arg-type]

    data = _buttons(update)
    assert not any(d.startswith(TOGGLE_PREFIX) for d in data)
    assert not any(d.startswith(DEL_PREFIX) for d in data)
    assert f"{ADD_PREFIX}web-1" not in data
    assert data == [MENU_CALLBACK], "only the Back row survives"


async def test_menu_callback_empty_alias_payload_is_graceful() -> None:
    update = _FakeUpdate(f"{MENU_PREFIX}   ")
    await alerts_menu_callback(update, None)  # type: ignore[arg-type]
    assert METRICS.get("alerts_panel_degraded_total", reason="menu_payload") >= 1


# -- add flow -----------------------------------------------------------------------


async def test_add_flow_three_steps_then_saves() -> None:
    # step 1: alias -> kind chooser
    update = _FakeUpdate(f"{ADD_PREFIX}web-1")
    await alerts_add_callback(update, None)  # type: ignore[arg-type]
    data = _buttons(update)
    assert f"{ADD_PREFIX}web-1_mem_high" in data

    # step 2: kind -> threshold row
    update = _FakeUpdate(f"{ADD_PREFIX}web-1_mem_high")
    await alerts_add_callback(update, None)  # type: ignore[arg-type]
    data = _buttons(update)
    for threshold in THRESHOLDS:
        assert f"{ADD_PREFIX}web-1_mem_high_{threshold}" in data

    # step 3: threshold -> confirm
    update = _FakeUpdate(f"{ADD_PREFIX}web-1_mem_high_80")
    await alerts_add_callback(update, None)  # type: ignore[arg-type]
    assert f"{CONFIRM_PREFIX}web-1_mem_high_80" in _buttons(update)

    # step 4: confirm -> persisted + toast
    update = _FakeUpdate(f"{CONFIRM_PREFIX}web-1_mem_high_80")
    await alerts_confirm_callback(update, None)  # type: ignore[arg-type]

    stored = list_rules(USER_A, alias="web-1")
    assert len(stored) == 1
    assert stored[0].kind == "mem_high" and stored[0].threshold == 80
    assert METRICS.get("alerts_rules_saved_total", outcome="ok") >= 1


async def test_add_flow_rejects_foreign_alias() -> None:
    """A crafted add callback for someone else's server saves nothing."""
    update = _FakeUpdate(f"{ADD_PREFIX}web-1", user_id=USER_B)
    await alerts_add_callback(update, None)  # type: ignore[arg-type]
    assert list_rules(USER_B) == []
    assert METRICS.get("alerts_panel_degraded_total", reason="add_alias") >= 1


async def test_add_flow_unknown_kind_degrades() -> None:
    update = _FakeUpdate(f"{ADD_PREFIX}web-1_gpu_high")
    await alerts_add_callback(update, None)  # type: ignore[arg-type]
    assert list_rules(USER_A) == []
    assert METRICS.get("alerts_panel_degraded_total", reason="add_kind") >= 1


async def test_add_flow_rejects_threshold_off_the_grid() -> None:
    update = _FakeUpdate(f"{ADD_PREFIX}web-1_mem_high_85")
    await alerts_add_callback(update, None)  # type: ignore[arg-type]
    assert list_rules(USER_A) == []
    assert METRICS.get("alerts_panel_degraded_total", reason="add_threshold") >= 1


async def test_confirm_duplicate_is_reported_not_saved() -> None:
    add_rule(USER_A, "web-1", "mem_high", 80)
    update = _FakeUpdate(f"{CONFIRM_PREFIX}web-1_mem_high_70")
    await alerts_confirm_callback(update, None)  # type: ignore[arg-type]

    rules = list_rules(USER_A, alias="web-1")
    assert len(rules) == 1 and rules[0].threshold == 80, "no silent second rule"
    assert METRICS.get("alerts_rules_saved_total", outcome="duplicate") >= 1


async def test_confirm_foreign_alias_saves_nothing() -> None:
    update = _FakeUpdate(f"{CONFIRM_PREFIX}web-1_mem_high_80", user_id=USER_B)
    await alerts_confirm_callback(update, None)  # type: ignore[arg-type]
    assert list_rules(USER_B) == []


# -- toggle -------------------------------------------------------------------------


async def test_toggle_pauses_then_resumes() -> None:
    rule = add_rule(USER_A, "web-1", "mem_high", 80)
    assert rule.id is not None

    update = _FakeUpdate(f"{TOGGLE_PREFIX}{rule.id}")
    await alerts_toggle_callback(update, None)  # type: ignore[arg-type]
    paused = get_rule(rule.id, USER_A)
    assert paused is not None and paused.enabled is False

    update = _FakeUpdate(f"{TOGGLE_PREFIX}{rule.id}")
    await alerts_toggle_callback(update, None)  # type: ignore[arg-type]
    resumed = get_rule(rule.id, USER_A)
    assert resumed is not None and resumed.enabled is True


async def test_toggle_unknown_id_degrades() -> None:
    update = _FakeUpdate(f"{TOGGLE_PREFIX}99999")
    await alerts_toggle_callback(update, None)  # type: ignore[arg-type]
    assert METRICS.get("alerts_panel_degraded_total", reason="toggle_missing") >= 1


async def test_toggle_is_ownership_scoped() -> None:
    rule = add_rule(USER_A, "web-1", "mem_high", 80)
    assert rule.id is not None
    update = _FakeUpdate(f"{TOGGLE_PREFIX}{rule.id}", user_id=USER_B)
    await alerts_toggle_callback(update, None)  # type: ignore[arg-type]

    still = get_rule(rule.id, USER_A)
    assert still is not None and still.enabled is True, "a foreign toggle is a no-op"


# -- delete -------------------------------------------------------------------------


async def test_delete_removes_rule_and_refreshes_pane() -> None:
    rule = add_rule(USER_A, "web-1", "mem_high", 80)
    assert rule.id is not None

    update = _FakeUpdate(f"{DEL_PREFIX}{rule.id}")
    await alerts_delete_callback(update, None)  # type: ignore[arg-type]

    assert get_rule(rule.id, USER_A) is None
    assert list_rules(USER_A) == []
    assert f"{ADD_PREFIX}web-1" in _buttons(update)


async def test_delete_is_ownership_scoped() -> None:
    rule = add_rule(USER_A, "web-1", "mem_high", 80)
    assert rule.id is not None
    update = _FakeUpdate(f"{DEL_PREFIX}{rule.id}", user_id=USER_B)
    await alerts_delete_callback(update, None)  # type: ignore[arg-type]

    assert get_rule(rule.id, USER_A) is not None, "a foreign delete is a no-op"
    assert delete_rule(rule.id, USER_A) is True


async def test_delete_unknown_id_degrades() -> None:
    update = _FakeUpdate(f"{DEL_PREFIX}abc")
    await alerts_delete_callback(update, None)  # type: ignore[arg-type]
    assert METRICS.get("alerts_panel_degraded_total", reason="delete_missing") >= 1


# -- add payload parser ----------------------------------------------------------


async def test_parser_resolves_alias_with_underscores_and_known_kind():
    """The longest owned alias wins; kinds may carry their own underscore."""
    parse = _parse_add_payload
    owned = ("web-1", "web-1_prod")
    assert parse("web-1_prod_mem_high_80", owned) == ("web-1_prod", "mem_high", 80)
    assert parse("web-1_prod_mem_high", owned) == ("web-1_prod", "mem_high", None)
    assert parse("web-1_prod", owned) == ("web-1_prod", None, None)
    # ``web-1_mem_high`` must NOT swallow the alias as ``web-1_mem``.
    assert parse("web-1_mem_high", ("web-1",)) == ("web-1", "mem_high", None)
    assert parse("web-1_cpu_high_90", ("web-1",)) == ("web-1", "cpu_high", 90)
    # Unknown kind: alias still resolves, kind stays unknown (degraded later).
    assert parse("web-1_gpu_high", ("web-1",)) == ("web-1", "gpu_high", None)
    assert parse("web-1_gpu_high_85", ("web-1",)) == ("web-1", "gpu_high", 85)
    # A stranger's payload has no owned alias: best-effort, never a match.
    assert parse("other-1_mem_high_80", ("web-1",)) == ("other-1", "mem_high", 80)
    assert parse("", ("web-1",)) == ("", None, None)


# -- no-identity / malformed updates -------------------------------------------------


async def test_callbacks_without_a_callback_query_are_noops() -> None:
    """A command-shaped update reaching a callback handler must not explode."""
    update = _FakeUpdate(None)
    for handler in (
        alerts_menu_callback,
        alerts_add_callback,
        alerts_confirm_callback,
        alerts_toggle_callback,
        alerts_delete_callback,
    ):
        await handler(update, None)  # type: ignore[arg-type]
    assert update.callback_query is None
