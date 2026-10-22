"""Observability package: structured logging, context binding and redaction.

PURPOSE
    Central observability primitives for the bot: JSON/human log formatting,
    context-safe field binding (correlation ids, user ids) and secret
    redaction so tokens never reach the log files.

OWNER
    Lane ``observability`` of the 3.2 remaster wave
    (branch ``feat/3.2-observability``).

CONSUMES
    * stdlib ``logging`` / ``contextvars`` / ``json`` / ``re`` only.

PROVIDES
    * :func:`configure_logging` — root logger setup (human console,
      optional JSON file handler).
    * :func:`bind_context` — contextvars-based, nesting-safe field binding.
    * :func:`current_context` — snapshot of the bound fields.
    * :func:`log_event` — ``event`` + fields structured log emission.
    * :func:`redact` — scrubs Telegram bot tokens, passwords, bearer
      headers and private-key blocks.

INVARIANTS
    * No third-party dependencies; no ``print()`` on library paths.
    * ``bind_context`` restores the previous values on normal exit, on
      exception, and stays isolated between concurrent asyncio tasks.
    * Redaction is conservative: short ordinary text passes through
      unchanged because over-redacting command output would destroy the
      audit trail.

EXTEND
    * Add new secret shapes to ``redact`` (and its tests) rather than at
      call sites; add new context field names to ``_CONTEXT_FIELDS``.
"""

from __future__ import annotations

import contextvars
import json
import logging
import logging.handlers
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Any, ContextManager

__all__ = [
    "configure_logging",
    "bind_context",
    "current_context",
    "log_event",
    "redact",
]

#: Context keys the middleware and the other lanes rely on. Extra keys are
#: accepted too; these are the documented, always-present ones.
_CONTEXT_FIELDS: frozenset[str] = frozenset(
    {"correlation_id", "user_id", "event", "request_type", "alias", "outcome"}
)

#: Default is ``None`` (never a shared mutable dict): a mutable default would be
#: handed to every context and let one task's bindings leak into another's.
_CONTEXT_VAR: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "tla_log_context", default=None
)

_REDACTED = "[REDACTED]"

#: Keys whose values are numeric in the JSON payload even when they arrived as
#: strings through the (``dict[str, str]``) context var.
_NUMERIC_KEYS: frozenset[str] = frozenset(
    {"user_id", "chat_id", "duration_ms", "line", "status"}
)

#: PEM-ish private key blocks (``-----BEGIN RSA PRIVATE KEY-----`` ...).
_KEY_BLOCK_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----.*?"
    r"-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----",
    re.DOTALL,
)

#: ``Authorization: Bearer <token>`` / ``Authorization: Basic <blob>``.
#: The scheme is captured so the redacted line keeps ``Bearer``/``Basic``
#: (useful when triaging which credential type leaked) while the value goes.
_AUTH_HEADER_RE = re.compile(
    r"(?i)\b(authorization)\s*([:=])\s*(bearer|basic|token)?\s*"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s'\"]+)"
)

#: Standalone ``Bearer <token>`` without a header name.
_BEARER_RE = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=\-]{8,}")

#: ``password=...`` / ``api_key: ...`` pairs, quoted or bare. The optional
#: ``[a-z_]*`` prefix catches environment-style names (``PGPASSWORD``,
#: ``DB_PASSWORD``, ``AWS_SECRET_ACCESS_KEY``) that a bare ``\b`` misses.
_ASSIGNMENT_RE = re.compile(
    r"(?i)\b([a-z_]*(?:password|passwd|pwd|secret|token|api_?key|access_?key))"
    r"(\s*[=:]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|\S+)"
)

#: Telegram bot token shape: ``<digits>:<base64url-ish secret>``. The secret
#: is >=16 chars (real tokens are ~35); the 6+ digit prefix keeps clock times
#: and ``key:value`` output such as ``12:34`` untouched. The trailing
#: lookahead allows a URL path (``/sendMessage``) right after the token.
_BOT_TOKEN_RE = re.compile(
    r"\b(\d{6,20}):([A-Za-z0-9_\-]{16,})(?![A-Za-z0-9_\-])"
)


# ---------------------------------------------------------------------------
# Context binding
# ---------------------------------------------------------------------------
def current_context() -> dict[str, str]:
    """Returns a snapshot of the structured-log fields bound right now.

    A copy is returned so a caller cannot mutate the context of a sibling
    task through the dict it got back.
    """
    return dict(_CONTEXT_VAR.get() or {})


class _ContextBinder(ContextManager[None]):
    """``with``-scoped contextvar binding that restores the prior fields."""

    __slots__ = ("_fields", "_token")

    def __init__(self, fields: dict[str, str]) -> None:
        self._fields = fields
        self._token: contextvars.Token[dict[str, str] | None] | None = None

    def __enter__(self) -> None:
        if self._fields:
            merged = {**(_CONTEXT_VAR.get() or {}), **self._fields}
            self._token = _CONTEXT_VAR.set(merged)
        return None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # Resetting the token restores exactly the pre-``__enter__`` mapping,
        # which is what makes nesting and exception exits correct.
        if self._token is not None:
            _CONTEXT_VAR.reset(self._token)
            self._token = None


