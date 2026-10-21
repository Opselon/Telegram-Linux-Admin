"""Screen builders: one function per screen, no Telegram objects, no I/O.

Each builder receives already-authorized data (target views, principal, plan
limits) and returns a :class:`~src.presentation.views.View`.  That makes every
screen unit-testable — including its rendering in each language — without a bot
token, and keeps handlers thin enough to read.
"""

from __future__ import annotations

from typing import Sequence

from src.application.dto import TargetView
from src.domain.targets import LOCAL_ALIAS
from src.presentation.i18n import get_language_label, translate
from src.presentation.views import View


def _t(key: str, lang: str, **kwargs: str) -> str:
    return translate(key, lang, **kwargs)


def _local_name(lang: str) -> str:
    return _t("button_self_host", lang)


def main_menu(
    *,
    lang: str,
    user_name: str,
    is_admin: bool,
    self_host_available: bool,
    active_target: str | None,
    server_count: int,
) -> View:
    """Root screen: sections, quick access to the self-host target, help."""
    view = View(title=f"🐧 {_t('app_title', lang)}")
    view.add(_t("app_subtitle", lang), "", _t("main_menu_intro", lang), "")
    view.add(_t("settings_account", lang, user=user_name))
    if active_target:
        view.add(_t("settings_target", lang, target=active_target))
    view.add(_t("settings_language", lang, language_name=get_language_label(lang)))

    servers_row = [(_t("menu_servers", lang), "connect_server_menu")]
    if self_host_available:
        servers_row.append((_local_name(lang), f"connect_{LOCAL_ALIAS}"))
    buttons: list[list[tuple[str, str]]] = [servers_row]
    buttons.append(
        [
            (_t("servers_add", lang), "add_server_start"),
            (_t("servers_remove", lang), "remove_server_menu"),
        ]
    )
    buttons.append(
        [
            (_t("menu_tools", lang), "tools_menu"),
            (_t("menu_settings", lang), "settings_menu"),
        ]
    )
    if is_admin:
        buttons.append([(_t("menu_admin", lang), "admin_menu")])
    buttons.append([(_t("menu_help", lang), "help_menu")])

    if server_count == 0 and not self_host_available:
        view.add("", _t("servers_empty", lang))

    return View(title=view.title, lines=view.lines, buttons=buttons)


def servers_menu(
    *,
    lang: str,
    targets: Sequence[TargetView],
    remote_count: int,
    limit: int,
) -> View:
    """Server picker: self-host first when present, then stored servers."""
    view = View(title=f"🖥 {_t('servers_title', lang)}")
    view.add(_t("servers_pick", lang), "")
    view.add(_t("servers_summary", lang, count=str(remote_count), limit=str(limit)))
    if any(target.is_local for target in targets):
        view.add("", _t("servers_self_host_note", lang))
    if not targets:
        view.add("", _t("servers_empty", lang))

    buttons: list[list[tuple[str, str]]] = []
    for target in targets:
        if target.is_local:
            label = _local_name(lang)
        elif target.hostname:
            label = f"🖥 {target.alias} · {target.hostname}"
        else:
            label = f"🖥 {target.alias}"
        buttons.append([(label[:64], f"connect_{target.alias}")])

    buttons.append(
        [
            (_t("servers_add", lang), "add_server_start"),
            (_t("servers_remove", lang), "remove_server_menu"),
        ]
    )
    buttons.append([(_t("button_main_menu", lang), "main_menu")])
    return View(title=view.title, lines=view.lines, buttons=buttons)


def dashboard(*, lang: str, alias: str, is_local: bool) -> View:
    """Connected-target panel: the same grid for SSH and self-host."""
    shown = _local_name(lang) if is_local else alias
    kind = _t("dashboard_kind_local" if is_local else "dashboard_kind_ssh", lang)
    view = View(title=shown if is_local else f"🖥 {shown}")
    view.add(f"{_t('dashboard_title', lang)} · {kind}")
    if is_local:
        view.add("", _t("servers_self_host_line", lang))

    buttons: list[list[tuple[str, str]]] = [
        [
            (_t("button_run_command", lang), f"run_command_{alias}"),
            (_t("button_terminal", lang), f"start_shell_{alias}"),
        ],
        [
            (_t("button_status", lang), f"server_status_menu_{alias}"),
            (_t("button_system_ops", lang), f"system_commands_menu_{alias}"),
        ],
        [
            (_t("button_services", lang), f"service_management_menu_{alias}"),
            (_t("button_processes", lang), f"process_management_menu_{alias}"),
        ],
        [
            (_t("button_docker", lang), f"docker_management_menu_{alias}"),
            (_t("button_packages", lang), f"package_management_menu_{alias}"),
        ],
        [
            (_t("button_files", lang), f"file_manager_menu_{alias}"),
            (_t("button_firewall", lang), f"firewall_management_menu_{alias}"),
        ],
        [
            (_t("button_disk", lang), f"disk_usage_{alias}"),
            (_t("button_network", lang), f"network_info_{alias}"),
        ],
        [(_t("button_ports", lang), f"open_ports_{alias}")],
        [
            (_t("button_switch", lang), "connect_server_menu"),
            (_t("button_main_menu", lang), "main_menu"),
        ],
        [(_t("button_disconnect", lang), f"disconnect_{alias}")],
    ]
    return View(title=view.title, lines=view.lines, buttons=buttons)


