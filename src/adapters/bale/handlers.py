"""Bale message command handlers.

``BaleAdapter`` wires incoming Bale updates to the platform service layer
(``src.services``).  It never touches SSH, the database, or the command policy
directly: every command resolves an Actor (server-side, via the store) and
calls a service that enforces ownership + permissions.

Commands: /start /help /servers /status /services /jobs /security /link
/unlink /run <alias> <command>.  Inline-keyboard presses land in
``callbacks.handle_callback``.
"""

from __future__ import annotations

import asyncio
import logging

import bale

from src.services import PlatformServices, get_services
from src.services import store
from src.services.command_service import CommandDenied
from src.services.operations_service import OperationError
from src.services.permissions import Actor, PermissionDenied

from . import authorization, callbacks, keyboards
from .utils import status_icon, truncate

logger = logging.getLogger(__name__)

NOT_LINKED = authorization.LINK_PROMPT.split("\n\n")[0]
USAGE_RUN = "Usage: `/run <alias> <command>` — e.g. `/run web1 uptime`"
USAGE_STATUS = "Usage: `/status <alias>` — send /servers to list aliases."
UNKNOWN_SERVER = "🔎 No server with that alias (or it is not yours)."


def _fmt_mem(used: object, total: object) -> str:
    try:
        return f"{float(str(used)):.0f}/{float(str(total)):.0f} MB"
    except (TypeError, ValueError):
        return "?"


