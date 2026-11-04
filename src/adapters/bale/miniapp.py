"""Bale mini-app launch: the URL-button fallback.

**Research conclusion** (python-bale-bot 2.5.0, installed source, verified by
grepping the whole package): the SDK has **no native mini-app / WebApp launch
support and no init-data signing contract**.  ``bale/ui/inline_keyboard_button.py``
defines exactly five fields — ``text``, ``callback_data``, ``url``,
``switch_inline_query``, ``switch_inline_query_current_chat`` — of which
``url`` is the standard "open this URL" button.  There is no ``web_app`` field
anywhere (zero matches for ``web_app|webapp|miniapp|mini_app|init_data``).

The shipped path is therefore a plain **URL button** pointing at the
responsive web app (opened by Bale's in-app browser).  See
``docs/upgrade/bale-miniapp-notes.md`` for the full evidence trail.
"""

from __future__ import annotations

import os

import bale

PUBLIC_URL_ENV = "TLA_PUBLIC_URL"
DEFAULT_PUBLIC_URL = "http://localhost:8080"
OPEN_APP_TEXT = "📱 Open Management App"


def public_url() -> str:
    """Base URL of the web app (``TLA_PUBLIC_URL``, default loopback:8080)."""
    return (os.environ.get(PUBLIC_URL_ENV) or "").strip() or DEFAULT_PUBLIC_URL


def open_app_button(text: str = OPEN_APP_TEXT) -> bale.InlineKeyboardButton:
    """A URL button that opens the web app in Bale's in-app browser."""
    return bale.InlineKeyboardButton(text, url=public_url())


def append_open_app(markup: bale.InlineKeyboardMarkup,
                    text: str = OPEN_APP_TEXT) -> bale.InlineKeyboardMarkup:
    """Adds the URL button as the last row of *markup* (rows are 1-based)."""
    existing = list(getattr(markup, "keyboards", []) or [])
    next_row = max((item.row for item in existing), default=0) + 1
    markup.add(open_app_button(text), row=next_row)
    return markup