def bind_context(**fields: object) -> ContextManager[None]:
    """Binds log fields (``correlation_id``, ``user_id``, ...) for a block.

    Uses ``contextvars`` so concurrent ``asyncio`` tasks each see their own
    values, and restores the previous state on normal exit *and* on
    exception — a handler that raises must not leak its correlation id into
    the next command.
    """
    cleaned = {str(k): str(v) for k, v in fields.items() if v is not None}
    return _ContextBinder(cleaned)


def log_event(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
    **fields: object,
) -> None:
    """Emits one structured record for ``event`` with its fields attached.

    The record carries ``ctx_event`` plus ``ctx_<field>`` entries, matching
    the ``ctx_*`` convention the existing formatter and ``src.main`` already
    use, so no formatter changes are needed to consume it.
    """
    extra: dict[str, Any] = {"ctx_event": event}
    for key, value in fields.items():
        extra[f"ctx_{key}"] = value
    logger.log(level, event, extra=extra)


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------
def _drop_key_block(match: re.Match[str]) -> str:
    return f"-----BEGIN PRIVATE KEY-----{_REDACTED}-----END PRIVATE KEY-----"


def _drop_auth_header(match: re.Match[str]) -> str:
    scheme = match.group(3)
    tail = f" {scheme}" if scheme else ""
    return f"{match.group(1)}{match.group(2)}{tail} {_REDACTED}"


def _drop_bearer(match: re.Match[str]) -> str:
    return f"{match.group(1)} {_REDACTED}"


def _drop_assignment(match: re.Match[str]) -> str:
    return f"{match.group(1)}{match.group(2)}{_REDACTED}"


def _drop_bot_token(match: re.Match[str]) -> str:
    return f"{match.group(1)}:{_REDACTED}"


def redact(value: str) -> str:
    """Scrubs secrets out of ``value`` before it is logged or stored.

    Covers Telegram bot tokens (``digits:base64ish``), ``password=...`` style
    assignments, ``Authorization``/``Bearer`` headers and private-key blocks.
    Ordinary short text without those shapes is returned unchanged.
    """
    if not value:
        return value

    text = str(value)
    text = _KEY_BLOCK_RE.sub(_drop_key_block, text)
    text = _AUTH_HEADER_RE.sub(_drop_auth_header, text)
    text = _BEARER_RE.sub(_drop_bearer, text)
    text = _ASSIGNMENT_RE.sub(_drop_assignment, text)
    text = _BOT_TOKEN_RE.sub(_drop_bot_token, text)
    return text


#: Field names whose whole value is a secret; the value never needs to match
#: a token shape because the key already tells us what it holds.
_SECRET_KEYS: frozenset[str] = frozenset(
    {"password", "passwd", "pwd", "secret", "token", "api_key", "access_key"}
)


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in _SECRET_KEYS)


def _field_value(key: str, value: object) -> object:
    """Renders one log field for output.

    Numbers stay numeric (dashboards and ``jq`` filter on numeric ranges).
    Text is redacted, and a field whose *name* is secret-ish is dropped
    wholesale — a bare ``password="hunter2"`` has no shape for the regexes to
    recognise, but the key already says it is a credential. Well-known
    identity keys arrive as strings from the ``dict[str, str]`` context but
    are numeric domain-wise, so they are parsed back.
    """
    if isinstance(value, (bool, int, float)):
        return value
    if _is_secret_key(key):
        return _REDACTED
    text = redact(str(value))
    if key in _NUMERIC_KEYS:
        try:
            return int(text)
        except ValueError:
            try:
                return float(text)
            except ValueError:
                return text
    return text


# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------
class _JsonFileFormatter(logging.Formatter):
    """Emits exactly one JSON object per line for the rotating file sink."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }
        # Bound context is the transport for identity fields; the formatter
        # turns the well-known numeric ones back into numbers.
        payload.update(
            {k: _field_value(k, v) for k, v in current_context().items()}
        )
        for key, value in record.__dict__.items():
            if key.startswith("ctx_"):
                payload[key[4:]] = _field_value(key[4:], value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class _HumanConsoleFormatter(logging.Formatter):
    """Readable single-line console output with trailing key=value context."""

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        merged: dict[str, object] = dict(current_context())
        for key, value in record.__dict__.items():
            if key.startswith("ctx_"):
                merged[key[4:]] = value
        extras = [f"{k}={_field_value(k, v)}" for k, v in merged.items()]
        if extras:
            return f"{base} | {' '.join(extras)}"
        return base


def configure_logging(
    *,
    level: int = logging.INFO,
    log_file: Path | str | None = None,
    json_file: bool = True,
    force: bool = True,
) -> None:
    """Configures the root logger: human console plus an optional file sink.

    ``log_file`` is written as one JSON object per line when ``json_file`` is
    true (machine consumption) and in the human format otherwise; the console
    always stays human-readable for the operator.
    """
    handlers: list[logging.Handler] = []

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(
        _HumanConsoleFormatter(
            fmt="%(asctime)s %(levelname)s %(name)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    handlers.append(console)

    if log_file is not None:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler: logging.Handler = logging.handlers.RotatingFileHandler(
            path,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(
            _JsonFileFormatter() if json_file else _HumanConsoleFormatter()
        )
        handlers.append(file_handler)

    logging.basicConfig(level=level, handlers=handlers, force=force)