class BaleAdapter:
    """One Bale bot wired to one service graph."""

    # Poll points after a SAFE command enqueue; patchable in tests.
    JOB_POLL_DELAYS: tuple[float, ...] = (0.5, 1.0)

    def __init__(self, bot, services: PlatformServices | None = None) -> None:
        self.bot = bot
        self.services = services if services is not None else get_services()
        self.links = authorization.LinkSessions()

    # -- identity --------------------------------------------------------------
    def actor_for(self, bale_user_id: str) -> Actor | None:
        return authorization.resolve_actor(bale_user_id, services=self.services)

    # -- transport ---------------------------------------------------------------
    async def send(self, chat_id, text: str, components=None):
        """Sends a message; delivery failures are logged, never raised."""
        try:
            return await self.bot.send_message(chat_id, truncate(text),
                                               components=components)
        except Exception:  # noqa: BLE001 - adapters must not die on I/O
            logger.exception("bale: send_message to %s failed", chat_id)
            return None

    async def edit(self, chat_id, message_id, text: str, components=None):
        """Edits a previously sent message; falls back to sending."""
        try:
            return await self.bot.edit_message(chat_id, message_id,
                                               truncate(text), components=components)
        except Exception:  # noqa: BLE001
            logger.exception("bale: edit_message %s/%s failed", chat_id, message_id)
            return await self.send(chat_id, text, components)

    # -- message entry ---------------------------------------------------------
    async def handle_message(self, message: bale.Message) -> None:
        user = message.from_user
        text = (message.text or "").strip()
        if user is None or not text:
            return
        bale_user_id = str(user.user_id)
        chat_id = message.chat_id
        if text.startswith("/"):
            await self._dispatch_command(user, chat_id, bale_user_id, text)
        elif self.links.active(bale_user_id):
            ok, reply = authorization.try_bind_code(
                self.services, self.links, bale_user_id,
                user.first_name or None, text)
            await self.send(chat_id, reply)
            if ok:
                actor = self.actor_for(bale_user_id)
                if actor is not None:
                    await self.send(chat_id, "Here is your main menu.",
                                    keyboards.main_menu(actor))

    async def _dispatch_command(self, user, chat_id, bale_user_id: str,
                                text: str) -> None:
        parts = text.split()
        command = parts[0].split("@", 1)[0].lower()
        args = parts[1:]

        if command == "/start":
            await self._cmd_start(user, chat_id, bale_user_id)
            return
        if command == "/help":
            await self.send(chat_id, HELP_TEXT, keyboards.help_keyboard())
            return

        actor = self.actor_for(bale_user_id)
        if actor is None:
            # Every unbound stranger is rejected everywhere (only /start and
            # /help above are reachable without a binding).
            await self.send(chat_id, NOT_LINKED, keyboards.unbound_keyboard())
            return

        handlers = {
            "/servers": lambda: self.show_servers(actor, chat_id),
            "/status": lambda: self._cmd_status(actor, chat_id, args),
            "/services": lambda: self._cmd_services(actor, chat_id, args),
            "/jobs": lambda: self.show_jobs(actor, chat_id),
            "/security": lambda: self.show_security(actor, chat_id),
            "/link": lambda: self._cmd_link(actor, chat_id),
            "/unlink": lambda: self._cmd_unlink(chat_id, bale_user_id),
            "/run": lambda: self._cmd_run(actor, chat_id, args),
        }
        handler = handlers.get(command)
        if handler is None:
            await self.send(chat_id, UNKNOWN_COMMAND)
            return
        try:
            await handler()
        except PermissionDenied:
            await self.send(chat_id, PERMISSION_DENIED)
        except OperationError as exc:
            await self.send(chat_id, f"⚠️ Operation failed: {exc}")
        except LookupError:
            await self.send(chat_id, UNKNOWN_SERVER)

    # -- commands ----------------------------------------------------------------
    async def _cmd_start(self, user, chat_id, bale_user_id: str) -> None:
        actor = self.actor_for(bale_user_id)
        if actor is None:
            prompt = authorization.start_linking(self.links, bale_user_id)
            await self.send(chat_id, prompt)
            return
        await self.send(chat_id, f"👋 Welcome back, {actor.display_name}.\n{HELP_SHORT}",
                        keyboards.main_menu(actor))

    async def _cmd_status(self, actor: Actor, chat_id, args: list[str]) -> None:
        if not args:
            await self.send(chat_id, USAGE_STATUS)
            return
        server = self._find_server(actor, args[0])
        if server is None:
            await self.send(chat_id, UNKNOWN_SERVER)
            return
        await self.show_status(actor, chat_id, None, int(server["id"]))

    async def _cmd_services(self, actor: Actor, chat_id, args: list[str]) -> None:
        if not args:
            await self.show_servers(actor, chat_id)
            return
        server = self._find_server(actor, args[0])
        if server is None:
            await self.send(chat_id, UNKNOWN_SERVER)
            return
        await self.show_services(actor, chat_id, None, int(server["id"]))

    async def _cmd_run(self, actor: Actor, chat_id, args: list[str]) -> None:
        if len(args) < 2:
            await self.send(chat_id, USAGE_RUN)
            return
        alias, command = args[0], " ".join(args[1:])
        server = self._find_server(actor, alias)
        if server is None:
            await self.send(chat_id, UNKNOWN_SERVER)
            return
        await self.run_command_flow(actor, chat_id, alias, command)

    async def _cmd_link(self, actor: Actor, chat_id) -> None:
        """Creates a link code for the caller's own account (admins may target
        other accounts via the web API)."""
        try:
            code = self.services.security.create_link_code(actor, None)
        except PermissionDenied:
            await self.send(chat_id, PERMISSION_DENIED)
            return
        await self.send(
            chat_id,
            f"🔗 Link code for *your* account: `{code}`\n"
            "Send it to this bot from the other Bale account within 5 minutes. "
            "It is single-use.")

    async def _cmd_unlink(self, chat_id, bale_user_id: str) -> None:
        removed = store.unbind_bale_user(bale_user_id)
        if removed:
            await self.send(chat_id, "🔓 This Bale account is unlinked. "
                                     "Send /start to link again.")
        else:
            await self.send(chat_id, "This Bale account was not linked.")

    # -- command execution flow (never bypasses services.commands) ---------------
    async def run_command_flow(self, actor: Actor, chat_id, alias: str,
                               command: str) -> None:
        server = self._find_server(actor, alias)
        if server is None:
            await self.send(chat_id, UNKNOWN_SERVER)
            return
        try:
            plan = await self.services.commands.request(actor, int(server["id"]),
                                                        command)
        except CommandDenied as exc:
            await self.send(chat_id, f"⛔ Command denied: {exc.reason}")
            return
        except PermissionDenied:
            await self.send(chat_id, PERMISSION_DENIED)
            return

        if plan.kind == "executed" and plan.job is not None:
            job = plan.job
            await self.send(chat_id, f"⏳ Queued job `{job.id}` on {alias}: "
                                     f"`{job.spec.command}`")
            await self._report_job(actor, chat_id, job)
        elif plan.kind == "confirm" and plan.pending is not None:
            pending = plan.pending
            await self.send(
                chat_id,
                f"⚠️ Confirmation required (risk: {plan.risk})\n"
                f"Server: {pending.alias}\nCommand: `{pending.command}`\n"
                f"Reason: {pending.reason}\n"
                "The token is single-use and expires in 3 minutes.",
                keyboards.confirm_keyboard(pending.token))
        else:
            await self.send(chat_id, f"⛔ Command blocked by policy: {plan.reason}")

    async def _report_job(self, actor: Actor, chat_id, job) -> None:
        """Polls once or twice for a queued job, then reports its state."""
        for delay in self.JOB_POLL_DELAYS:
            if job.is_terminal():
                break
            await asyncio.sleep(delay)
            fresh = self.services.jobs.get_job(actor, job.id)
            if fresh is not None:
                job = fresh
        icon = status_icon(job.state.value)
        exit_note = "" if job.exit_ok is None else f" exit={'ok' if job.exit_ok else 'failed'}"
        await self.send(chat_id,
                        f"{icon} Job `{job.id}` on {job.spec.alias}: "
                        f"state={job.state.value}{exit_note}",
                        keyboards.job_keyboard(job.id, actor))

    # -- shared renders (used by both commands and callbacks) ---------------------
    def _find_server(self, actor: Actor, alias: str) -> dict | None:
        for server in self.services.servers.list_servers(actor):
            if str(server.get("alias")) == str(alias):
                return server
        return None

    async def show_servers(self, actor: Actor, chat_id, message_id=None) -> None:
        servers = self.services.servers.list_servers(actor)
        if not servers:
            await self._reply(chat_id, message_id, "No servers registered yet.")
            return
        lines = [f"{status_icon(server.get('status'))} "
                 f"*{server.get('alias')}* (#{server['id']})" for server in servers]
        await self._reply(chat_id, message_id, "\n".join(lines),
                          keyboards.servers_keyboard(servers))

    async def show_server_menu(self, actor: Actor, chat_id, message_id,
                               server_id: int) -> None:
        server = self.services.servers.get_server(actor, server_id)
        await self._reply(
            chat_id, message_id,
            f"🖥 *{server.get('alias')}* (#{server_id})",
            keyboards.server_menu(server_id, str(server.get("alias")), actor))

    async def show_status(self, actor: Actor, chat_id, message_id,
                          server_id: int) -> None:
        server = self.services.servers.get_server(actor, server_id)
        try:
            snap = await self.services.monitor.snapshot(actor, server_id)
        except OperationError as exc:
            await self._reply(chat_id, message_id,
                              f"⚠️ Monitoring snapshot for {server.get('alias')} "
                              f"failed: {exc}")
            return
        except Exception as exc:  # SSH/auth failures surface as generic errors
            await self._reply(chat_id, message_id,
                              f"⚠️ Monitoring snapshot for {server.get('alias')} "
                              f"failed: {exc}")
            return
        stale = " (stale)" if snap.get("stale") else ""
        disks = ", ".join(f"{d.get('mount')}: {d.get('used_percent')}%"
                          for d in snap.get("disks", [])[:4]) or "-"
        await self._reply(
            chat_id, message_id,
            f"📊 *{server.get('alias')}*{stale}\n"
            f"CPU: {snap.get('cpu_percent', 0)}%\n"
            f"RAM: {snap.get('ram_percent', 0)}% "
            f"({_fmt_mem(snap.get('ram_used_mb'), snap.get('ram_total_mb'))})\n"
            f"Load: {snap.get('load', {}).get('1', 0)} "
            f"{snap.get('load', {}).get('5', 0)} {snap.get('load', {}).get('15', 0)}\n"
            f"Disks: {disks}")

    async def show_services(self, actor: Actor, chat_id, message_id,
                            server_id: int) -> None:
        server = self.services.servers.get_server(actor, server_id)
        units = await self.services.operations.list_services(actor, server_id)
        active = [u for u in units if u.get("active") == "active"]
        failed = [u for u in units if u.get("active") == "failed"]
        lines = [f"⚙️ Services on *{server.get('alias')}* "
                 f"({len(active)} active, {len(failed)} failed)"]
        lines.extend(f"{status_icon(u.get('active'))} {u.get('unit')}"
                     for u in units[:15])
        await self._reply(chat_id, message_id, "\n".join(lines),
                          keyboards.services_keyboard(server_id, units, actor))

    async def show_processes(self, actor: Actor, chat_id, message_id,
                             server_id: int) -> None:
        server = self.services.servers.get_server(actor, server_id)
        procs = await self.services.operations.list_processes(actor, server_id)
        lines = [f"🧵 Top processes on *{server.get('alias')}*"]
        lines.extend(f"`{p['pid']}` {p['user'][:12]} cpu={p['cpu']}% "
                     f"{p['command'][:60]}" for p in procs[:10])
        await self._reply(chat_id, message_id, "\n".join(lines),
                          keyboards.back_to_server_keyboard(server_id))

    async def show_docker(self, actor: Actor, chat_id, message_id,
                          server_id: int) -> None:
        server = self.services.servers.get_server(actor, server_id)
        containers = await self.services.operations.docker_containers(actor, server_id)
        lines = [f"🐳 Containers on *{server.get('alias')}*"]
        if containers:
            lines.extend(f"{status_icon(c.get('state'))} {c.get('name')} — "
                         f"{c.get('status')}" for c in containers[:15])
        else:
            lines.append("No containers found.")
        await self._reply(chat_id, message_id, "\n".join(lines),
                          keyboards.back_to_server_keyboard(server_id))

    async def show_firewall(self, actor: Actor, chat_id, message_id,
                            server_id: int) -> None:
        server = self.services.servers.get_server(actor, server_id)
        rules = await self.services.operations.firewall_rules(actor, server_id)
        await self._reply(chat_id, message_id,
                          f"🔥 Firewall on *{server.get('alias')}*:\n"
                          f"`{rules.get('raw', '')[:2500]}`",
                          keyboards.back_to_server_keyboard(server_id))

    async def show_run_prompt(self, actor: Actor, chat_id, message_id,
                              server_id: int) -> None:
        server = self.services.servers.get_server(actor, server_id)
        await self._reply(chat_id, message_id,
                          f"⌨️ Send: `/run {server.get('alias')} <command>`")

    async def show_jobs(self, actor: Actor, chat_id, message_id=None) -> None:
        jobs = self.services.jobs.list_jobs(actor)
        if not jobs:
            await self._reply(chat_id, message_id, "📦 No active jobs.")
            return
        lines = [f"{status_icon(job.state.value)} `{job.id}` "
                 f"{job.spec.alias}: {job.spec.command[:60]} "
                 f"({job.state.value})" for job in jobs[:10]]
        await self._reply(chat_id, message_id, "\n".join(lines),
                          components=keyboards.jobs_keyboard(jobs, actor))

    async def show_security(self, actor: Actor, chat_id, message_id=None) -> None:
        overview = self.services.security.overview(actor)
        admins = ", ".join(str(a) for a in overview.get("admins", [])) or "-"
        accounts = overview.get("accounts", [])
        lines = [
            "🛡 Security overview",
            f"Access mode: {overview.get('access_mode')}",
            f"Telegram admins: {admins}",
            f"Linked accounts: {len(accounts)} "
            f"(bale: {sum(1 for a in accounts if a.get('kind') == 'bale')})",
        ]
        await self._reply(chat_id, message_id, "\n".join(lines))

    # -- internal --------------------------------------------------------------
    async def _reply(self, chat_id, message_id, text: str, components=None) -> None:
        if message_id is not None:
            await self.edit(chat_id, message_id, text, components)
        else:
            await self.send(chat_id, text, components)

    async def handle_callback(self, callback: bale.CallbackQuery) -> None:
        await callbacks.handle_callback(self, callback)


PERMISSION_DENIED = "⛔ You do not have permission for this action."
UNKNOWN_COMMAND = "❓ Unknown command. Send /help for the command list."
HELP_SHORT = "Send /help for the command list."

HELP_TEXT = (
    "🤖 *Linux Admin Bot* — commands\n\n"
    "/start — link your account or show the main menu\n"
    "/servers — list servers\n"
    "/status <alias> — monitoring snapshot\n"
    "/services [alias] — systemd services\n"
    "/jobs — active jobs\n"
    "/security — security overview (admins)\n"
    "/link — create a Bale link code for your account\n"
    "/unlink — remove this Bale binding\n"
    "/run <alias> <command> — run a command (policy-checked)\n"
    "/help — this message"
)


def register(bot, services: PlatformServices | None = None) -> BaleAdapter:
    """Registers the Bale event handlers on *bot* and returns the adapter."""
    adapter = BaleAdapter(bot, services)

    @bot.event
    async def on_message(message: bale.Message) -> None:
        await adapter.handle_message(message)

    @bot.event
    async def on_callback(callback: bale.CallbackQuery) -> None:
        await adapter.handle_callback(callback)

    return adapter
