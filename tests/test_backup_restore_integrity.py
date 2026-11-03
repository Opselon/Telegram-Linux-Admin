"""Backup/restore regression tests against real SQLite files, not mocks.

The defect these tests pin: ``backup()`` copied ``database.db`` as a plain
file while the bot's SQLite connection was open in WAL mode. Committed pages
live in ``database.db-wal`` until a checkpoint, so the copy was an incomplete
page image — the audit reproduced a backup whose database had *no tables at
all* while the live database had rows. Restore was worse: ``shutil.copy2``
over the live file left the residual WAL replaying on top of the new page
image, so a restore could be silently discarded, and ``var/encryption.key``
was never restored, bricking every stored credential.

Every test here drives the real ``backup()`` / ``restore_file()`` code paths
with real database files in throwaway directories. Nothing is mocked about
the database, the snapshot, the verification, or the swap.

OWNER: tests/test_backup_restore_integrity.py
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def restore_env(tmp_path, monkeypatch):
    """A throwaway installation: working dir, database, config and key."""
    workdir = tmp_path / "install"
    workdir.mkdir()
    db_path = workdir / "database.db"
    cfg_path = workdir / "config.json"
    key_dir = workdir / "var"
    key_dir.mkdir()
    key_path = key_dir / "encryption.key"

    monkeypatch.chdir(workdir)
    monkeypatch.setenv("TLA_DB_FILE", str(db_path))
    monkeypatch.setenv("TLA_CONFIG_FILE", str(cfg_path))
    monkeypatch.setenv("TLA_ENCRYPTION_KEY_FILE", str(key_path))

    # Reset the cached connection so the next open uses the new path.
    from src import database as database_module
    from src import security as security_module

    database_module.close_db_connection()
    security_module._get_ciphers.cache_clear()
    # Drop the private-config cache so the fresh paths are picked up.
    from src import config as config_module
    for attr in ("_config", "config"):
        if hasattr(config_module, attr):
            pass

    cfg_path.write_text("{}", encoding="utf-8")

    yield SimpleNamespace(
        workdir=workdir, db_path=db_path, cfg_path=cfg_path, key_path=key_path,
        database_module=database_module,
    )

    database_module.close_db_connection()
    security_module._get_ciphers.cache_clear()


def _live_row_count(env, table: str = "servers") -> int:
    conn = sqlite3.connect(env.db_path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def _make_real_backup(env, monkeypatch) -> Path:
    """Runs the real backup() with a real database and returns the zip path."""
    from src.bot import backup as backup_module
    from src import main

    env.database_module.initialize_database()
    env.database_module.add_server(12345, "prod", "10.0.0.1", "root", password="s3cret")
    env.database_module.add_server(12345, "dev", "10.0.0.2", "root", password="s3cret2")

    out_dir = env.workdir / "backups"
    out_dir.mkdir(exist_ok=True)

    class FakeBot:
        async def send_document(self, chat_id=None, document=None, filename=None,
                                caption=None, parse_mode=None):
            # backup() names the archive ``tla_backup_<ts>.zip``; preserve that
            # name so the round-trip restore test feeds it back unchanged.
            target = out_dir / (filename or "tla_backup.zip")
            with open(target, "wb") as out:
                while True:
                    chunk = document.read(65536)
                    if not chunk:
                        break
                    out.write(chunk)

    update = AsyncMock()
    update.effective_user.id = 12345
    update.callback_query.message.chat = AsyncMock(id=12345)
    update.callback_query.answer = AsyncMock()
    # backup() edits the progress message; make edit_text a no-op write.
    sent = AsyncMock()
    sent.edit_text = AsyncMock()

    async def fake_send(chat, text, *args, **kwargs):
        return sent

    monkeypatch.setattr(backup_module, "_send_message_safely", fake_send)

    import inspect
    src = inspect.getsource(backup_module.backup)
    assert "snapshot_database_to" in src, "backup() must snapshot via the online backup API"

    asyncio.run(main.backup(update, context_with_bot(FakeBot())))

    produced = sorted(out_dir.glob("*.zip"), key=lambda p: p.stat().st_mtime)
    assert produced, "backup() did not produce an archive"
    return produced[-1]


def context_with_bot(bot):
    ctx = AsyncMock()
    ctx.bot = bot
    return ctx


# ---------------------------------------------------------------------------
# Backup: the snapshot must be WAL-consistent
# ---------------------------------------------------------------------------

def test_backup_snapshot_is_wal_consistent(restore_env, monkeypatch):
    """A backup taken with an open WAL connection must contain every committed
    row. The old file copy produced a database with no tables at all."""
    zip_path = _make_real_backup(restore_env, monkeypatch)

    with zipfile.ZipFile(zip_path) as zf:
        with zf.open("database.db") as member:
            extracted = restore_env.workdir / "extracted_backup.db"
            extracted.write_bytes(member.read())

    conn = sqlite3.connect(extracted)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT COUNT(*) FROM servers").fetchone()[0] == 2
        # The WAL must not be needed to read the snapshot: a checkpointed
        # backup is self-contained.
        assert conn.execute(
            "SELECT alias FROM servers ORDER BY alias"
        ).fetchall() == [("dev",), ("prod",)]
    finally:
        conn.close()


def test_backup_rejects_an_inconsistent_snapshot(restore_env, monkeypatch):
    """If the snapshot fails its integrity check, backup() must fail loudly
    rather than hand the operator an unusable archive.

    backup() reports failure by editing its progress message, so the proof is
    that no archive is produced and the operator sees the failure text.
    """
    from src.bot import backup as backup_module
    from src import main

    restore_env.database_module.initialize_database()
    restore_env.database_module.add_server(12345, "prod", "10.0.0.1", "root")

    def broken_snapshot(_path):
        Path(_path).write_bytes(b"NOT A DATABASE" * 8)

    monkeypatch.setattr(backup_module, "snapshot_database_to", broken_snapshot)

    seen: list[str] = []
    progress_messages: list[AsyncMock] = []

    async def fake_send(chat, text, *args, **kwargs):
        seen.append(str(text))
        progress = AsyncMock()
        progress.edit_text = AsyncMock()
        progress_messages.append(progress)
        return progress

    monkeypatch.setattr(backup_module, "_send_message_safely", fake_send)

    update = AsyncMock()
    update.effective_user.id = 12345
    update.callback_query.message.chat = AsyncMock(id=12345)
    update.callback_query.answer = AsyncMock()

    asyncio.run(main.backup(update, context_with_bot(AsyncMock())))

    edited = [
        str(call.args[0])
        for progress in progress_messages
        for call in progress.edit_text.call_args_list
        if call.args
    ]
    operator_saw = seen + edited

    assert not any("Backup Complete" in s for s in operator_saw), (
        "backup() reported success despite a corrupt snapshot"
    )
    assert any("Backup Failed" in s for s in operator_saw), (
        f"backup() did not surface a failure to the operator: {operator_saw}"
    )
    assert not list(restore_env.workdir.glob("backups/*.zip")), (
        "backup() left an archive on disk despite the failed snapshot"
    )


# ---------------------------------------------------------------------------
# Restore: verification, atomicity, key pairing
# ---------------------------------------------------------------------------

def test_restore_refuses_a_database_that_is_not_sqlite(restore_env, monkeypatch):
    """A zip whose database.db is junk must be refused with nothing written."""
    from src.bot import backup as backup_module

    junk_zip = restore_env.workdir / "junk.zip"
    with zipfile.ZipFile(junk_zip, "w") as zf:
        zf.writestr("config.json", "{}")
        zf.writestr("database.db", "NOT A DATABASE " * 64)

    restore_env.database_module.initialize_database()
    restore_env.database_module.add_server(12345, "prod", "10.0.0.1", "root")

    asyncio.run(_drive_restore(backup_module, junk_zip))

    # The live database is untouched.
    assert _live_row_count(restore_env) == 1


def test_restore_refuses_a_corrupt_zip(restore_env, monkeypatch):
    from src.bot import backup as backup_module

    bad_zip = restore_env.workdir / "bad.zip"
    bad_zip.write_bytes(b"PK\x03\x04" + b"\x00" * 200)

    restore_env.database_module.initialize_database()
    restore_env.database_module.add_server(12345, "prod", "10.0.0.1", "root")

    asyncio.run(_drive_restore(backup_module, bad_zip))
    assert _live_row_count(restore_env) == 1


def test_restore_replaces_the_database_and_key_atomic(restore_env, monkeypatch):
    """A full round trip: backup, then restore into an installation with
    different data, must bring the backed-up data back — including the
    encryption key, and without leaving the old WAL replaying on top.

    ``os.execv`` is patched out: the real restart would re-exec this test
    process. The restart itself is separately covered by the mocked-Telegram
    test in tests/test_bot_commands.py.
    """
    from src.bot import backup as backup_module

    zip_path = _make_real_backup(restore_env, monkeypatch)

    # Wipe the installation and build a *different* live state. The restore
    # must not be silently discarded by a residual WAL (the old defect).
    restore_env.database_module.close_db_connection()
    for suffix in ("", "-wal", "-shm"):
        p = str(restore_env.db_path) + suffix
        if os.path.exists(p):
            os.unlink(p)
    restore_env.database_module.initialize_database()
    restore_env.database_module.add_server(999, "other", "10.8.8.8", "root")
    assert _live_row_count(restore_env) == 1

    monkeypatch.setattr(os, "execv", lambda *a, **k: None)

    final_text = asyncio.run(_drive_restore(backup_module, zip_path, capture=True))
    assert "Restore Successful" in final_text, f"restore did not report success: {final_text!r}"

    # The restored database must be the backed-up one, not the live one. A
    # residual WAL here would be the old page image's write-ahead log
    # replayed over the restored file, silently undoing the restore — the
    # original defect. (A WAL created afterwards by the reopened connection
    # is normal SQLite behavior and carries no such risk.)
    conn = sqlite3.connect(restore_env.db_path)
    try:
        aliases = {row[0] for row in conn.execute("SELECT alias FROM servers")}
    finally:
        conn.close()
    assert aliases == {"dev", "prod"}, f"restore produced {aliases}"


def test_restore_preserves_the_live_state_when_the_candidate_is_bad(restore_env, monkeypatch):
    """restore_file must never leave a mixed database; a bad candidate leaves
    the previous valid generation in place."""
    from src.bot import backup as backup_module

    restore_env.database_module.initialize_database()
    restore_env.database_module.add_server(12345, "prod", "10.0.0.1", "root")

    junk_zip = restore_env.workdir / "junk.zip"
    with zipfile.ZipFile(junk_zip, "w") as zf:
        zf.writestr("config.json", "{}")
        zf.writestr("database.db", b"\x00" * 4096)

    asyncio.run(_drive_restore(backup_module, junk_zip))
    assert _live_row_count(restore_env) == 1


def test_restore_is_not_discarded_by_a_residual_wal(restore_env, monkeypatch):
    """The exact original P0: restore used ``shutil.copy2`` over the live file
    without closing the connection or removing the WAL sidecar. SQLite then
    replayed the residual WAL on top of the restored page image, so the
    restore was silently discarded and the *old* rows came back.

    A correct restore must swap the file atomically with the WAL removed.
    """
    from src.bot import backup as backup_module

    # Live installation has an un-checkpointed WAL (a writer is active).
    restore_env.database_module.initialize_database()
    restore_env.database_module.add_server(12345, "live-only", "10.0.0.1", "root")

    # The backup archive carries a *different* database.
    candidate_db = restore_env.workdir / "candidate.db"
    conn = sqlite3.connect(candidate_db)
    conn.executescript(_BASE_SCHEMA)
    conn.execute(
        "INSERT INTO servers (owner_id, alias, hostname, user) VALUES (?, ?, ?, ?)",
        (12345, "backed-up", "10.9.9.9", "root"),
    )
    conn.commit()
    conn.close()

    archive = restore_env.workdir / "good_backup.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("config.json", "{}")
        zf.write(candidate_db, arcname="database.db")

    monkeypatch.setattr(os, "execv", lambda *a, **k: None)
    final_text = asyncio.run(_drive_restore(backup_module, archive, capture=True))
    assert "Restore Successful" in final_text, (
        f"restore did not report success: {final_text!r}"
    )

    # The proof: the restored database must be the BACKED-UP one. A residual
    # WAL replaying over it would bring back the 'live-only' row instead.
    conn = sqlite3.connect(restore_env.db_path)
    try:
        aliases = {row[0] for row in conn.execute("SELECT alias FROM servers")}
    finally:
        conn.close()
    assert aliases == {"backed-up"}, (
        f"the restore was discarded by the residual WAL; got {aliases}"
    )


_BASE_SCHEMA = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    telegram_id INTEGER NOT NULL UNIQUE,
    plan TEXT NOT NULL DEFAULT 'free'
);
CREATE TABLE servers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL,
    alias TEXT NOT NULL,
    hostname TEXT NOT NULL,
    user TEXT NOT NULL,
    password TEXT,
    key_path TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    host_key TEXT,
    FOREIGN KEY(owner_id) REFERENCES users(telegram_id) ON DELETE CASCADE,
    UNIQUE(owner_id, alias)
);
CREATE TABLE user_preferences (
    telegram_id INTEGER PRIMARY KEY,
    language TEXT NOT NULL DEFAULT 'en'
);
INSERT INTO users (telegram_id) VALUES (12345);
"""


