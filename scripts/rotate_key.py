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

import os
import sys
import json
from pathlib import Path
from cryptography.fernet import Fernet, InvalidToken

# Ensure the script can find the 'src' directory
sys.path.append(str(Path(__file__).parent.parent))

from src.database import get_all_servers, update_server, close_db_connection
from src.security import _get_key_path, _load_keys, SecretEncryptionError

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

        # Step 4: Re-encrypt all secrets in the database
        print("   - Re-encrypting secrets in the database...")
        servers = get_all_servers()
        if not servers:
            print("   - No servers found in the database. Nothing to re-encrypt.")
        else:
            re_encrypted_count = 0
            for server in servers:
                owner_id = server["owner_id"]
                alias = server["alias"]

                # Decrypt with old keys
                try:
                    decrypted_password = decrypt_with_any_key(server.get("password"), ciphers)
                    decrypted_key_path = decrypt_with_any_key(server.get("key_path"), ciphers)
                except SecretEncryptionError as e:
                    print(f"   - ERROR: Could not decrypt data for server '{alias}'. Skipping. Error: {e}")
                    continue

                # Re-encrypt with the new primary key
                new_cipher = ciphers[new_version]
                encrypted_password = new_cipher.encrypt(decrypted_password.encode('utf-8')) if decrypted_password else None
                encrypted_key_path = new_cipher.encrypt(decrypted_key_path.encode('utf-8')) if decrypted_key_path else None

                # Prepend version info for storage
                final_password = f"{new_version}:".encode('utf-8') + encrypted_password if encrypted_password else None
                final_key_path = f"{new_version}:".encode('utf-8') + encrypted_key_path if encrypted_key_path else None

                update_server(
                    owner_id,
                    alias,
                    password=final_password,
                    key_path=final_key_path
                )
                re_encrypted_count += 1
            print(f"   - Successfully re-encrypted secrets for {re_encrypted_count} server(s).")

        # Step 5: Write the new key file
        print(f"   - Saving updated key file to {key_path}...")
        key_path.parent.mkdir(parents=True, exist_ok=True)
        with open(key_path, "w", encoding="utf-8") as f:
            json.dump(key_data, f, indent=2)
        os.chmod(key_path, 0o600)

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