def admin_dashboard_actions(*, lang: str, alias: str) -> View:
    """Admin-only extra row appended to a connected dashboard."""
    return View(
        title=_t("menu_admin", lang),
        buttons=[
            [
                (_t("button_backup", lang), "backup"),
                (_t("button_restore", lang), "restore"),
            ],
            [
                (_t("button_audit", lang), "audit_menu"),
                (_t("button_dashboard", lang), "dashboard_menu"),
            ],
        ],
    )


def tools_menu(*, lang: str, alias: str, is_local: bool) -> View:
    """Quick actions for the active target, without re-opening the dashboard."""
    shown = _local_name(lang) if is_local else alias
    view = View(title=f"🛠 {_t('menu_tools', lang)}")
    view.add(f"{_t('dashboard_title', lang)} · {shown}")
    buttons = [
        [
            (_t("button_run_command", lang), f"run_command_{alias}"),
            (_t("button_terminal", lang), f"start_shell_{alias}"),
        ],
        [
            (_t("button_status", lang), f"server_status_menu_{alias}"),
            (_t("button_processes", lang), f"process_management_menu_{alias}"),
        ],
        [
            (_t("button_docker", lang), f"docker_management_menu_{alias}"),
            (_t("button_files", lang), f"file_manager_menu_{alias}"),
        ],
        [(_t("button_back", lang), f"connect_{alias}")],
        [(_t("button_main_menu", lang), "main_menu")],
    ]
    return View(title=view.title, lines=view.lines, buttons=buttons)


def settings_menu(
    *, lang: str, user_name: str, active_target: str | None, access_mode: str
) -> View:
    view = View(title=f"⚙️ {_t('settings_title', lang)}")
    view.add(_t("settings_account", lang, user=user_name))
    view.add(
        _t("settings_target", lang, target=active_target or _t("settings_no_target", lang))
    )
    view.add(_t("settings_access", lang, mode=access_mode))
    view.section(_t("settings_language", lang, language_name=get_language_label(lang)))
    return View(
        title=view.title,
        lines=view.lines,
        buttons=[
            [(_t("settings_language", lang), "language_menu")],
            [(_t("button_main_menu", lang), "main_menu")],
        ],
    )


def admin_menu(*, lang: str, self_host_available: bool) -> View:
    view = View(title=f"🛡 {_t('menu_admin', lang)}")
    view.add(_t("help_security", lang))
    buttons = [
        [
            (_t("button_backup", lang), "backup"),
            (_t("button_restore", lang), "restore"),
        ],
        [
            (_t("button_update_bot", lang), "update_bot"),
            (_t("button_audit", lang), "audit_menu"),
        ],
        [(_t("button_dashboard", lang), "dashboard_menu")],
    ]
    if self_host_available:
        buttons.append([("⚡ " + _local_name(lang), f"connect_{LOCAL_ALIAS}")])
    buttons.append([(_t("button_main_menu", lang), "main_menu")])
    return View(title=view.title, lines=view.lines, buttons=buttons)


def help_menu(*, lang: str) -> View:
    view = View(title=f"❓ {_t('help_title', lang)}")
    view.add(_t("help_body", lang), "", _t("help_self_host", lang))
    view.add("", _t("help_security", lang))
    return View(
        title=view.title,
        lines=view.lines,
        buttons=[[(_t("button_main_menu", lang), "main_menu")]],
    )


def language_menu(*, lang: str, active: str, languages: Sequence[str]) -> View:
    """Language grid with the active language checked."""
    view = View(title=f"🌐 {_t('app_title', lang)}")
    view.add(_t("language_pick", lang), "", _t("language_active", lang) + f": {get_language_label(active)}")
    buttons: list[list[tuple[str, str]]] = []
    row: list[tuple[str, str]] = []
    for code in languages:
        label = get_language_label(code)
        if code == active:
            label = f"✅ {label}"
        row.append((label, f"set_language_{code}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([(_t("button_main_menu", lang), "main_menu")])
    return View(title=view.title, lines=view.lines, buttons=buttons)


def remove_server_menu(*, lang: str, targets: Sequence[TargetView]) -> View:
    view = View(title=f"➖ {_t('servers_remove', lang)}")
    view.add(_t("servers_pick", lang))
    buttons = [
        [(f"🗑 {target.alias}", f"remove_{target.alias}")]
        for target in targets
        if not target.is_local
    ]
    if not buttons:
        view.add("", _t("no_servers_to_remove", lang))
    buttons.append([(_t("button_back", lang), "connect_server_menu")])
    buttons.append([(_t("button_main_menu", lang), "main_menu")])
    return View(title=view.title, lines=view.lines, buttons=buttons)


__all__ = [
    "admin_dashboard_actions",
    "admin_menu",
    "dashboard",
    "help_menu",
    "language_menu",
    "main_menu",
    "remove_server_menu",
    "servers_menu",
    "settings_menu",
    "tools_menu",
]
