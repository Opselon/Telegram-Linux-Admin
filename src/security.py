"""Utility helpers for encrypting and decrypting sensitive secrets."""

from __future__ import annotations

import os
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken


class SecretEncryptionError(RuntimeError):
    """Raised when a secret cannot be encrypted or decrypted safely."""


def _load_key() -> bytes:
    """Loads and validates the configured encryption key."""
    key = os.environ.get("TLA_ENCRYPTION_KEY", "").strip()
    if not key:
        raise SecretEncryptionError(
            "TLA_ENCRYPTION_KEY environment variable must be set to a valid Fernet key."
        )
    try:
        return key.encode("utf-8") if isinstance(key, str) else key
    except Exception as exc:  # pragma: no cover - defensive
        raise SecretEncryptionError("Unable to process the configured encryption key.") from exc


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

