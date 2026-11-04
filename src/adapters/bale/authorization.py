"""Bale user → platform account binding and the link-code conversation.

Flow: an authenticated user (web Security Center — ``POST /api/v1/bale/link-code``
— or a bound user via the bot ``/link`` command) creates a 6-character
single-use link code through the service layer
(``SecurityService.create_link_code`` → ``store.create_link_code``).  The
unbound stranger sends ``/start`` to the bot, then types the code; the adapter
consumes it via ``store.consume_link_code`` and binds the Bale user with
``store.bind_bale_user``.  Until bound, the user is rejected everywhere.

State safety: the conversation is a per-``bale_user_id`` entry with an expiry
(in-memory, TTL 5 min, matching the code TTL) — no unkeyed global state.  The
code itself remains the only proof of authorization; the session merely says
"this user is currently expected to type a code".
"""

from __future__ import annotations

import re
import time

from src.services import PlatformServices, get_services
from src.services import store
from src.services.permissions import Actor

LINK_SESSION_TTL = 300.0  # mirror LINK_CODE_TTL in services.store

CODE_PATTERN = re.compile(r"^[A-Z0-9]{6}$")

LINK_PROMPT = (
    "🔗 *Account linking*\n\n"
    "This bot manages Linux servers and only linked accounts may use it.\n"
    "1. Sign in to the web management app (or ask an admin).\n"
    "2. Create a 6-character link code (Security Center → Bale linking, "
    "or send /link from a linked account).\n"
    "3. Type the code here.\n\n"
    "The code is single-use and valid for 5 minutes."
)

CODE_PROMPT = "🔑 Send me your 6-character link code now."
INVALID_CODE = (
    "❌ Invalid or expired link code. Codes are 6 characters, single-use, "
    "and expire after 5 minutes. Send /start to try again."
)
LINKED_OK = "✅ Linked. Send /help to see what you can do."


class LinkSessions:
    """Per-user linking conversation state, keyed by ``bale_user_id``.

    Entries expire after *ttl* seconds; :meth:`sweep` removes them lazily so
    the dict cannot grow without bound.
    """

    def __init__(self, ttl: float = LINK_SESSION_TTL) -> None:
        self._ttl = float(ttl)
        self._started: dict[str, float] = {}

    def begin(self, bale_user_id: str) -> None:
        self.sweep()
        self._started[str(bale_user_id)] = time.monotonic()

    def active(self, bale_user_id: str) -> bool:
        started = self._started.get(str(bale_user_id))
        return started is not None and (time.monotonic() - started) < self._ttl

    def clear(self, bale_user_id: str) -> None:
        self._started.pop(str(bale_user_id), None)

    def sweep(self) -> int:
        now = time.monotonic()
        expired = [k for k, t in self._started.items() if now - t >= self._ttl]
        for key in expired:
            self._started.pop(key, None)
        return len(expired)


def resolve_actor(bale_user_id: str, *,
                  services: PlatformServices | None = None) -> Actor | None:
    """The platform Actor for a bound Bale user, or ``None`` for strangers.

    The Actor is rebuilt from the store on *every* call so role changes and
    unbinds take effect immediately; callback data is never trusted.
    """
    services = services if services is not None else get_services()
    account = store.get_bale_binding(str(bale_user_id))
    if account is None:
        return None
    return store.actor_from_account(account)


def start_linking(sessions: LinkSessions, bale_user_id: str) -> str:
    """Opens the linking conversation for *bale_user_id*; returns the prompt."""
    sessions.begin(bale_user_id)
    return f"{LINK_PROMPT}\n\n{CODE_PROMPT}"


def try_bind_code(services: PlatformServices, sessions: LinkSessions,
                  bale_user_id: str, display_name: str | None,
                  text: str) -> tuple[bool, str]:
    """Consumes a link code typed by an unbound user and binds the account."""
    code = (text or "").strip().upper()
    if not CODE_PATTERN.match(code):
        return False, INVALID_CODE
    account = store.consume_link_code(code)
    if account is None:
        return False, INVALID_CODE
    store.bind_bale_user(str(bale_user_id), account.id, display_name)
    sessions.clear(bale_user_id)
    return True, LINKED_OK
