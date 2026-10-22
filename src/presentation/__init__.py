"""Presentation layer: Telegram-facing UI.

Everything that knows about ``python-telegram-bot`` lives here.  The split from
``main.py`` gives the bot a real architecture instead of one 3.8k-line handler
file: handlers talk to application use cases, never to sqlite or asyncssh.
"""
