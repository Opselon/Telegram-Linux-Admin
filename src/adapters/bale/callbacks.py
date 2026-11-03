"""Callback routing — every press is re-authorized server-side.

Rules enforced here for every single callback:

1. The Actor is re-resolved from the store (``authorization.resolve_actor``) —
   the button press itself carries no authorization.
2. The specific permission is checked (either explicitly via
   ``Actor.require`` or implicitly inside the service call).
3. Unknown routes and expired one-shot tokens are rejected with a clear
   message.

Callback routes (kept short; ``<sid>`` = server id)::

    srv:list | srv:view:<sid> | srv:status:<sid> | srv:services:<sid>
    srv:procs:<sid> | srv:docker:<sid> | srv:firewall:<sid> | srv:run:<sid>
    svc:<action>:<sid>:<name>          action ∈ start|stop|restart
    cmd:confirm:<token> | cmd:cancel:<token>
    jobs:list | jobs:cancel:<job_id>
    sec:view | cb:noop
"""

from __future__ import annotations

import logging

import bale

from src.services.command_service import ConfirmationError
from src.services.operations_service import OperationError
from src.services.permissions import PermissionDenied

logger = logging.getLogger(__name__)

DENIED = "⛔ You do not have permission for this action."
NOT_AUTHORIZED = (
    "⛔ Not authorized: this Bale account is not linked to the platform.\n"
    "Send /start to link it."
)
NOT_FOUND = "🔎 Not found (or not yours)."
UNKNOWN_ROUTE = "❓ Unknown or expired action."
EXPIRED_TOKEN = "⏳ This confirmation expired or was already used."

_SERVICE_ACTIONS = {"start", "stop", "restart"}


async def handle_callback(adapter, callback: bale.CallbackQuery) -> None:
    """Entry point for every inline-keyboard press."""
    user = callback.from_user
    if user is None:
        return
    data = (callback.data or "").strip()
    chat_id = callback.message.chat_id if callback.message else user.chat_id
    message_id = callback.message.message_id if callback.message else None
    actor = adapter.actor_for(str(user.user_id))
    if actor is None:
        await adapter.send(chat_id, NOT_AUTHORIZED)
        return
    try:
        await _route(adapter, actor, chat_id, message_id, data)
    except PermissionDenied:
        await _reply(adapter, chat_id, message_id, DENIED)
    except LookupError:
        await _reply(adapter, chat_id, message_id, NOT_FOUND)
    except ConfirmationError:
        await _reply(adapter, chat_id, message_id, EXPIRED_TOKEN)
    except OperationError as exc:
        await _reply(adapter, chat_id, message_id, f"⚠️ Operation failed: {exc}")
    except ValueError as exc:
        await _reply(adapter, chat_id, message_id, f"⚠️ {exc}")


async def _reply(adapter, chat_id, message_id: str | None, text: str,
                 components=None) -> None:
    """Edits the pressed message when possible, otherwise sends a new one."""
    if message_id is not None:
        await adapter.edit(chat_id, message_id, text, components)
    else:
        await adapter.send(chat_id, text, components)


async def _route(adapter, actor, chat_id, message_id, data: str) -> None:
    parts = data.split(":")
    route = parts[0]
    if route == "srv":
        await _route_server(adapter, actor, chat_id, message_id, parts)
    elif route == "svc":
        await _route_service_action(adapter, actor, chat_id, message_id, parts)
    elif route == "cmd":
        await _route_command(adapter, actor, chat_id, message_id, parts)
    elif route == "jobs":
        await _route_jobs(adapter, actor, chat_id, message_id, parts)
    elif route == "sec" and len(parts) == 2 and parts[1] == "view":
        await adapter.show_security(actor, chat_id, message_id)
    elif route == "cb" and data == "cb:noop":
        return
    else:
        await _reply(adapter, chat_id, message_id, UNKNOWN_ROUTE)


async def _route_server(adapter, actor, chat_id, message_id, parts) -> None:
    action = parts[1] if len(parts) > 1 else ""
    if action == "list":
        await adapter.show_servers(actor, chat_id, message_id)
        return
    if len(parts) < 3:
        await _reply(adapter, chat_id, message_id, UNKNOWN_ROUTE)
        return
    server_id = int(parts[2])
    if action == "view":
        await adapter.show_server_menu(actor, chat_id, message_id, server_id)
    elif action == "status":
        await adapter.show_status(actor, chat_id, message_id, server_id)
    elif action == "services":
        await adapter.show_services(actor, chat_id, message_id, server_id)
    elif action == "procs":
        await adapter.show_processes(actor, chat_id, message_id, server_id)
    elif action == "docker":
        await adapter.show_docker(actor, chat_id, message_id, server_id)
    elif action == "firewall":
        await adapter.show_firewall(actor, chat_id, message_id, server_id)
    elif action == "run":
        await adapter.show_run_prompt(actor, chat_id, message_id, server_id)
    else:
        await _reply(adapter, chat_id, message_id, UNKNOWN_ROUTE)


async def _route_service_action(adapter, actor, chat_id, message_id, parts) -> None:
    # svc:<action>:<server_id>:<name> — the service layer re-checks
    # services.control and re-validates the unit name; this parse is routing only.
    if len(parts) < 4 or parts[1] not in _SERVICE_ACTIONS:
        await _reply(adapter, chat_id, message_id, UNKNOWN_ROUTE)
        return
    action, server_id, name = parts[1], int(parts[2]), parts[3]
    result = await adapter.services.operations.service_action(
        actor, server_id, name, action)
    unit = result.get("unit", name)
    await _reply(adapter, chat_id, message_id,
                 f"✅ {action} sent for *{unit}*. Reopen Services to refresh.")


async def _route_command(adapter, actor, chat_id, message_id, parts) -> None:
    # cmd:confirm:<token> — the token is single-use and actor-bound; the
    # service layer verifies pending.account_id == actor.account_id.
    if len(parts) != 3 or parts[1] not in {"confirm", "cancel"}:
        await _reply(adapter, chat_id, message_id, UNKNOWN_ROUTE)
        return
    verb, token = parts[1], parts[2]
    commands = adapter.services.commands
    if verb == "confirm":
        if commands.peek_confirmation(actor, token) is None:
            raise ConfirmationError("expired or replayed token")
        job = await commands.confirm(actor, token)
        await _reply(adapter, chat_id, message_id,
                     f"✅ Confirmed. Job `{job.id}` queued "
                     f"({job.spec.alias}: {job.spec.command}).")
    else:
        cancelled = commands.cancel_confirmation(actor, token)
        await _reply(adapter, chat_id, message_id,
                     "❎ Cancelled." if cancelled else EXPIRED_TOKEN)


async def _route_jobs(adapter, actor, chat_id, message_id, parts) -> None:
    if len(parts) == 2 and parts[1] == "list":
        await adapter.show_jobs(actor, chat_id, message_id)
        return
    if len(parts) == 3 and parts[1] == "cancel":
        job_id = parts[2]
        cancelled = await adapter.services.jobs.cancel_job(actor, job_id)
        await _reply(adapter, chat_id, message_id,
                     f"🛑 Job `{job_id}` cancelled." if cancelled
                     else f"⚠️ Job `{job_id}` could not be cancelled.")
        return
    await _reply(adapter, chat_id, message_id, UNKNOWN_ROUTE)
