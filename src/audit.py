"""Structured audit trail for every privileged or security-relevant action.

The bot handles other people's SSH credentials, so "who did what to which
server" must be answerable after the fact — including after a restart.  This
module writes bounded JSON-lines records to disk:

* one append-only file per process, rotated by size so an unattended
  deployment cannot fill the disk;
* every record carries an ``event`` name, the acting Telegram user, the
  target server alias when applicable, an outcome, and a wall clock
  timestamp (UTC, ISO 8601);
* secrets are never recorded — values are passed as named context only.

Read the log with ``jq``::

    jq -c 'select(.outcome == "denied")' var/log/audit.log
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

try:  # pragma: no cover - import guard keeps audit usable standalone
    from .metrics import METRICS
except Exception:  # pragma: no cover
    METRICS = None  # type: ignore[assignment]

DEFAULT_AUDIT_DIR = os.environ.get("TLA_AUDIT_DIR", "var/log")
DEFAULT_MAX_BYTES = int(os.environ.get("TLA_AUDIT_MAX_BYTES", 5 * 1024 * 1024))  # 5 MiB
DEFAULT_KEEP_FILES = int(os.environ.get("TLA_AUDIT_KEEP_FILES", 3))
DEFAULT_ENABLED = os.environ.get("TLA_AUDIT_ENABLED", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


class AuditLogger:
    """Bounded, append-only JSON-lines audit log with size rotation."""

    def __init__(
        self,
        directory: str | os.PathLike[str] = DEFAULT_AUDIT_DIR,
        *,
        filename: str = "audit.log",
        max_bytes: int = DEFAULT_MAX_BYTES,
        keep_files: int = DEFAULT_KEEP_FILES,
    ) -> None:
        self._directory = Path(directory)
        self._path = self._directory / filename
        self._max_bytes = max(64 * 1024, max_bytes)
        self._keep_files = max(1, keep_files)
        self._lock = threading.Lock()
        self._enabled = DEFAULT_ENABLED
        self._sequence = 0
        self._write_failed = False
        if not self._enabled:
            logger.warning("Audit log disabled via TLA_AUDIT_ENABLED")

    # -- lifecycle -------------------------------------------------------
    @property
    def path(self) -> Path:
        return self._path

    def set_enabled(self, enabled: bool) -> None:
        """Toggles writing.  Records are still logged to the normal logger."""
        with self._lock:
            self._enabled = bool(enabled)

    def is_writable(self) -> bool:
        """Returns True when the audit log can actually be appended to."""
        if not self._enabled:
            return True
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            with open(self._path, "a", encoding="utf-8"):
                pass
            return True
        except OSError:
            return False

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    # -- writing ---------------------------------------------------------
    def record(
        self,
        event: str,
        *,
        user_id: int | None = None,
        alias: str | None = None,
        outcome: str = "success",
        **context: Any,
    ) -> dict[str, Any]:
        """Emits one audit record and returns the record that was written.

        ``context`` must never carry secrets — pass names and identifiers,
        not passwords or key material.
        """
        with self._lock:
            self._sequence += 1
            record: dict[str, Any] = {
                "timestamp": _utcnow(),
                "seq": self._sequence,
                "event": event,
                "outcome": outcome,
            }
            if user_id is not None:
                record["user_id"] = user_id
            if alias is not None:
                record["alias"] = alias
            if context:
                # Flatten nested mappings so every jq filter stays one level.
                flat: dict[str, Any] = {}
                for key, value in context.items():
                    if isinstance(value, Mapping):
                        flat.update({f"{key}.{k}": v for k, v in value.items()})
                    else:
                        flat[key] = value
                record["context"] = flat

            line = json.dumps(record, default=str, ensure_ascii=False)
            try:
                if self._enabled:
                    self._write(line)
                self._write_failed = False
            except Exception as exc:  # pragma: no cover - audit must never break the bot
                if METRICS is not None:
                    METRICS.inc("audit_write_failures_total")
                if not self._write_failed:
                    # Warn once with an actionable hint: a silently unwritable
                    # audit directory is a security regression, not a detail.
                    self._write_failed = True
                    logger.error(
                        "Audit log is NOT writable at %s (%s). Records are being "
                        "lost. Fix ownership/permissions of %s or set TLA_AUDIT_DIR "
                        "to a writable path (in Docker the process runs as uid 1000).",
                        self._path, exc, self._directory,
                    )
                else:
                    logger.debug("Audit write failed for %r", event, exc_info=True)
            return record

    # -- internals -------------------------------------------------------
    def _write(self, line: str) -> None:
        """Appends one line, rotating when the size budget is exceeded."""
        should_rotate = False
        try:
            should_rotate = self._path.exists() and self._path.stat().st_size >= self._max_bytes
        except OSError:
            pass

        if should_rotate:
            self._rotate()

        self._directory.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def _rotate(self) -> None:
        """Rotates ``audit.log`` -> ``audit.log.1`` -> ... dropping the oldest."""
        suffix = self._path.suffix
        for index in range(self._keep_files, 0, -1):
            source = self._path.with_suffix(suffix + f".{index}")
            if index >= self._keep_files:
                # Drop the oldest archive so the family stays bounded.
                if source.exists():
                    source.unlink()
                continue
            target = self._path.with_suffix(suffix + f".{index + 1}")
            if source.exists():
                source.replace(target)
        if self._path.exists():
            self._path.replace(self._path.with_suffix(self._path.suffix + ".1"))

    # -- introspection (used by the admin ``/audit`` command) ------------
    def tail(self, limit: int = 20, *, event: str | None = None) -> list[dict[str, Any]]:
        """Returns the most recent records, oldest first."""
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError:
            logger.warning("Audit read failed", exc_info=True)
            return []

        records: list[dict[str, Any]] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event is None or record.get("event") == event:
                records.append(record)
        return records[-limit:]

    def counts(self) -> dict[str, int]:
        """Returns per-event totals, useful for the admin dashboard."""
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        totals: dict[str, int] = {}
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            name = str(record.get("event", "unknown"))
            totals[name] = totals.get(name, 0) + 1
        return totals


#: Process-wide audit logger.  Disabled in tests via ``set_enabled(False)``.
audit = AuditLogger()

