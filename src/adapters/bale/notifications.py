"""Bale fan-out for :class:`~services.notification_service.NotificationService`.

The service emits alerts with a ``recipients`` list of account ids; this
module maps each account to its bound Bale users, applies the per-account
category opt-out (``NotificationService.recipient_filter`` →
``store.get_disabled_categories``) and sends one message per Bale user.
Per-send errors are logged and swallowed — a broken listener must never break
the emitter (the service also guards its own loop).

Note: the service layer exposes ``store.get_bale_binding``/``bind``/
``unbind`` but no reverse lookup; until one is added upstream this module
queries the ``bale_bindings`` table through ``src.database`` directly (same
table the store owns).  Recorded in docs/upgrade/bale-miniapp-notes.md.
"""

from __future__ import annotations

import logging

from src import database
from src.services import PlatformServices, get_services

from .utils import truncate

logger = logging.getLogger(__name__)

_SEVERITY_ICON = {"critical": "🚨", "warning": "⚠️", "info": "ℹ️"}

_NON_DETAIL_KEYS = frozenset({"kind", "scope", "severity", "at", "recipients"})


def render_alert(kind: str, payload: dict) -> str:
    """One-line-per-detail alert text for a Bale message."""
    icon = _SEVERITY_ICON.get(str(payload.get("severity")), "🔔")
    scope = str(payload.get("scope") or "-")
    details = ", ".join(
        f"{key}={value}" for key, value in sorted(payload.items())
        if key not in _NON_DETAIL_KEYS)
    head = f"{icon} Alert [{kind}] on {scope}"
    return f"{head}\n{details}" if details else head


def bale_user_ids_for_account(account_id: int) -> list[str]:
    """Bale user ids bound to *account_id* (reverse of ``get_bale_binding``)."""
    conn = database.get_db_connection()
    rows = conn.execute(
        "SELECT bale_user_id FROM bale_bindings WHERE account_id = ?",
        (int(account_id),),
    ).fetchall()
    return [str(row["bale_user_id"]) for row in rows]


async def deliver_alerts(kind: str, payload: dict, *, bot,
                         services: PlatformServices) -> None:
    """Sends one alert to every eligible Bale user; never raises."""
    text = render_alert(kind, payload)
    recipients = payload.get("recipients") or []
    for account_id in recipients:
        try:
            account_id = int(account_id)
        except (TypeError, ValueError):
            continue
        if not services.notifications.recipient_filter(account_id, kind):
            continue  # account opted out of this category
        for bale_user_id in bale_user_ids_for_account(account_id):
            try:
                await bot.send_message(bale_user_id, truncate(text))
            except Exception:  # noqa: BLE001 - one bad send must not stop the rest
                logger.exception("bale: alert delivery to %s failed",
                                 bale_user_id)


def wire_notifications(bot,
                       services: PlatformServices | None = None):
    """Subscribes the Bale sender to the notification service.

    Returns an ``unsubscribe`` callable (the service keeps a plain list).
    """
    svc = services if services is not None else get_services()

    async def _subscriber(kind: str, payload: dict) -> None:
        await deliver_alerts(kind, payload, bot=bot, services=svc)

    svc.notifications.subscribe(_subscriber)

    def unsubscribe() -> None:
        try:
            svc.notifications._subscribers.remove(_subscriber)  # noqa: SLF001
        except ValueError:
            pass

    return unsubscribe
