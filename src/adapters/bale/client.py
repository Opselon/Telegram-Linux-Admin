"""Bale bot factory.

The only place the adapter touches ``bale.Bot`` construction.  Verified SDK
contract (python-bale-bot 2.5.0, installed source):

- ``bale.Bot(token)`` — ``bale/bot.py`` L70; raises ``InvalidToken`` only for a
  non-string token; server-side validity is checked by ``await bot.get_me()``
  (``bale/bot.py`` L323), which raises ``bale.InvalidToken`` for a bad token.
- ``bot.updater.interval`` — ``bale/updater.py`` ``__slots__``; when set, the
  polling loop sleeps between ``getUpdates`` calls.  Left ``None`` the SDK
  spins in a hot loop, so the factory always sets it.
"""

from __future__ import annotations

import logging
import os

import bale

logger = logging.getLogger(__name__)

TOKEN_ENV = "BALE_BOT_TOKEN"
POLL_INTERVAL_ENV = "TLA_BALE_POLL_INTERVAL"
DEFAULT_POLL_INTERVAL = 1.0
MIN_POLL_INTERVAL = 0.2


def bale_token_from_env() -> str:
    """The configured Bale bot token (``BALE_BOT_TOKEN``), or ``""``."""
    return (os.environ.get(TOKEN_ENV) or "").strip()


def poll_interval_from_env() -> float:
    """Poll interval in seconds (``TLA_BALE_POLL_INTERVAL``, default 1.0)."""
    raw = os.environ.get(POLL_INTERVAL_ENV, "").strip()
    try:
        value = float(raw) if raw else DEFAULT_POLL_INTERVAL
    except ValueError:
        logger.warning("invalid %s=%r; using %.1fs", POLL_INTERVAL_ENV, raw,
                       DEFAULT_POLL_INTERVAL)
        value = DEFAULT_POLL_INTERVAL
    return max(MIN_POLL_INTERVAL, value)


def create_bot(token: str) -> bale.Bot:
    """Builds a ``bale.Bot`` with a sane polling interval."""
    if not token:
        raise ValueError("Bale bot token must be a non-empty string")
    bot = bale.Bot(token)
    bot.updater.interval = poll_interval_from_env()
    return bot
