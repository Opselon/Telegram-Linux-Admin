"""Adds the 4.0 UI keys (English source of truth) to the JSON catalog."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "src" / "presentation" / "i18n" / "catalog" / "en.json"

NEW_KEYS: dict[str, str] = {
    "app_title": "Linux Admin Console",
    "app_subtitle": "Secure remote and local server management",
    "main_menu_intro": "Pick a section. Everything you open stays scoped to you.",
    "menu_servers": "🖥 Servers",
    "menu_tools": "🛠 Tools",
    "menu_settings": "⚙️ Settings",
    "menu_admin": "🛡 Administration",
    "menu_help": "❓ Help",
    "button_self_host": "⚡ This server",
    "badge_self_host": "SELF-HOST",
    "button_back": "⬅️ Back",
    "button_main_menu": "🏠 Main menu",
    "button_refresh": "🔄 Refresh",
    "button_close": "✖️ Close",
    "servers_title": "Your servers",
    "servers_summary": "{count} remote servers — plan limit {limit}",
    "servers_empty": "No servers yet. Add one to run commands over SSH.",
    "servers_self_host_note": "You are the administrator: the host running this bot can be managed directly.",
    "servers_self_host_line": "This bot's own host — always available to the administrator.",
    "servers_add": "➕ Add server",
    "servers_remove": "➖ Remove server",
    "servers_pick": "Select a server to connect to.",
    "dashboard_title": "Connected",
    "dashboard_kind_ssh": "SSH",
    "dashboard_kind_local": "local host",
    "dashboard_group_command": "Command",
    "dashboard_group_system": "System",
    "dashboard_group_storage": "Files and packages",
    "dashboard_group_network": "Network",
    "dashboard_group_admin": "Administration",
    "button_run_command": "▶️ Run command",
    "button_terminal": "⌨️ Terminal",
    "button_status": "📊 Status",
    "button_system_ops": "🧭 System ops",
    "button_services": "🔧 Services",
    "button_processes": "📈 Processes",
    "button_docker": "🐳 Docker",
    "button_packages": "📦 Packages",
    "button_files": "📁 Files",
    "button_firewall": "🔥 Firewall",
    "button_disk": "💾 Disk usage",
    "button_network": "🌐 Network info",
    "button_ports": "🔌 Open ports",
    "button_backup": "💾 Backup",
    "button_restore": "♻️ Restore",
    "button_update_bot": "⬆️ Update bot",
    "button_audit": "🔐 Audit log",
    "button_dashboard": "📊 Dashboard",
    "button_switch": "🖥 Servers",
    "settings_title": "Settings",
    "settings_language": "🌐 Language: {language}",
    "settings_target": "🎯 Active target: {target}",
    "settings_no_target": "none selected",
    "settings_access": "🔐 Access mode: {mode}",
    "settings_account": "👤 Signed in as {user}",
    "help_title": "How this bot works",
    "help_body": "Use Servers to add a machine once, then pick it to open the control panel. Commands run over SSH with host-key pinning; the administrator can also manage the host that runs the bot.",
    "help_self_host": "This server is the host running the bot. Nobody else can see or use it.",
    "help_security": "Every action is scoped to your Telegram account and written to the audit log.",
    "self_host_connected": "Connected to this server. The bot's own host is now manageable.",
    "self_host_disabled": "Self-host management is disabled for this deployment.",
    "alias_reserved": "That alias is reserved for this server. Choose another name.",
    "invalid_request": "Invalid request data.",
    "menu_outdated": "This menu is outdated — open it again.",
    "target_required": "Open a server first.",
    "action_done": "Done.",
    "language_pick": "Choose the language this bot speaks to you in.",
    "language_active": "Active",
    "confirm_remove_title": "Remove {alias}?",
    "confirm_remove_body": "The saved credentials for this server are deleted. This cannot be undone.",
    "button_confirm_remove": "🗑 Yes, remove",
    "button_cancel": "✖️ Cancel",
    "quick_actions": "Quick actions",
}


def main() -> None:
    data = json.loads(CATALOG.read_text(encoding="utf-8"))
    added = [key for key in NEW_KEYS if key not in data]
    data.update(NEW_KEYS)
    CATALOG.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"en.json now has {len(data)} keys ({len(added)} added)")


if __name__ == "__main__":
    main()
