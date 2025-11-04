"""Configuration helpers for the Telegram Linux Admin application."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
from typing import Iterable, List

DEFAULT_CONFIG_FILE = os.environ.get("TLA_CONFIG_FILE", "config.json")


class ConfigError(RuntimeError):
    """Raised when the configuration file cannot be processed safely."""


def _ensure_directory(path: str) -> None:
    directory = os.path.dirname(path)
    if directory and not os.path.exists(directory):
        os.makedirs(directory, exist_ok=True)


def validate_token(token: str) -> str:
    """Normalizes and validates a Telegram bot token.

    The token must follow the standard ``<bot id>:<secret>`` structure. A
    descriptive :class:`ValueError` is raised when the token does not meet the
    expected requirements.
    """

    sanitized = token.strip() if isinstance(token, str) else ""
    if not sanitized:
        raise ValueError("Telegram token cannot be empty.")

    if ":" not in sanitized:
        raise ValueError(
            "Telegram tokens must contain ':' separating the numeric bot ID and secret."
        )

    bot_id, secret = sanitized.split(":", 1)
    if not bot_id.isdigit():
        raise ValueError("Telegram token must start with the numeric bot ID before ':'.")

    if not secret:
        raise ValueError("Telegram token must include the secret value after ':'.")

    if secret != secret.strip() or any(ch.isspace() for ch in secret):
        raise ValueError("Telegram token must not contain whitespace characters in the secret part.")

    return sanitized


class Config:
    """Represents the persistent configuration for the application."""

    def __init__(self, path: str = DEFAULT_CONFIG_FILE):
        self.path = path
        self.telegram_token: str = ""
        self.whitelisted_users: List[int] = []
        self._lock = threading.RLock()
        self.last_error: Exception | None = None
        self.warnings: List[str] = []
        self.load_config()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def load_config(self) -> None:
        """Loads the configuration from disk, falling back to safe defaults."""
        with self._lock:
            self.telegram_token = ""
            self.whitelisted_users = []
            self.last_error = None
            self.warnings = []

            if not os.path.exists(self.path):
                return

            self._audit_permissions()

            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except json.JSONDecodeError as exc:
                self.last_error = ConfigError(
                    f"Invalid JSON in {self.path!r}: {exc.msg}"
                )
                return

            raw_token = data.get("telegram_token", "")
            if isinstance(raw_token, str):
                raw_token = raw_token.strip()
            else:
                if raw_token not in ("", None):
                    self.last_error = ConfigError(
                        f"Invalid telegram_token in {self.path!r}: value must be a string"
                    )
                raw_token = ""

            if raw_token:
                try:
                    self.telegram_token = validate_token(raw_token)
                except ValueError as exc:
                    self.last_error = ConfigError(
                        f"Invalid telegram_token in {self.path!r}: {exc}"
                    )
                    self.telegram_token = ""
            else:
                self.telegram_token = ""

            self.whitelisted_users = self._sanitize_users(
                data.get("whitelisted_users", [])
            )

    def save_config(self) -> None:
        """Persists the current configuration atomically with strict permissions."""
        with self._lock:
            payload = {
                "telegram_token": self.telegram_token,
                "whitelisted_users": self._sanitize_users(self.whitelisted_users),
            }

            _ensure_directory(self.path)
            fd, temp_path = tempfile.mkstemp(
                dir=os.path.dirname(self.path) or ".", prefix="config.", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                    json.dump(payload, tmp, indent=2, sort_keys=True)
                    tmp.flush()
                    os.fsync(tmp.fileno())
                os.replace(temp_path, self.path)
                try:
                    os.chmod(self.path, 0o600)
                except PermissionError:
                    # On file systems that do not support chmod (e.g. FAT32), ignore but warn.
                    self.warnings.append(
                        f"Unable to enforce secure permissions on {self.path!r};"
                        " please ensure the file is not world-readable."
                    )
                except OSError as exc:
                    self.warnings.append(
                        f"Failed to adjust permissions on {self.path!r}: {exc}."
                    )
            finally:
                if os.path.exists(temp_path):
                    os.remove(temp_path)

    def set_token(self, token: str) -> None:
        """Validates and stores a new Telegram bot token."""
        sanitized = validate_token(token)
        with self._lock:
            self.telegram_token = sanitized
            self.save_config()

    def clear_token(self) -> None:
        with self._lock:
            self.telegram_token = ""
            self.save_config()

    def add_whitelisted_user(self, telegram_id: int | str) -> None:
        user_id = self._validate_user_id(telegram_id)
        with self._lock:
            users = set(self.whitelisted_users)
            users.add(user_id)
            self.whitelisted_users = sorted(users)
            self.save_config()

    def remove_whitelisted_user(self, telegram_id: int | str) -> bool:
        user_id = self._validate_user_id(telegram_id, allow_empty=True)
        if user_id is None:
            return False
        with self._lock:
            if user_id in self.whitelisted_users:
                self.whitelisted_users = [
                    uid for uid in self.whitelisted_users if uid != user_id
                ]
                self.save_config()
                return True
        return False

    def replace_whitelist(self, users: Iterable[int | str]) -> None:
        sanitized = self._sanitize_users(users)
        with self._lock:
            self.whitelisted_users = sanitized
            self.save_config()

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------
    @classmethod
    def _validate_user_id(cls, telegram_id: int | str, allow_empty: bool = False) -> int | None:
        if telegram_id in ("", None):
            if allow_empty:
                return None
            raise ValueError("Telegram user ID cannot be empty.")

        if isinstance(telegram_id, str):
            if not telegram_id.strip().isdigit():
                raise ValueError("Telegram user ID must be a positive integer.")
            telegram_id = int(telegram_id.strip())

        if not isinstance(telegram_id, int) or telegram_id <= 0:
            raise ValueError("Telegram user ID must be a positive integer.")

        return telegram_id

    @classmethod
    def _sanitize_users(cls, users: Iterable[int | str]) -> List[int]:
        sanitized: set[int] = set()
        for user in users:
            try:
                sanitized.add(cls._validate_user_id(user))
            except ValueError:
                # Skip invalid entries silently; they will not persist.
                continue
        return sorted(sanitized)

    def _audit_permissions(self) -> None:
        """Ensure the configuration file is not readable by other users."""
        if os.name == "nt":
            return

        try:
            mode = stat.S_IMODE(os.stat(self.path).st_mode)
        except FileNotFoundError:
            return

        if mode & 0o077 == 0:
            return

        try:
            os.chmod(self.path, 0o600)
            self.warnings.append(
                f"Configuration file {self.path!r} had permissions {mode:#o};"
                " tightened to 0o600."
            )
        except PermissionError:
            self.warnings.append(
                f"Configuration file {self.path!r} has insecure permissions ({mode:#o})"
                " and could not be corrected automatically."
            )
        except OSError as exc:
            self.warnings.append(
                f"Unable to tighten permissions on {self.path!r}: {exc}."
            )


# Singleton instance used across the application.
config = Config()

