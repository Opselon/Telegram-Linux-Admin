"""Inline keyboards for the Bale adapter.

Callback data is short and structured (``svc:restart:<server_id>:<name>``) and
**never** carries authorization — every press re-resolves the Actor server-side
and re-checks the specific permission (see ``callbacks.py``).  Button labels
are truncated to keep rows narrow; rows are 1-based (SDK requirement in
``bale/ui/reply_markup_item.py``).
"""

from __future__ import annotations

import bale

from src.services.permissions import Actor

from .miniapp import open_app_button
from .utils import short


def _btn(text: str, callback_data: str) -> bale.InlineKeyboardButton:
    return bale.InlineKeyboardButton(short(text, 28), callback_data=callback_data)


def _build(rows: list[list[bale.InlineKeyboardButton]]) -> bale.InlineKeyboardMarkup:
    markup = bale.InlineKeyboardMarkup()
    for index, row in enumerate(rows, start=1):
        for button in row:
            markup.add(button, row=index)
    return markup


def unbound_keyboard() -> bale.InlineKeyboardMarkup:
    return _build([[open_app_button("🌐 Open Web App to get a code")]])


def main_menu(actor: Actor) -> bale.InlineKeyboardMarkup:
    rows = [[_btn("🖥 Servers", "srv:list")]]
    actions: list[bale.InlineKeyboardButton] = []
    if actor.has("jobs.read"):
        actions.append(_btn("📦 Jobs", "jobs:list"))
    if actor.has("security.read"):
        actions.append(_btn("🛡 Security", "sec:view"))
    if actions:
        rows.append(actions)
    rows.append([open_app_button()])
    return _build(rows)


def help_keyboard() -> bale.InlineKeyboardMarkup:
    return _build([[open_app_button()]])


def servers_keyboard(servers: list[dict]) -> bale.InlineKeyboardMarkup:
    rows = [[_btn(f"🖥 {server.get('alias')}", f"srv:view:{server['id']}")]
            for server in servers[:20]]
    return _build(rows)


def server_menu(server_id: int, alias: str, actor: Actor) -> bale.InlineKeyboardMarkup:
    sid = str(server_id)
    rows = [
        [_btn("📊 Status", f"srv:status:{sid}"),
         _btn("⚙️ Services", f"srv:services:{sid}")],
        [_btn("🧵 Processes", f"srv:procs:{sid}"),
         _btn("🐳 Docker", f"srv:docker:{sid}")],
        [_btn("🔥 Firewall", f"srv:firewall:{sid}")],
    ]
    if actor.has("commands.execute"):
        rows.append([_btn("⌨️ Run command", f"srv:run:{sid}"),
                     _btn("🔄 Refresh", f"srv:view:{sid}")])
    rows.append([_btn("⬅️ Servers", "srv:list")])
    return _build(rows)


def services_keyboard(server_id: int, services: list[dict],
                      actor: Actor) -> bale.InlineKeyboardMarkup | None:
    """One control row per unit — or ``None`` when the actor cannot control."""
    if not actor.has("services.control"):
        return None
    sid = str(server_id)
    rows = []
    for unit in services[:12]:
        name = str(unit.get("unit") or "")
        rows.append([
            _btn(name, "cb:noop"),
            _btn("▶", f"svc:start:{sid}:{name}"),
            _btn("⏹", f"svc:stop:{sid}:{name}"),
            _btn("↻", f"svc:restart:{sid}:{name}"),
        ])
    rows.append([_btn("⬅️ Server", f"srv:view:{sid}")])
    return _build(rows)


def confirm_keyboard(token: str) -> bale.InlineKeyboardMarkup:
    """CONFIRM band command: one-shot, actor-bound token inside callback data."""
    return _build([
        [_btn("✅ Confirm", f"cmd:confirm:{token}")],
        [_btn("❌ Cancel", f"cmd:cancel:{token}")],
    ])


def job_keyboard(job_id: str, actor: Actor) -> bale.InlineKeyboardMarkup | None:
    if not actor.has("jobs.cancel"):
        return None
    return _build([[_btn("🛑 Cancel job", f"jobs:cancel:{job_id}")]])


def jobs_keyboard(jobs: list, actor: Actor) -> bale.InlineKeyboardMarkup | None:
    """One cancel row per queued/running job (None when not permitted)."""
    if not actor.has("jobs.cancel") or not jobs:
        return None
    return _build([[_btn(f"🛑 Cancel {job.id[-6:]}", f"jobs:cancel:{job.id}")]
                   for job in jobs[:10]])


def back_to_server_keyboard(server_id: int) -> bale.InlineKeyboardMarkup:
    return _build([[_btn("⬅️ Server", f"srv:view:{server_id}")],
                   [open_app_button()]])
