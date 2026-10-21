"""Screen tests: layout, self-host labelling, and admin-only rows.

Screens are pure data, so these assertions cover the whole menu surface in
every shipped language without a bot, a network, or a database.
"""

from __future__ import annotations

import pytest

from src.application.dto import TargetView
from src.parse_mode import PARSE_MODE_HTML, PARSE_MODE_MARKDOWN_V2, get_parse_mode
from src.presentation import screens
from src.presentation.i18n import SUPPORTED_LANGUAGES, translate
from src.presentation.views import View, render

LOCAL = TargetView(alias="self-host", kind="local", is_local=True, hostname="127.0.0.1")
REMOTE = TargetView(alias="web", kind="ssh", is_local=False, hostname="10.0.0.5")


def callbacks(view: View) -> list[str]:
    return [payload for row in view.buttons for _, payload in row]


def labels(view: View) -> list[str]:
    return [label for row in view.buttons for label, _ in row]


def test_main_menu_shows_self_host_for_admins():
    view = screens.main_menu(
        lang="en", user_name="Cap", is_admin=True, self_host_available=True,
        active_target=None, server_count=1,
    )
    assert "connect_self-host" in callbacks(view)
    assert "admin_menu" in callbacks(view)
    assert "help_menu" in callbacks(view)


def test_main_menu_hides_self_host_and_admin_for_regular_users():
    view = screens.main_menu(
        lang="en", user_name="guest", is_admin=False, self_host_available=False,
        active_target=None, server_count=0,
    )
    assert "connect_self-host" not in callbacks(view)
    assert "admin_menu" not in callbacks(view)
    assert translate("servers_empty", "en") in " ".join(view.lines)


def test_main_menu_reports_active_target_and_language():
    view = screens.main_menu(
        lang="fa", user_name="Cap", is_admin=True, self_host_available=True,
        active_target="⚡ این سرور", server_count=2,
    )
    body = " ".join(view.lines)
    assert "Cap" in body
    assert "این سرور" in body


def test_servers_menu_lists_self_host_first_without_leaking_alias():
    view = screens.servers_menu(lang="en", targets=[LOCAL, REMOTE], remote_count=1, limit=3)
    first_label, first_payload = view.buttons[0][0]
    assert first_payload == "connect_self-host"
    assert "self-host" not in first_label.lower()
    assert "⚡" in first_label
    assert "10.0.0.5" in view.buttons[1][0][0]


def test_servers_menu_empty_state_and_navigation():
    view = screens.servers_menu(lang="de", targets=[], remote_count=0, limit=2)
    assert "main_menu" in callbacks(view)
    assert translate("servers_empty", "de") in " ".join(view.lines)


def test_dashboard_is_identical_for_both_kinds_but_labelled():
    local = screens.dashboard(lang="en", alias="self-host", is_local=True)
    remote = screens.dashboard(lang="en", alias="web", is_local=False)

    # Same actions, same order — only the alias inside the payloads differs.
    assert [cb.replace("self-host", "web") for cb in callbacks(local)] == callbacks(remote)
    assert translate("dashboard_kind_local", "en") in " ".join(local.lines)
    assert translate("dashboard_kind_ssh", "en") in " ".join(remote.lines)
    assert "self-host" not in local.title


def test_dashboard_has_every_operational_action():
    view = screens.dashboard(lang="en", alias="web", is_local=False)
    expected = {
        "run_command_web",
        "start_shell_web",
        "server_status_menu_web",
        "system_commands_menu_web",
        "service_management_menu_web",
        "process_management_menu_web",
        "docker_management_menu_web",
        "package_management_menu_web",
        "file_manager_menu_web",
        "firewall_management_menu_web",
        "disk_usage_web",
        "network_info_web",
        "open_ports_web",
        "disconnect_web",
        "main_menu",
    }
    assert expected.issubset(set(callbacks(view)))


def test_tools_menu_stays_inside_the_active_target():
    view = screens.tools_menu(lang="en", alias="web", is_local=False)
    assert "run_command_web" in callbacks(view)
    assert "main_menu" in callbacks(view)


def test_admin_menu_exposes_privileged_actions():
    view = screens.admin_menu(lang="en", self_host_available=True)
    assert {"backup", "restore", "update_bot", "audit_menu", "connect_self-host"}.issubset(
        set(callbacks(view))
    )


def test_language_menu_marks_active_and_offers_every_language():
    view = screens.language_menu(lang="fa", active="fa", languages=SUPPORTED_LANGUAGES)
    payloads = callbacks(view)
    for language in SUPPORTED_LANGUAGES:
        assert f"set_language_{language}" in payloads
    active_label = next(
        label for row in view.buttons for label, payload in row if payload == "set_language_fa"
    )
    assert active_label.startswith("✅")


def test_remove_menu_never_offers_self_host():
    view = screens.remove_server_menu(lang="en", targets=[LOCAL, REMOTE])
    assert callbacks(view).count("remove_self-host") == 0
    assert "remove_web" in callbacks(view)


def test_remove_menu_empty_state():
    view = screens.remove_server_menu(lang="en", targets=[])
    assert translate("no_servers_to_remove", "en") in " ".join(view.lines)


@pytest.mark.parametrize("language", SUPPORTED_LANGUAGES)
def test_every_screen_renders_without_raw_keys_in_every_language(language):
    parse_mode = get_parse_mode(language)
    views = [
        screens.main_menu(
            lang=language, user_name="Cap", is_admin=True, self_host_available=True,
            active_target=None, server_count=1,
        ),
        screens.servers_menu(lang=language, targets=[LOCAL, REMOTE], remote_count=1, limit=5),
        screens.dashboard(lang=language, alias="self-host", is_local=True),
        screens.tools_menu(lang=language, alias="web", is_local=False),
        screens.settings_menu(
            lang=language, user_name="Cap", active_target="web", access_mode="whitelist"
        ),
        screens.admin_menu(lang=language, self_host_available=True),
        screens.help_menu(lang=language),
        screens.language_menu(lang=language, active=language, languages=SUPPORTED_LANGUAGES),
        screens.remove_server_menu(lang=language, targets=[REMOTE]),
    ]
    for view in views:
        text = render(view, parse_mode)
        assert text.strip(), f"{language}: empty render for {view.title}"
        for row in view.buttons:
            for label, payload in row:
                assert label.strip(), f"{language}: empty button label"
                assert payload.strip()
        # Raw catalog keys must never reach the user.
        for token in ("servers_title", "button_self_host", "dashboard_title"):
            assert token not in text


def test_render_escapes_both_parse_modes():
    view = View(title="A.b", lines=["x-y (z)"], buttons=[])
    markdown = render(view, PARSE_MODE_MARKDOWN_V2)
    html = render(view, PARSE_MODE_HTML)
    assert "\\." in markdown and "\\-" in markdown
    assert "<b>A.b</b>" in html
