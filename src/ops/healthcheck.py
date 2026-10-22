"""Deep readiness probe beyond the HTTP liveness check (ops lane).

PURPOSE
    :func:`deep_readiness` answers one operator question — "can this bot
    actually do its job right now?" — with a single boolean plus a list of
    human explanations covering every check that failed.  Deliberately
    MORE than the ``/readyz`` HTTP probe: this also verifies the audit
    directory is writable and the token is shaped like a Telegram token
    (format only — it is never sent anywhere from here; contract hard
    law 5).  An outbound Telegram API HEAD probe is strictly opt-in: the
    flag exists so an operator can dial it, but the default stays off and
    no network I/O happens in tests.

OWNER
    Lane ops (feat/3.3-ops).  Wire nothing to the HTTP server from here —
    the orchestrator owns that call site.

CONSUMES
    src.database (connectivity via ``SELECT 1``), src.audit (directory
    writability via :meth:`AuditLogger.is_writable`), src.config
    (token shape via :func:`validate_token` on a copy of the token string
    — hardened only against whitespace, never persisted).

PROVIDES
    :class:`Check` and :class:`DeepReadiness` — structured, JSON-safe
    result objects; :func:`deep_readiness` — the probe itself.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from ..audit import audit
from ..config import validate_token

logger = logging.getLogger(__name__)

#: Where the bot reaches Telegram. Only touched when the operator opts in.
TELEGRAM_API_URL = "https://api.telegram.org"


@dataclass
class Check:
    """One counted readiness check, JSON-safe."""

    name: str
    ok: bool
    detail: str


@dataclass
class DeepReadiness:
    """Aggregated deep-readiness result: single boolean + explanations."""

    ok: bool
    checks: list[Check] = field(default_factory=list)
    explanations: list[str] = field(default_factory=list)


def _check_db() -> Check:
    try:
        from ..database import get_db_connection

        conn = get_db_connection()
        conn.execute("SELECT 1").fetchone()
        return Check(name="database", ok=True, detail="SELECT 1 ok")
    except sqlite3.Error as exc:
        return Check(name="database", ok=False, detail=f"sqlite error: {exc}")
    except Exception as exc:  # degrade with a readable message
        logger.exception("database readiness probe degraded")
        return Check(name="database", ok=False, detail=f"unavailable: {exc}")


def _check_audit_dir(audit_logger: Any | None = None) -> Check:
    target = audit_logger if audit_logger is not None else audit
    writable = bool(target.is_writable())
    if writable:
        return Check(name="audit_dir", ok=True, detail="appendable")
    return Check(
        name="audit_dir",
        ok=False,
        detail="audit directory not writable on this host",
    )


def _check_token_shape(config: Any | None) -> Check:
    token = getattr(config, "telegram_token", "") if config is not None else ""
    if not token:
        return Check(
            name="token", ok=False, detail="no telegram token configured"
        )
    try:
        validate_token(token)
    except ValueError as exc:
        detail = f"token shape invalid: {exc}"
        detail = detail.replace(token, "<token>")  # never echo secret material
        return Check(name="token", ok=False, detail=detail)
    return Check(name="token", ok=True, detail="shape valid")


async def _check_telegram_probe(
    token: str,
    *,
    enabled: bool,
    timeout: float,
) -> Check:
    """Optional outbound HEAD to api.telegram.org. OFF by default."""
    if not enabled:
        return Check(
            name="telegram_probe",
            ok=True,
            detail="skipped (probe disabled by default)",
        )
    if not token:
        return Check(
            name="telegram_probe", ok=False, detail="no token to probe with"
        )
    try:
        import urllib.error
        import urllib.request

        req = urllib.request.Request(
            TELEGRAM_API_URL, method="HEAD", headers={"User-Agent": "tla-healthcheck"}
        )
        await asyncio.get_running_loop().run_in_executor(
            None, lambda: urllib.request.urlopen(req, timeout=timeout).close()
        )
        return Check(name="telegram_probe", ok=True, detail="api reachable")
    except urllib.error.URLError as exc:
        return Check(name="telegram_probe", ok=False, detail=f"unreachable: {exc}")


async def deep_readiness(
    config: Any | None = None,
    *,
    audit_logger: Any | None = None,
    telegram_probe: bool = False,
    timeout: float = 5.0,
) -> DeepReadiness:
    """Returns the structured deep-readiness result for this process.

    Checks database connectivity, audit dir writability, token shape and
    (only when explicitly turned on) an outbound Telegram API HEAD.  The
    single ``ok`` flips False the moment any single check fails and the
    failing check contributes a human explanation each.
    """
    checks: list[Check] = []
    checks.append(_check_db())
    checks.append(_check_audit_dir(audit_logger))
    token = getattr(config, "telegram_token", "") if config is not None else ""
    checks.append(_check_token_shape(config))
    checks.append(await _check_telegram_probe(token, enabled=telegram_probe, timeout=timeout))
    explanations = [f"{c.name}: {c.detail}" for c in checks if not c.ok]
    return DeepReadiness(
        ok=all(c.ok for c in checks),
        checks=checks,
        explanations=explanations,
    )
