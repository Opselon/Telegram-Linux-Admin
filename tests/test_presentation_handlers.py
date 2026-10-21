"""Handler tests: menu flows wired through a fake Telegram and catalog.

No bot token, no network: the handlers are pure functions over an injected
:class:`UiContext`, which is exactly what the composition root provides at
runtime.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from src.application.catalog import TargetCatalog
from src.application.dto import Principal
from src.domain.policies import TargetAccessPolicy
from src.domain.targets import ServerTarget, TargetKind
from src.presentation import handlers as ui
from src.presentation.handlers import UiContext


class FakeRepository:
    def __init__(self) -> None:
        self.rows = {
            1: {
                "web": ServerTarget(
                    alias="web", kind=TargetKind.SSH, owner_id=1, hostname="10.0.0.5", user="root"
                )
            }
        }

    def list_targets(self, owner_id):
        return list(self.rows.get(owner_id, {}).values())

    def find(self, owner_id, alias):
        return self.rows.get(owner_id, {}).get(alias)

    def exists(self, owner_id, alias):
        return alias in self.rows.get(owner_id, {})

    def remove(self, owner_id, alias):
        return self.rows.get(owner_id, {}).pop(alias, None) is not None

    def count(self, owner_id):
        return len(self.rows.get(owner_id, {}))


class Recorder:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.edited: list[dict[str, Any]] = []
        self.answers: list[tuple[str | None, bool]] = []

    async def send(self, chat, text, user_id, **kwargs):
        self.sent.append({"text": text, "markup": kwargs.get("reply_markup")})
        return SimpleNamespace(chat=chat)

    async def edit(self, message, text, user_id, **kwargs):
        self.edited.append({"text": text, "markup": kwargs.get("reply_markup")})
        return message

    async def answer(self, query, text=None, alert=False):
        self.answers.append((text, bool(alert)))

    @property
    def last_text(self) -> str:
        return (self.edited or self.sent)[-1]["text"]

    def last_keyboard(self) -> list[list[tuple[str, str]]]:
        markup = (self.edited or self.sent)[-1]["markup"]
        if markup is None:
            return []
        return [[(b.text, b.callback_data) for b in row] for row in markup.inline_keyboard]

    def flat_callbacks(self) -> list[str]:
        return [payload for row in self.last_keyboard() for _, payload in row]


def update_for(user_id: int = 1, *, data: str = "main_menu", name: str = "Cap"):
    user = SimpleNamespace(id=user_id, full_name=name, username="cap")
    chat = SimpleNamespace(id=user_id)
    query = SimpleNamespace(
        data=data,
        from_user=user,
        message=SimpleNamespace(chat=chat),
        answer=None,
    )
    update = SimpleNamespace(
        effective_user=user,
        effective_chat=chat,
        effective_message=SimpleNamespace(chat=chat),
        callback_query=query,
        message=None,
    )
    return update


@pytest.fixture()
def admin_env(monkeypatch):
    recorder = Recorder()
    admin = {"value": True}
    active: dict[int, str | None] = {}
    persisted: list[tuple[int, str]] = []
    repo = FakeRepository()
    catalog = TargetCatalog(repo, TargetAccessPolicy(local_enabled=True))

    ui.configure(
        UiContext(
            get_language=lambda uid: "en",
            get_parse_mode=lambda uid: None,
            principal=lambda upd: Principal(upd.effective_user.id, admin["value"]),
            catalog=catalog,
            active_target=lambda uid: active.get(uid),
            server_limit=lambda uid: 5,
            access_mode=lambda: "whitelist",
            user_name=lambda upd: upd.effective_user.full_name,
            send=recorder.send,
            edit=recorder.edit,
            answer=recorder.answer,
            persist_language=lambda uid, code: persisted.append((uid, code)),
        )
    )
    return SimpleNamespace(
        recorder=recorder, admin=admin, active=active, persisted=persisted, catalog=catalog
    )


@pytest.mark.asyncio
async def test_main_menu_offers_self_host_to_admins(admin_env):
    await ui.main_menu(update_for(), None)
    callbacks = admin_env.recorder.flat_callbacks()
    assert "connect_self-host" in callbacks
    assert "admin_menu" in callbacks
    assert "connect_server_menu" in callbacks


@pytest.mark.asyncio
async def test_main_menu_hides_self_host_for_regular_users(admin_env):
    admin_env.admin["value"] = False
    await ui.main_menu(update_for(), None)
    callbacks = admin_env.recorder.flat_callbacks()
    assert "connect_self-host" not in callbacks
    assert "admin_menu" not in callbacks


@pytest.mark.asyncio
async def test_servers_menu_lists_self_host_and_remote(admin_env):
    await ui.servers_menu(update_for(data="connect_server_menu"), None)
    callbacks = admin_env.recorder.flat_callbacks()
    assert "connect_self-host" in callbacks
    assert "connect_web" in callbacks


@pytest.mark.asyncio
async def test_tools_menu_without_active_target_falls_back_to_servers(admin_env):
    await ui.tools_menu(update_for(data="tools_menu"), None)
    assert admin_env.recorder.answers[-1][0] == ui.translate("target_required", "en")
    assert "connect_web" in admin_env.recorder.flat_callbacks()


@pytest.mark.asyncio
async def test_tools_menu_with_active_target_shows_quick_actions(admin_env):
    admin_env.active[1] = "web"
    await ui.tools_menu(update_for(data="tools_menu"), None)
    assert "run_command_web" in admin_env.recorder.flat_callbacks()


@pytest.mark.asyncio
async def test_admin_menu_denies_non_admin_with_toast(admin_env):
    admin_env.admin["value"] = False
    await ui.admin_menu(update_for(data="admin_menu"), None)
    assert admin_env.recorder.answers[-1] == (ui.translate("admin_only", "en"), True)
    assert "backup" not in admin_env.recorder.flat_callbacks()


@pytest.mark.asyncio
async def test_admin_menu_lists_privileged_actions_for_admin(admin_env):
    await ui.admin_menu(update_for(data="admin_menu"), None)
    callbacks = admin_env.recorder.flat_callbacks()
    assert {"backup", "restore", "update_bot", "audit_menu"}.issubset(set(callbacks))


@pytest.mark.asyncio
async def test_set_language_persists_and_rerenders(admin_env):
    await ui.set_language(update_for(data="set_language_fa"), None)
    assert admin_env.persisted == [(1, "fa")]
    assert admin_env.recorder.answers[-1][0] == ui.translate("language_saved_toast", "fa")
    assert "set_language_fa" in admin_env.recorder.flat_callbacks()


@pytest.mark.asyncio
async def test_set_language_rejects_unsupported_code(admin_env):
    await ui.set_language(update_for(data="set_language_xx"), None)
    assert admin_env.persisted == []
    assert admin_env.recorder.answers[-1] == (ui.translate("language_unsupported", "en"), True)


@pytest.mark.asyncio
async def test_dashboard_panel_reopens_active_target(admin_env):
    admin_env.active[1] = "self-host"
    await ui.dashboard_panel(update_for(data="dashboard_menu"), None)
    assert "run_command_self-host" in admin_env.recorder.flat_callbacks()
    assert "self-host" not in admin_env.recorder.last_text


@pytest.mark.asyncio
async def test_settings_menu_shows_plan_and_access_mode(admin_env):
    await ui.settings_menu(update_for(data="settings_menu"), None)
    text = admin_env.recorder.last_text
    assert "whitelist" in text
    assert "language_menu" in admin_env.recorder.flat_callbacks()


@pytest.mark.asyncio
async def test_remove_server_menu_never_lists_self_host(admin_env):
    await ui.remove_server_menu(update_for(data="remove_server_menu"), None)
    callbacks = admin_env.recorder.flat_callbacks()
    assert "remove_web" in callbacks
    assert all("self-host" not in cb for cb in callbacks)
