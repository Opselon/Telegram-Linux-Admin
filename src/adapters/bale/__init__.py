"""Bale adapter — implemented against the verified python-bale-bot SDK contract.

Modules:
- ``client`` — bot factory (``BALE_BOT_TOKEN``).
- ``lifecycle`` — ``run_bale(stop)`` polling task (called by ``src.runtime_app``).
- ``authorization`` — Bale user → account binding + link-code conversation.
- ``handlers`` / ``callbacks`` / ``keyboards`` — commands and inline menus.
- ``notifications`` — alert fan-out with per-account category opt-outs.
- ``miniapp`` — URL-button fallback for the web management app.

All behavior goes through ``src.services``; nothing here touches SSH, the
database, or command policy directly (one documented exception: the reverse
bale-binding lookup in ``notifications`` — see docs/upgrade/bale-miniapp-notes.md).
"""

from .lifecycle import run_bale

__all__: list[str] = ["run_bale"]
