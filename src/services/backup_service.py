"""Backup service — the archive/restore core extracted from the Telegram flow.

Reuses the WAL-consistent snapshot/verify/replace primitives in
``src.database`` and the zip conventions of the existing bot backup so old
backups stay restorable.  The Telegram bot's chat delivery stays in
``src/bot/backup.py``; this service is the shared data layer for the web API.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import database
from ..audit import audit as _audit


def audit_record(event: str, **fields: object) -> None:
    """Audit one security-relevant event (shared helper for all services)."""
    _audit.record(event, **fields)
from ..config import config as app_config
from ..security import get_primary_key_version
from ..security import _reload_ciphers as reload_ciphers  # noqa: SLF001 - canonical reload
from .permissions import Actor

MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
_BACKUP_DIR = Path(os.environ.get("TLA_BACKUP_DIR", "var/backups"))


class BackupError(RuntimeError):
    """Raised for any backup/restore failure the operator may see."""


def _backup_dir() -> Path:
    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    return _BACKUP_DIR


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class BackupService:
    """Creates and restores platform backups (config + keys + DB snapshot)."""

    _safety_prefix: str = field(default="safety", repr=False)

    # -- create ------------------------------------------------------------
    def create_backup(self, actor: Actor) -> dict[str, Any]:
        actor.require("backups.create")
        _backup_dir()
        backup_id = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(4)
        target = _backup_dir() / f"backup-{backup_id}.zip"
        db_tmp = target.with_suffix(".db.tmp")
        try:
            database.snapshot_database_to(db_tmp)
            metadata = {
                "backup_id": backup_id,
                "created_at": time.time(),
                "created_by": {"account_id": actor.account_id, "kind": actor.kind},
                "key_version": get_primary_key_version(),
                "schema_version": database.SCHEMA_VERSION,
            }
            with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("metadata.json", json.dumps(metadata, indent=2))
                zf.write(db_tmp, "database.db")
                if Path("config.json").exists():
                    zf.write("config.json", "config.json")
                key_file = os.environ.get("TLA_ENCRYPTION_KEY_FILE", "var/encryption.key")
                if Path(key_file).exists():
                    zf.write(key_file, "encryption.key")
                # checksums last, covering everything above
                sums = {name: _sha256_of_zip_entry(zf, name) for name in zf.namelist()}
                zf.writestr("checksums.json", json.dumps(sums, indent=2))
            size = target.stat().st_size
            if size > MAX_ARCHIVE_BYTES:
                target.unlink(missing_ok=True)
                raise BackupError("backup exceeds the archive size limit")
        finally:
            db_tmp.unlink(missing_ok=True)
        audit_record("backup.created", backup_id=backup_id, account_id=actor.account_id)
        return {"backup_id": backup_id, "file": str(target), "size": size,
                "sha256": _sha256(target)}

    # -- list / inspect ------------------------------------------------------
    def list_backups(self, actor: Actor) -> list[dict[str, Any]]:
        actor.require("backups.read")
        backups = []
        for path in sorted(_backup_dir().glob("backup-*.zip"), reverse=True):
            info = self._read_metadata(path)
            backups.append({
                "backup_id": info.get("backup_id") or path.stem.removeprefix("backup-"),
                "created_at": info.get("created_at"),
                "key_version": info.get("key_version"),
                "schema_version": info.get("schema_version"),
                "size": path.stat().st_size,
                "sha256": _sha256(path),
                "path": str(path),
            })
        return backups

    def get_backup(self, actor: Actor, backup_id: str) -> dict[str, Any]:
        actor.require("backups.read")
        path = self._resolve(actor, backup_id)
        info = self._read_metadata(path)
        return {
            "backup_id": info.get("backup_id") or backup_id,
            "created_at": info.get("created_at"),
            "key_version": info.get("key_version"),
            "size": path.stat().st_size,
            "sha256": _sha256(path),
            "path": str(path),
            "verified": self.verify_backup(actor, backup_id)["ok"],
        }

    def _resolve(self, actor: Actor, backup_id: str) -> Path:
        if not backup_id or "/" in backup_id or "\\" in backup_id or ".." in backup_id:
            raise BackupError("invalid backup id")
        path = _backup_dir() / f"backup-{backup_id}.zip"
        if not path.exists():
            raise BackupError("backup not found")
        return path

    def _read_metadata(self, path: Path) -> dict[str, Any]:
        try:
            with zipfile.ZipFile(path) as zf:
                return json.loads(zf.read("metadata.json"))
        except (zipfile.BadZipFile, KeyError, json.JSONDecodeError, OSError):
            return {}

    # -- verify / delete -------------------------------------------------------
    def verify_backup(self, actor: Actor, backup_id: str) -> dict[str, Any]:
        actor.require("backups.read")
        path = self._resolve(actor, backup_id)
        issues: list[str] = []
        try:
            with zipfile.ZipFile(path) as zf:
                sums = json.loads(zf.read("checksums.json"))
                for name, expected in sums.items():
                    try:
                        actual = _sha256_of_zip_entry(zf, name)
                    except KeyError:
                        issues.append(f"missing member: {name}")
                        continue
                    if actual != expected:
                        issues.append(f"checksum mismatch: {name}")
                if not sums:
                    issues.append("no checksums recorded")
                if "database.db" not in zf.namelist():
                    issues.append("no database snapshot inside")
                else:
                    with tempfile.TemporaryDirectory() as tmp:
                        zf.extract("database.db", tmp)
                        if not database.verify_database_file(Path(tmp) / "database.db"):
                            issues.append("database snapshot failed integrity check")
        except zipfile.BadZipFile:
            issues.append("archive is corrupt")
        return {"ok": not issues, "issues": issues}

    def delete_backup(self, actor: Actor, backup_id: str) -> None:
        actor.require("backups.delete")
        path = self._resolve(actor, backup_id)
        path.unlink()
        audit_record("backup.deleted", backup_id=backup_id, account_id=actor.account_id)

    # -- restore ------------------------------------------------------------
    def restore_backup(self, actor: Actor, backup_id: str) -> dict[str, Any]:
        """Restores config + encryption key + database from a verified backup.

        Runs the same atomic-swap sequence as the Telegram flow: safety
        snapshot → config swap → key swap + cipher reload → DB replace →
        post-verify.  The caller (API layer) is responsible for the process
        restart so restored code/config take effect.
        """
        actor.require("backups.restore")
        path = self._resolve(actor, backup_id)
        verification = self.verify_backup(actor, backup_id)
        if not verification["ok"]:
            raise BackupError("backup failed verification: " + "; ".join(verification["issues"]))

        # 1. Safety snapshot of the current state (rollback point).
        safety_id = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(4)
        safety = _backup_dir() / f"{self._safety_prefix}-{safety_id}.zip"
        db_tmp = safety.with_suffix(".db.tmp")
        try:
            database.snapshot_database_to(db_tmp)
            with zipfile.ZipFile(safety, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("metadata.json", json.dumps({
                    "backup_id": f"{self._safety_prefix}-{safety_id}",
                    "created_at": time.time(), "kind": "safety",
                }))
                zf.write(db_tmp, "database.db")
        finally:
            db_tmp.unlink(missing_ok=True)

        # 2. Extract and swap.
        key_file = os.environ.get("TLA_ENCRYPTION_KEY_FILE", "var/encryption.key")
        with tempfile.TemporaryDirectory() as tmp:
            with zipfile.ZipFile(path) as zf:
                zf.extractall(tmp)
            extracted = Path(tmp)
            if (extracted / "config.json").exists():
                os.replace(extracted / "config.json", "config.json")
            if (extracted / "encryption.key").exists():
                os.replace(extracted / "encryption.key", key_file)
                reload_ciphers()
            database.replace_database_with(extracted / "database.db")
        if not database.verify_database_file(os.environ.get("TLA_DB_FILE", database.DEFAULT_DB_FILE)):
            raise BackupError("post-restore verification failed; restore the safety backup")
        audit_record("backup.restored", backup_id=backup_id,
                     safety=safety.name, account_id=actor.account_id)
        return {"ok": True, "backup_id": backup_id, "safety_backup": safety.name,
                "restart_required": True}


def _sha256_of_zip_entry(zf: zipfile.ZipFile, name: str) -> str:
    digest = hashlib.sha256()
    with zf.open(name) as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()
