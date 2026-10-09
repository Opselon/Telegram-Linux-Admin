"""Tests for security helpers around encryption keys."""

from __future__ import annotations

import json
import os

import pytest

from src import security


@pytest.fixture(autouse=True)
def clear_key_cache():
    """Reset memoized cipher dictionary between tests to avoid cross-test bleed."""
    security._get_ciphers.cache_clear()
    yield
    security._get_ciphers.cache_clear()


def test_keyfile_is_created_when_missing(monkeypatch, tmp_path):
    """A new Fernet key file (JSON format) should be generated and persisted when absent."""
    keyfile = tmp_path / "nested" / "encryption.key"
    monkeypatch.setenv("TLA_ENCRYPTION_KEY_FILE", str(keyfile))

    encrypted = security.encrypt_secret("data")
    assert encrypted is not None
    decrypted = security.decrypt_secret(encrypted)
    assert decrypted == "data"

    assert keyfile.exists()
    assert os.stat(keyfile).st_mode & 0o777 == 0o600

    data = json.loads(keyfile.read_text(encoding="utf-8"))
    assert data.get("primary_key") == "v1"
    assert "v1" in data.get("keys", {})


def test_encrypt_decrypt_roundtrip(monkeypatch, tmp_path):
    """Encrypt and decrypt roundtrip matches original text."""
    keyfile = tmp_path / "encryption.key"
    monkeypatch.setenv("TLA_ENCRYPTION_KEY_FILE", str(keyfile))

    secret = "my-super-secret-ssh-password-12345"
    encrypted = security.encrypt_secret(secret)
    assert encrypted is not None
    assert encrypted != secret.encode("utf-8")

    decrypted = security.decrypt_secret(encrypted)
    assert decrypted == secret
