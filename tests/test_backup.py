"""Tests for the backup and restore functionality."""
import os
import random
import string
import tempfile
import tarfile
import unittest
from pathlib import Path
from unittest.mock import patch
from cryptography.fernet import Fernet
from src.backup_manager import BackupManager
from src import database
from src import security
class TestBackupManager(unittest.TestCase):
    """Tests for the BackupManager class."""
    def setUp(self):
        """Set up a temporary database and encryption key for each test."""
        db_fp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        key_fp = tempfile.NamedTemporaryFile(delete=False, suffix=".key")
        self.db_path = db_fp.name
        self.key_path = key_fp.name
        db_fp.close()
        key_fp.close()
        # Generate and write a valid key
        key = Fernet.generate_key()
        with open(self.key_path, "wb") as f:
            f.write(key)
        os.environ["TLA_DB_FILE"] = self.db_path
        os.environ["TLA_ENCRYPTION_KEY_FILE"] = self.key_path
        # Force re-initialization of the database and cipher
        database.reset_connection()
        database.initialize_database()
        self.backup_manager = BackupManager()
    def tearDown(self):
        """Clean up the temporary database and encryption key."""
        database.reset_connection()
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)
        if os.path.exists(self.key_path):
            os.unlink(self.key_path)
        del os.environ["TLA_DB_FILE"]
        del os.environ["TLA_ENCRYPTION_KEY_FILE"]
    def _generate_random_string(self, length=10):
        """Generates a random string of fixed length."""
        letters = string.ascii_lowercase
        return "".join(random.choice(letters) for i in range(length))
    def _create_random_server(self, owner_id):
        """Creates a server with random data for a given user."""
        server_data = {
            "owner_id": owner_id,
            "alias": self._generate_random_string(),
            "hostname": f"{self._generate_random_string()}.com",
            "user": self._generate_random_string(),
            "password": self._generate_random_string(16),
        }
        database.add_server(**server_data)
        return server_data
    def test_user_backup_restore_cycle(self):
        """Tests a full backup and restore cycle for a single user."""
        user_id = 12345
        database.add_user(user_id)
        database.set_user_language_preference(user_id, "fr")
        original_servers = [self._create_random_server(user_id) for _ in range(random.randint(1, 3))]
        original_key = security._load_key()
        # Backup
        with tempfile.NamedTemporaryFile(delete=False, suffix=".tar.gz") as tf:
            backup_path = Path(tf.name)
        self.backup_manager.backup_user(user_id, backup_path)
        # Purge user data
        database.remove_user(user_id)
        self.assertEqual(len(database.get_all_servers(owner_id=user_id)), 0)
        # Restore
        self.backup_manager.restore_user(user_id, backup_path)
        # Verify
        self.assertEqual(database.get_user_language_preference(user_id), "fr")
        restored_servers = database.get_all_servers(owner_id=user_id)
        self.assertEqual(len(restored_servers), len(original_servers))
        # Sort by alias to ensure consistent comparison
        original_servers.sort(key=lambda x: x['alias'])
        restored_servers.sort(key=lambda x: x['alias'])
        for original, restored in zip(original_servers, restored_servers):
            self.assertEqual(original['alias'], restored['alias'])
            self.assertEqual(original['hostname'], restored['hostname'])
            self.assertEqual(original['user'], restored['user'])
            self.assertEqual(original['password'], restored['password'])
        # Ensure the encryption key was not touched
        self.assertEqual(security._load_key(), original_key)
        os.remove(backup_path)
    def test_system_backup_restore_cycle(self):
        """Tests a full system backup and restore cycle."""
        num_users = random.randint(2, 5)
        original_data = {}
        for i in range(num_users):
            user_id = 1000 + i
            database.add_user(user_id)
            database.set_user_language_preference(user_id, "de")
            original_data[user_id] = {
                "servers": [self._create_random_server(user_id) for _ in range(random.randint(1, 3))],
                "language": "de"
            }
        original_key = security._load_key()
        # Backup
        with tempfile.NamedTemporaryFile(delete=False, suffix=".tar.gz") as tf:
            backup_path = Path(tf.name)
        self.backup_manager.backup_system(backup_path)
        # Delete DB and key
        database.reset_connection()
        os.unlink(self.db_path)
        os.unlink(self.key_path)
        # Restore
        self.backup_manager.restore_system(backup_path)
        # Verify
        self.assertTrue(os.path.exists(self.db_path))
        self.assertTrue(os.path.exists(self.key_path))
        self.assertEqual(security._load_key(), original_key)
        for user_id, data in original_data.items():
            self.assertEqual(database.get_user_language_preference(user_id), data["language"])
            restored_servers = database.get_all_servers(owner_id=user_id)
            self.assertEqual(len(restored_servers), len(data["servers"]))
        os.remove(backup_path)
    def test_backup_integrity_validation(self):
        """Tests that corrupted backups are rejected."""
        user_id = 54321
        database.add_user(user_id)
        self._create_random_server(user_id)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".tar.gz") as tf:
            backup_path = Path(tf.name)
        self.backup_manager.backup_user(user_id, backup_path)
        # Verify valid backup
        self.assertIsNotNone(self.backup_manager.verify_integrity(backup_path))
        # Tamper with the backup
        with open(backup_path, "r+b") as f:
            f.seek(10)
            f.write(b"\x00")
        # Verify tampered backup
        with self.assertRaises(ValueError):
            self.backup_manager.verify_integrity(backup_path)
        os.remove(backup_path)
    def test_permission_isolation(self):
        """Ensures user backups do not affect other users."""
        user1_id = 1111
        user2_id = 2222
        database.add_user(user1_id)
        database.add_user(user2_id)
        user1_servers = [self._create_random_server(user1_id) for _ in range(2)]
        user2_servers = [self._create_random_server(user2_id) for _ in range(2)]
        # Backup user 1
        with tempfile.NamedTemporaryFile(delete=False, suffix=".tar.gz") as tf:
            backup_path = Path(tf.name)
        self.backup_manager.backup_user(user1_id, backup_path)
        # Tamper with user 2's data
        database.remove_server(user2_id, user2_servers[0]['alias'])
        self.assertEqual(len(database.get_all_servers(owner_id=user2_id)), 1)
        # Restore user 1
        self.backup_manager.restore_user(user1_id, backup_path)
        # Verify user 1's data is restored and user 2's data is untouched
        self.assertEqual(len(database.get_all_servers(owner_id=user1_id)), 2)
        self.assertEqual(len(database.get_all_servers(owner_id=user2_id)), 1)
        os.remove(backup_path)
if __name__ == '__main__':
    unittest.main()
