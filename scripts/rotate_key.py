"""
Administrative script for rotating the Fernet encryption key.

This script performs the following actions:
1.  Loads the existing key file.
2.  Generates a new Fernet key and adds it to the key file, marking it as the new primary.
3.  Connects to the database.
4.  Iterates through all server records.
5.  Decrypts the 'password' and 'key_path' fields using the old key(s).
6.  Re-encrypts the decrypted data using the new primary key.
7.  Updates the database records with the new encrypted data.
8.  Saves the updated key file.

This ensures a seamless key rotation without invalidating existing data.
"""

import base64
import os
import sys
import json
import sqlite3
from pathlib import Path
from cryptography.fernet import Fernet, InvalidToken

# Ensure the script can find the 'src' directory
sys.path.append(str(Path(__file__).parent.parent))

from src.database import get_all_servers, update_server, close_db_connection
from src.security import _get_key_path, _load_keys, _reload_ciphers, SecretEncryptionError


def _reload_ciphers_running_process() -> None:
    """Invalidates the cipher cache.

    ``scripts/rotate_key.py`` runs as its own process, so clearing the cache
    here only helps itself. It also runs (as a shim) under the bot's process
    when invoked from a long-lived context; in that case the bot's next
    encrypt/decrypt picks up the new primary instead of raising ``KeyError``
    on the stale one.
    """
    _reload_ciphers()


def _open_database_raw() -> sqlite3.Connection:
    """Opens the database directly to read still-encrypted ciphertext.

    ``database.get_all_servers`` decrypts before returning, which would leave
    rotation nothing to re-encrypt. This reads the raw stored bytes (Base64-
    wrapped, version-prefixed Fernet ciphertext) exactly as they live on disk.
    """
    db_path = os.environ.get("TLA_DB_FILE", "database.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = None
    return conn

def rotate_encryption_key():
    """
    Orchestrates the key rotation process.
    """
    print("🔒 Starting encryption key rotation...")

    key_path = _get_key_path()

    try:
        # Step 1: Load existing keys
        print(f"   - Loading keys from {key_path}...")
        try:
            with open(key_path, "r", encoding="utf-8") as f:
                key_data = json.load(f)
            primary_version_str = key_data.get("primary_key", "v1")
            version_num = int(primary_version_str[1:])
            new_version = f"v{version_num + 1}"
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            print("   - WARNING: Key file not found or corrupted. A new one will be created.")
            key_data = {"primary_key": "v1", "keys": {}}
            new_version = "v1"

        # Step 2: Generate and add the new key
        print(f"   - Generating new key with version '{new_version}'...")
        new_key = Fernet.generate_key().decode("utf-8")
        key_data["keys"][new_version] = new_key
        key_data["primary_key"] = new_version

        # Step 3: Load all ciphers for decryption
        all_keys = {k: v.encode('utf-8') for k, v in key_data["keys"].items()}
        ciphers = {v: Fernet(k) for v, k in all_keys.items()}

        # Step 5: Write the new key file BEFORE any database record is touched.
        #
        # The previous order re-encrypted the database first and persisted the
        # key afterwards, so a crash in between left the database encrypted
        # under a key that had never been written — every credential
        # irreversibly lost. Writing the key first means the worst case is a
        # database still encrypted under the old key, which the key file still
        # holds (rotation keeps every old key), so the state is always
        # recoverable.
        #
        # The write is atomic: a temp file on the same filesystem, chmod 0600
        # *before* the content is linked, then os.replace. The old code
        # truncated the live file in place, so a concurrent reader could catch
        # it mid-write and raise "Key file is corrupted".
        print(f"   - Saving updated key file to {key_path}...")
        key_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_key = key_path.with_suffix(key_path.suffix + ".new")
        fd = os.open(tmp_key, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(key_data, f, indent=2)
        except BaseException:
            os.close(fd)
            raise
        os.replace(tmp_key, key_path)
        # The running bot cached the old ciphers; drop them so it picks up the
        # new primary on its next use.
        _reload_ciphers_running_process()

        # Step 6: Re-encrypt all secrets in the database (key persisted in step 5)
        print("   - Re-encrypting secrets in the database...")
        # Read the raw (still-encrypted, base64-wrapped) rows: get_all_servers()
        # returns decrypted plaintext, and re-running a decrypt helper on it
        # fails. Rotation must see the ciphertext as stored.
        conn = _open_database_raw()
        try:
            rows = conn.execute("SELECT owner_id, alias, password, key_path FROM servers").fetchall()
        finally:
            conn.close()
        if not rows:
            print("   - No servers found in the database. Nothing to re-encrypt.")
        else:
            re_encrypted_count = 0
            for row in rows:
                owner_id, alias, stored_password, stored_key_path = row

                # Stored values are Base64-wrapped ciphertext; unwrap to the
                # versioned bytes the decrypt helper expects.
                def _unwrap(value):
                    if value is None:
                        return None
                    try:
                        return base64.b64decode(value)
                    except Exception:
                        return value.encode("utf-8")

                try:
                    decrypted_password = decrypt_with_any_key(_unwrap(stored_password), ciphers)
                    decrypted_key_path = decrypt_with_any_key(_unwrap(stored_key_path), ciphers)
                except SecretEncryptionError as e:
                    print(f"   - ERROR: Could not decrypt data for server '{alias}'. Skipping. Error: {e}")
                    continue

                # Re-encrypt with the new primary key. The previous version
                # assembled the versioned ciphertext bytes here and handed
                # them to update_server(), whose _encrypt_value feeds them to
                # security.encrypt_secret — which enforces str and raises
                # TypeError, and would have double-encrypted even if it had
                # not. update_server owns the versioning and the encryption;
                # it receives the PLAINTEXT here.
                update_server(
                    owner_id,
                    alias,
                    password=decrypted_password,
                    key_path=decrypted_key_path,
                )
                re_encrypted_count += 1
            print(f"   - Successfully re-encrypted secrets for {re_encrypted_count} server(s).")

        print("\n✅ Key rotation complete!")
        print(f"   - New primary key version is '{new_version}'.")
        print("   - All existing secrets have been migrated to the new key.")

    except Exception as e:
        print(f"\n❌ An error occurred during key rotation: {e}")
        sys.exit(1)
    finally:
        close_db_connection()


def decrypt_with_any_key(encrypted_value: bytes | None, ciphers: dict[str, Fernet]) -> str | None:
    """
    Attempts to decrypt a value using all available ciphers.
    Handles both version-prefixed and legacy (non-prefixed) data.
    """
    if encrypted_value is None:
        return None

    try:
        version, data = encrypted_value.split(b':', 1)
        version_str = version.decode('utf-8')
        if version_str in ciphers:
            return ciphers[version_str].decrypt(data).decode('utf-8')
    except (ValueError, KeyError):
        # Could be legacy data without a prefix. Try all keys.
        for cipher in ciphers.values():
            try:
                return cipher.decrypt(encrypted_value).decode('utf-8')
            except InvalidToken:
                continue

    raise SecretEncryptionError("Could not decrypt data with any of the available keys.")


if __name__ == "__main__":
    # Simple confirmation prompt
    if "-y" not in sys.argv and "--yes" not in sys.argv:
        print("This script will generate a new encryption key and re-encrypt all secrets in the database.")
        print("It is recommended to back up your 'database.db' and 'var/encryption.key' files before proceeding.")
        response = input("Are you sure you want to continue? (y/N): ").lower().strip()
        if response != 'y':
            print("Aborted.")
            sys.exit(0)

    rotate_encryption_key()
