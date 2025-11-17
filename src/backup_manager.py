"""Backup and restore manager for the Telegram Linux Admin application."""
from __future__ import annotations
import os
import json
import tarfile
import hashlib
import tempfile
import io
import shutil
from datetime import datetime, timezone
from pathlib import Path
from . import database
from .security import _get_key_path

SYSTEM_BACKUP_DB_NAME = "database.sqlite"
SYSTEM_BACKUP_KEY_NAME = "encryption.key"
SYSTEM_BACKUP_METADATA = "metadata.json"

class BackupManager:
    """Manages user- and system-level backups."""

    def backup_user(self, user_id: int, backup_path: Path | str) -> None:
        """
        Creates a compressed backup for a specific user.
        The backup includes servers and preferences, but not the encryption key.
        """
        servers = database.get_all_servers(owner_id=user_id)
        language = database.get_user_language_preference(user_id)
        user_data = {
            "user_id": user_id,
            "language": language or "en",
        }

        def default_serializer(o):
            if isinstance(o, datetime):
                return o.isoformat()
            raise TypeError(f"Object of type {o.__class__.__name__} is not JSON serializable")

        backup_content = {
            "user.json": json.dumps(user_data, indent=2),
            "servers.json": json.dumps(servers, indent=2, default=default_serializer),
        }
        self._create_backup_archive("user", backup_content, backup_path)

    def restore_user(self, user_id: int, backup_file: Path | str) -> None:
        """
        Restores a user's data from a backup.
        This operation is destructive and replaces existing data for the user.
        """
        metadata = self.verify_integrity(backup_file)
        if metadata["type"] != "user":
            raise ValueError("Invalid backup type for user restore.")

        with tarfile.open(backup_file, "r:gz") as tar:
            user_data = json.loads(self._extract_from_tar(tar, "user.json"))
            servers = json.loads(self._extract_from_tar(tar, "servers.json"))

        # Clear existing user data
        database.remove_user(user_id)

        # Restore user data
        database.set_user_language_preference(user_id, user_data.get("language", "en"))
        for server in servers:
            database.add_server(
                owner_id=user_id,
                alias=server["alias"],
                hostname=server["hostname"],
                user=server["user"],
                password=server.get("password"),
                key_path=server.get("key_path"),
            )

    def backup_system(self, backup_path: Path | str) -> None:
        """
        Creates a full system backup including the database and encryption key.
        This treats the database as an opaque snapshot.
        """
        # Ensure WAL is flushed by checkpointing the connection before backup
        database.checkpoint()

        db_path = database.get_db_path()
        key_path = _get_key_path()

        files_to_backup = {}
        if os.path.exists(db_path):
            with open(db_path, "rb") as f:
                files_to_backup[SYSTEM_BACKUP_DB_NAME] = f.read()

        if os.path.exists(key_path):
            with open(key_path, "rb") as f:
                files_to_backup[SYSTEM_BACKUP_KEY_NAME] = f.read()

        self._create_backup_archive("system", files_to_backup, backup_path)

    def restore_system(self, archive_path: Path | str) -> None:
        """
        Restores a full system backup from a snapshot.
        - Replaces the SQLite database file
        - Restores the encryption key file
        - Clears DB / cipher caches so new operations see the restored data
        """
        archive_path = Path(archive_path)
        if not archive_path.is_file():
            raise FileNotFoundError(f"Backup archive not found: {archive_path}")

        with tempfile.TemporaryDirectory(prefix="tla-restore-") as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)

            # 1) Extract backup archive
            with tarfile.open(archive_path, mode="r:gz") as tf:
                tf.extractall(tmp_dir)

            # 2) Validate metadata
            meta_path = tmp_dir / SYSTEM_BACKUP_METADATA
            if not meta_path.is_file():
                raise RuntimeError("Invalid backup: missing metadata.json")
            with meta_path.open("r", encoding="utf-8") as f:
                meta = json.load(f)
            if meta.get("type") != "system":
                raise RuntimeError(f"Invalid backup type: {meta.get('type')!r}, expected 'system'")

            # 3) Restore DB snapshot
            db_src = tmp_dir / SYSTEM_BACKUP_DB_NAME
            if not db_src.is_file():
                raise RuntimeError("Invalid backup: missing database.sqlite")

            db_dest = database.get_db_path()

            # IMPORTANT: Close cached DB connection BEFORE overwriting the file
            database.reset_connection()

            db_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(db_src, db_dest)

            # 4) Restore encryption key file
            key_src = tmp_dir / SYSTEM_BACKUP_KEY_NAME
            if key_src.is_file():
                key_dest = _get_key_path()
                key_dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(key_src, key_dest)

    def verify_integrity(self, backup_archive: Path | str) -> dict:
        """
        Verifies the integrity of a backup archive.
        Returns the metadata if the backup is valid, otherwise raises an exception.
        """
        try:
            with tarfile.open(backup_archive, "r:gz") as tar:
                integrity_hash = self._extract_from_tar(tar, "integrity.hash").decode("utf-8").strip()
                metadata_bytes = self._extract_from_tar(tar, "metadata.json")

                if self._calculate_hash(metadata_bytes) != integrity_hash:
                    raise ValueError("Backup integrity check failed: metadata corrupted.")

                metadata = json.loads(metadata_bytes)
                manifest = metadata.get("manifest", {})
                for name, expected_hash in manifest.items():
                    file_bytes = self._extract_from_tar(tar, name)
                    if self._calculate_hash(file_bytes) != expected_hash:
                        raise ValueError(f"Backup integrity check failed: {name} corrupted.")

                return metadata
        except (tarfile.ReadError, KeyError, EOFError) as e:
            raise ValueError("Backup integrity check failed: archive is corrupted or invalid.") from e

    def _create_backup_archive(self, backup_type: str, content: dict[str, str | bytes], backup_path: Path | str):
        """
        Creates a compressed tarball with the provided content.
        Includes metadata and an integrity hash.
        """
        manifest = {}
        with tarfile.open(backup_path, "w:gz") as tar:
            for name, data in content.items():
                self._add_file_to_tar(tar, name, data)
                manifest[name] = self._calculate_hash(data)

            metadata = {
                "schema_version": 1,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "type": backup_type,
                "manifest": manifest,
            }
            metadata_bytes = json.dumps(metadata, indent=2).encode("utf-8")
            self._add_file_to_tar(tar, "metadata.json", metadata_bytes)

            integrity_hash = self._calculate_hash(metadata_bytes)
            self._add_file_to_tar(tar, "integrity.hash", integrity_hash)

    def _add_file_to_tar(self, tar: tarfile.TarFile, name: str, data: str | bytes):
        """Adds a file to a tar archive."""
        if isinstance(data, str):
            data = data.encode("utf-8")
        tarinfo = tarfile.TarInfo(name=name)
        tarinfo.size = len(data)
        tarinfo.mtime = int(datetime.now(timezone.utc).timestamp())
        tar.addfile(tarinfo, fileobj=io.BytesIO(data))

    def _extract_from_tar(self, tar: tarfile.TarFile, name: str) -> bytes:
        """Extracts a file from a tar archive."""
        member = tar.getmember(name)
        file_obj = tar.extractfile(member)
        if file_obj:
            return file_obj.read()
        raise KeyError(f"Member '{name}' not found in tar archive.")


    def _calculate_hash(self, data: str | bytes) -> str:
        """Calculates the SHA256 hash of a file."""
        if isinstance(data, str):
            data = data.encode("utf-8")
        return hashlib.sha256(data).hexdigest()
