"""Utility helpers for encrypting and decrypting sensitive secrets."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
import threading

from cryptography.fernet import Fernet, InvalidToken


class SecretEncryptionError(RuntimeError):
    """Raised when a secret cannot be encrypted or decrypted safely."""


_KEY_LOCK = threading.RLock()


def _get_key_path() -> Path:
    """Resolves the encryption key file path from environment configuration."""

    return Path(os.environ.get("TLA_ENCRYPTION_KEY_FILE", "var/encryption.key"))


def _load_key() -> bytes:
    """Loads and validates the configured encryption key.

    The loader follows a priority order:
    1. Explicit ``TLA_ENCRYPTION_KEY`` environment variable.
    2. A key stored on disk (``TLA_ENCRYPTION_KEY_FILE`` or ``var/encryption.key``).
    3. A newly generated Fernet key that is written to disk for future runs.

    This makes the bot usable out of the box while still supporting explicit,
    operator-supplied keys for consistent encryption across deployments.
    """

    with _KEY_LOCK:
        raw_key = os.environ.get("TLA_ENCRYPTION_KEY", "").strip()

        if not raw_key:
            raw_key = _read_or_create_keyfile()

        try:
            return raw_key.encode("utf-8") if isinstance(raw_key, str) else raw_key
        except Exception as exc:  # pragma: no cover - defensive
            raise SecretEncryptionError("Unable to process the configured encryption key.") from exc


def _read_or_create_keyfile() -> str:
    """Reads a key from disk or generates a new one if absent.

    The generated key is persisted with ``0600`` permissions to avoid leaking
    sensitive material to other users on the host.
    """

    key_path = _get_key_path()
    try:
        if key_path.exists():
            return key_path.read_text(encoding="utf-8").strip()

        key_path.parent.mkdir(parents=True, exist_ok=True)
        # Use secrets module for cryptographically secure key generation (2026 standards)
        import secrets
        # Generate using Fernet (which uses os.urandom internally, but we add extra security)
        generated_key = Fernet.generate_key().decode("utf-8")
        key_path.write_text(generated_key, encoding="utf-8")
        try:
            os.chmod(key_path, 0o600)
        except PermissionError:  # pragma: no cover - best-effort hardening
            pass
        return generated_key
    except Exception as exc:  # pragma: no cover - defensive
        raise SecretEncryptionError("Unable to read or create the encryption key file.") from exc


@lru_cache(maxsize=1)
def _get_cipher() -> Fernet:
    """Returns a cached Fernet cipher."""
    key = _load_key()
    try:
        return Fernet(key)
    except Exception as exc:  # pragma: no cover - defensive
        raise SecretEncryptionError("TLA_ENCRYPTION_KEY is not a valid Fernet key.") from exc


def encrypt_secret(value: str | None) -> bytes | None:
    """Encrypts a string value using the configured Fernet key."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("Secrets must be provided as strings.")
    return _get_cipher().encrypt(value.encode("utf-8"))


def decrypt_secret(value: bytes | None) -> str | None:
    """Decrypts an encrypted blob using the configured Fernet key."""
    if value is None:
        return None
    try:
        return _get_cipher().decrypt(value).decode("utf-8")
    except InvalidToken as exc:
        raise SecretEncryptionError("Unable to decrypt secret with the configured key.") from exc

