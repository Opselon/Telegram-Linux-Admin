"""Utility helpers for encrypting and decrypting sensitive secrets."""

from __future__ import annotations

import os
import json
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


def _load_keys() -> dict[str, bytes]:
    """Loads and validates the configured encryption keys from a versioned JSON file.

    If the file doesn't exist, it generates a new primary key and creates the file.
    """
    with _KEY_LOCK:
        key_path = _get_key_path()
        if not key_path.exists():
            # Create a new key file if it doesn't exist
            new_key = Fernet.generate_key()
            key_data = {
                "primary_key": "v1",
                "keys": {"v1": new_key.decode("utf-8")}
            }
            key_path.parent.mkdir(parents=True, exist_ok=True)
            key_path.write_text(json.dumps(key_data, indent=2), encoding="utf-8")
            try:
                os.chmod(key_path, 0o600)
            except PermissionError:
                pass
            return {"v1": new_key}

        try:
            key_data = json.loads(key_path.read_text(encoding="utf-8"))
            keys = {k: v.encode("utf-8") for k, v in key_data["keys"].items()}
            if key_data["primary_key"] not in keys:
                raise SecretEncryptionError("Primary key not found in key file.")
            return keys
        except (json.JSONDecodeError, KeyError) as exc:
            raise SecretEncryptionError("Key file is corrupted or has an invalid format.") from exc


@lru_cache(maxsize=1)
def _get_ciphers() -> dict[str, Fernet]:
    """Returns a cached dictionary of Fernet ciphers, one for each key."""
    keys = _load_keys()
    try:
        return {version: Fernet(key) for version, key in keys.items()}
    except Exception as exc:
        raise SecretEncryptionError("One or more keys are invalid Fernet keys.") from exc


def get_primary_key_version() -> str:
    """Returns the version of the primary encryption key."""
    key_path = _get_key_path()
    if not key_path.exists():
        _load_keys()  # Ensure key file is created
    key_data = json.loads(key_path.read_text(encoding="utf-8"))
    return key_data["primary_key"]


def encrypt_secret(value: str | None) -> bytes | None:
    """Encrypts a string value using the primary Fernet key and prepends the key version."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("Secrets must be provided as strings.")

    primary_key_version = get_primary_key_version()
    ciphers = _get_ciphers()
    cipher = ciphers[primary_key_version]

    encrypted_data = cipher.encrypt(value.encode("utf-8"))
    # Prepend the key version and a separator to the ciphertext
    return f"{primary_key_version}:".encode("utf-8") + encrypted_data


def decrypt_secret(value: bytes | None) -> str | None:
    """Decrypts an encrypted blob by detecting the key version and using the corresponding key."""
    if value is None:
        return None

    try:
        # Split the version from the ciphertext
        parts = value.split(b":", 1)
        if len(parts) != 2:
            # Fallback for old format without version prefix
            primary_key_version = get_primary_key_version()
            cipher = _get_ciphers()[primary_key_version]
            return cipher.decrypt(value).decode("utf-8")

        key_version, encrypted_data = parts
        key_version_str = key_version.decode("utf-8")

        ciphers = _get_ciphers()
        if key_version_str not in ciphers:
            raise SecretEncryptionError(f"Unknown key version '{key_version_str}' found in data.")

        cipher = ciphers[key_version_str]
        return cipher.decrypt(encrypted_data).decode("utf-8")
    except InvalidToken as exc:
        raise SecretEncryptionError("Unable to decrypt secret. The data may be corrupt or the key incorrect.") from exc

