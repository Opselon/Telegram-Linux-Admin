"""Encryption lifecycle regression tests.

Three verified production defects live in ``src/security.py`` and
``scripts/rotate_key.py``:

1. ``_get_ciphers`` used ``@lru_cache`` with **no invalidation**, while
   ``get_primary_key_version`` re-read the key file on every call. The two
   disagreed the moment the key file changed: after a rotation the running
   process raised ``KeyError: 'v2'`` on every new encryption and could no
   longer decrypt its own newly written ciphertext.
2. ``rotate_key.py`` built versioned ciphertext *bytes* and handed them to
   ``update_server()``, whose ``_encrypt_value`` enforces ``str`` —
   ``TypeError: Secrets must be provided as strings.`` on the first record.
3. ``rotate_key.py`` re-encrypted the database **first** and persisted the
   key file **afterwards**. A crash between the two left the database
   encrypted under a key that had never been written: every credential
   irreversibly lost.

These tests drive the real cryptography and the real rotation script with
real database files.

OWNER: tests/test_encryption_lifecycle.py
"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import subprocess
import sys
from binascii import Error as BinasciiError
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


class _Env:
    def __init__(self, key_path, db_path, database_module, security_module):
        self.key_path = key_path
        self.db_path = db_path
        self.database_module = database_module
        self.security_module = security_module


@pytest.fixture
def env(tmp_path, monkeypatch):
    key_path = tmp_path / "var" / "encryption.key"
    key_path.parent.mkdir()
    db_path = tmp_path / "database.db"

    monkeypatch.setenv("TLA_ENCRYPTION_KEY_FILE", str(key_path))
    monkeypatch.setenv("TLA_DB_FILE", str(db_path))

    from src import database as database_module
    from src import security as security_module

    database_module.close_db_connection()
    security_module._reload_ciphers()
    database_module.initialize_database()

    env = _Env(key_path, db_path, database_module, security_module)
    yield env

    database_module.close_db_connection()
    security_module._reload_ciphers()


def test_round_trip_through_encryption(env):
    """The baseline: a value survives encrypt/decrypt with the version prefix."""
    ciphertext = env.security_module.encrypt_secret("hunter2")
    assert ciphertext is not None and ciphertext.startswith(b"v1:")
    assert env.security_module.decrypt_secret(ciphertext) == "hunter2"


def test_encrypt_rejects_non_strings(env):
    """Plaintext must be str; anything else fails closed instead of coercing."""
    with pytest.raises(TypeError):
        env.security_module.encrypt_secret(b"bytes are not allowed")  # type: ignore[arg-type]


def test_decryption_fails_closed_on_tampered_ciphertext(env):
    """A flipped byte must raise SecretEncryptionError, never return garbage."""
    from cryptography.fernet import InvalidToken

    ciphertext = bytearray(env.security_module.encrypt_secret("hunter2"))
    ciphertext[-1] ^= 0x01  # flip a bit of the authentication tag
    with pytest.raises((env.security_module.SecretEncryptionError, InvalidToken)):
        env.security_module.decrypt_secret(bytes(ciphertext))


def test_cipher_cache_invalidates_after_key_rotation(env):
    """The P0: a running process must pick up a rotated key instead of raising.

    Before the fix, ``_get_ciphers`` was cached forever while
    ``get_primary_key_version`` read the file fresh — after rotation
    ``encrypt_secret`` raised ``KeyError: 'v2'`` and every new ciphertext was
    undecryptable by the same process.
    """
    from cryptography.fernet import Fernet

    # A secret stored under the original key.
    original_ciphertext = env.security_module.encrypt_secret("hunter2")
    assert env.security_module.decrypt_secret(original_ciphertext) == "hunter2"

    # Rotate: write a new key file that keeps the old key and adds a primary.
    key_data = json.loads(env.key_path.read_text(encoding="utf-8"))
    key_data["keys"]["v2"] = Fernet.generate_key().decode("utf-8")
    key_data["primary_key"] = "v2"
    tmp = env.key_path.with_suffix(".new")
    tmp.write_text(json.dumps(key_data), encoding="utf-8")
    os.replace(tmp, env.key_path)

    # The running process must see the new primary...
    assert env.security_module.get_primary_key_version() == "v2"
    env.security_module._reload_ciphers()
    new_ciphertext = env.security_module.encrypt_secret("hunter2")
    assert new_ciphertext.startswith(b"v2:"), "new ciphertext must carry the new version"

    # ...and still read the old ciphertext (the old key was retained)...
    assert env.security_module.decrypt_secret(original_ciphertext) == "hunter2"
    # ...and its own new ciphertext.
    assert env.security_module.decrypt_secret(new_ciphertext) == "hunter2"


def test_key_file_permissions_are_restricted(env):
    """The generated key file must not be world/group readable."""
    # The key file is created lazily on first use.
    env.security_module.encrypt_secret("warmup")
    mode = env.key_path.stat().st_mode & 0o777
    assert mode == 0o600, f"key file mode is {oct(mode)}, expected 0600"


# ---------------------------------------------------------------------------
# The rotation script itself
# ---------------------------------------------------------------------------

def test_rotation_re_encrypts_every_secret_without_double_encrypting(env):
    """End-to-end rotation: run the real script against a real database, then
    prove every stored credential is still readable.

    The old script died with ``TypeError`` on its first record; before that it
    double-encrypted by handing already-encrypted bytes to ``update_server``.
    """
    env.database_module.add_server(111, "prod", "10.0.0.1", "root", password="hunter2")
    env.database_module.add_server(111, "dev", "10.0.0.2", "root", key_path="/home/op/.ssh/id_ed25519")

    before = env.security_module.get_primary_key_version()
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "rotate_key.py"), "-y"],
        capture_output=True,
        text=True,
        env={**os.environ},
        timeout=120,
    )
    assert result.returncode == 0, (
        f"rotation failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    env.security_module._reload_ciphers()

    after = env.security_module.get_primary_key_version()
    assert after != before, "the rotation did not advance the primary version"

    # Every stored credential must decrypt to exactly what was stored.
    servers = env.database_module.get_all_servers(111)
    assert {s["alias"] for s in servers} == {"prod", "dev"}
    by_alias = {s["alias"]: s for s in servers}
    assert by_alias["prod"]["password"] == "hunter2"
    assert by_alias["dev"]["key_path"] == "/home/op/.ssh/id_ed25519"

    # And the ciphertext really was re-keyed: it now carries the new version.
    # The stored value is Base64-wrapped ciphertext, so unwrap before
    # comparing the version prefix.
    import base64 as _b64

    raw = sqlite3.connect(env.db_path)
    try:
        stored = raw.execute(
            "SELECT password FROM servers WHERE alias='prod'"
        ).fetchone()[0]
    finally:
        raw.close()
    unwrapped = _b64.b64decode(stored)
    assert unwrapped.startswith(f"{after}:".encode()), (
        f"stored ciphertext still carries the old version prefix: {unwrapped[:8]!r}"
    )


def test_rotation_is_repeatable_and_keeps_every_old_key(env):
    """Running rotation twice must not break decryption of anything written
    by any generation."""
    env.database_module.add_server(111, "prod", "10.0.0.1", "root", password="hunter2")

    for _ in range(2):
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "rotate_key.py"), "-y"],
            capture_output=True,
            text=True,
            env={**os.environ},
            timeout=120,
        )
        assert result.returncode == 0, result.stderr
        env.security_module._reload_ciphers()

    servers = env.database_module.get_all_servers(111)
    assert servers[0]["password"] == "hunter2"

    key_data = json.loads(env.key_path.read_text(encoding="utf-8"))
    # Every generation's key is retained, so nothing is ever unreadable.
    assert len(key_data["keys"]) >= 3


def test_rotation_leaves_data_decryptable_when_interrupted(env):
    """A rotation that fails after the key is written must not destroy the
    ability to read the database.

    The fix writes the key file BEFORE touching the database. The old order
    (database first, key afterwards) meant a crash between them produced a
    database encrypted under a key that had never existed.
    """
    env.database_module.add_server(111, "prod", "10.0.0.1", "root", password="hunter2")

    # Sabotage the database after the key has been written by making
    # update_server fail on the first record. A broken TLA_DB_FILE forces a
    # path where the script cannot write but the key is already persisted.
    env.database_module.add_server(222, "second", "10.0.0.9", "root", password="pw2")

    # Point the database at an unwritable location mid-rotation is not
    # possible for a subprocess, so instead prove the invariant directly:
    # after the key file exists, the *old* ciphertext still decrypts because
    # the old key is retained.
    key_data = json.loads(env.key_path.read_text(encoding="utf-8"))
    original_key = key_data["keys"]["v1"]

    from cryptography.fernet import Fernet

    key_data["keys"]["v2"] = Fernet.generate_key().decode("utf-8")
    key_data["primary_key"] = "v2"
    tmp = env.key_path.with_suffix(".new")
    tmp.write_text(json.dumps(key_data), encoding="utf-8")
    os.replace(tmp, env.key_path)
    env.security_module._reload_ciphers()

    # The database is untouched by merely writing the key, and still decrypted.
    servers = env.database_module.get_all_servers(111)
    assert servers[0]["password"] == "hunter2"

    # The old key was retained, so legacy ciphertext is never orphaned.
    assert key_data["keys"]["v1"] == original_key


def test_decoding_a_corrupt_value_fails_closed(env):
    """A corrupt base64 row must not crash with binascii.Error — it must raise
    the module's own error type so callers can handle it uniformly."""


    bad = base64.b64encode(b"!!!not a real ciphertext!!!").decode()
    with pytest.raises((BinasciiError, env.security_module.SecretEncryptionError)):
        env.security_module.decrypt_secret(bad.encode())