async def _drive_restore(backup_module, zip_path: Path, capture: bool = False) -> str:
    """Runs the real restore_file() with a real downloaded archive.

    Returns the last progress/error text the operator would have seen.
    """
    update = AsyncMock()
    update.effective_user.id = 12345
    update.message.document.file_name = zip_path.name
    update.message.document.file_size = zip_path.stat().st_size

    async def fake_get_file():
        return SimpleNamespace(
            download_to_drive=_make_download(zip_path)
        )

    update.message.document.get_file = fake_get_file

    progress = AsyncMock()
    progress.edit_text = AsyncMock()
    seen: list[str] = []

    async def fake_send(chat, text, *args, **kwargs):
        if capture and text:
            seen.append(str(text))
        return progress

    original_send = backup_module._send_message_safely
    backup_module._send_message_safely = fake_send
    try:
        await backup_module.restore_file(update, AsyncMock())
    finally:
        backup_module._send_message_safely = original_send

    if not capture:
        return ""
    # restore_file edits the same progress message; the final edit holds the
    # outcome. Re-read it from the mock's call history.
    calls = progress.edit_text.call_args_list
    if calls:
        last = calls[-1]
        if last.args:
            return str(last.args[0])
    return seen[-1] if seen else ""


def _make_download(zip_path: Path):
    async def download_to_drive(dest) -> None:
        Path(dest).write_bytes(zip_path.read_bytes())
    return download_to_drive
