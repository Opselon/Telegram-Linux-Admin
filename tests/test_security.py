"""Tests for security helpers around encryption keys."""

from __future__ import annotations

import os

import pytest

from src import security


@pytest.fixture(autouse=True)
def clear_key_cache(monkeypatch):
    """Reset memoized cipher between tests to avoid cross-test bleed."""
    security._get_cipher.cache_clear()
    monkeypatch.delenv("TLA_ENCRYPTION_KEY", raising=False)
    yield
    security._get_cipher.cache_clear()


def test_env_key_takes_priority(monkeypatch, tmp_path):
    """Explicit environment variable should be used over files."""
    keyfile = tmp_path / "encryption.key"
    keyfile.write_text("should-not-be-used")
    monkeypatch.setenv("TLA_ENCRYPTION_KEY", "K1pGzzKquqCPIAHJ_Tz6lhZKBiJMOrlrNUmt1Bd6Rwk=")
    monkeypatch.setenv("TLA_ENCRYPTION_KEY_FILE", str(keyfile))

    cipher = security._get_cipher()
    decrypted = cipher.decrypt(cipher.encrypt(b"secret"))

    assert decrypted == b"secret"


def test_keyfile_is_created_when_missing(monkeypatch, tmp_path):
    """A new Fernet key should be generated and persisted when absent."""
    keyfile = tmp_path / "nested" / "encryption.key"
    monkeypatch.setenv("TLA_ENCRYPTION_KEY_FILE", str(keyfile))

    cipher = security._get_cipher()
    decrypted = cipher.decrypt(cipher.encrypt(b"data"))

    assert decrypted == b"data"
    assert keyfile.exists()
    assert os.stat(keyfile).st_mode & 0o777 == 0o600

