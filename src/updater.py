from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import io
import shlex
import shutil
import subprocess
import sys
import tempfile
import requests
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

try:
    from unittest.mock import Mock  # type: ignore
except ImportError:  # pragma: no cover
    Mock = None  # type: ignore

COMMAND_TIMEOUT = 180
GITHUB_REPO_URL = "https://github.com/Opselon/Telegram-Linux-Admin/archive/refs/heads/main.zip"
REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = REPO_ROOT / "var" / "log"
try:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    fallback_dir = Path.cwd() / "updater-logs"
    fallback_dir.mkdir(parents=True, exist_ok=True)
    LOG_DIR = fallback_dir
if os.name == "posix":
    try:
        os.chmod(LOG_DIR, 0o700)
    except PermissionError:
        pass
LOG_FILE = LOG_DIR / "updater.log"
DB_PATH = REPO_ROOT / "database.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        RotatingFileHandler(LOG_FILE, maxBytes=1_048_576, backupCount=5, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("updater")

def download_and_extract_zip(url: str, destination: Path):
    """Downloads and extracts a zip file to a destination."""
    logger.info(f"Downloading update from {url}...")
    response = requests.get(url, stream=True)
    response.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        # The files are usually inside a single root folder in the zip.
        # We need to strip this top-level directory when extracting.
        for member in zf.infolist():
            # Skip directories
            if member.is_dir():
                continue

            # Path inside the zip file, e.g., "Telegram-Linux-Admin-main/src/main.py"
            zip_path = Path(member.filename)

            # Remove the top-level directory (e.g., "Telegram-Linux-Admin-main/")
            try:
                relative_path = zip_path.relative_to(zip_path.parts[0])
            except ValueError:
                continue

            target_path = destination / relative_path

            # Ensure parent directories exist
            target_path.parent.mkdir(parents=True, exist_ok=True)

            # Extract the file
            with zf.open(member, 'r') as source, open(target_path, 'wb') as target:
                shutil.copyfileobj(source, target)

def apply_update(is_auto: bool = False) -> str:
    update_log: List[str] = []

    def log_message(message: str) -> None:
        logger.info(message)
        if not is_auto:
            update_log.append(message)

    log_message("[1/6] Starting update process.")

    backup_dir = REPO_ROOT / "backup"
    data_files = ["config.json", "database.db"]

    try:
        # Backup the current installation
        if backup_dir.exists():
            shutil.rmtree(backup_dir)
        shutil.copytree(REPO_ROOT, backup_dir, ignore=shutil.ignore_patterns('backup', 'venv'))
        log_message("[2/6] Current installation backed up.")

        # Backup data files
        for file in data_files:
            if (REPO_ROOT / file).exists():
                shutil.copy(REPO_ROOT / file, backup_dir / file)
        log_message("[3/6] Data files backed up.")

        # Download and extract the new version
        download_and_extract_zip(GITHUB_REPO_URL, REPO_ROOT)
        log_message("[4/6] New version downloaded and extracted.")

        # Restore data files
        for file in data_files:
            if (backup_dir / file).exists():
                shutil.copy(backup_dir / file, REPO_ROOT / file)
        log_message("[5/6] Data files restored.")

        # Install dependencies
        subprocess.run([sys.executable, "-m", "pip", "install", "-e", "."], check=True)
        log_message("[6/6] Dependencies installed.")

        # Restart the bot
        subprocess.run(["systemctl", "restart", "telegram_bot.service"], check=True)
        log_message("Bot restarted.")

        log_message("Update process completed!")

    except Exception as e:
        log_message(f"Update Failed: {e}")
        log_message("Attempting to roll back...")
        rollback_entries = rollback(backup_dir)
        update_log.extend(rollback_entries)
    finally:
        if backup_dir.exists():
            shutil.rmtree(backup_dir)

    return "\n".join(update_log)

def rollback(backup_dir: Path) -> List[str]:
    rollback_log: List[str] = ["Rollback sequence initiated."]

    def append(message: str) -> None:
        logger.warning(message)
        rollback_log.append(message)

    try:
        append("Stopping bot service...")
        subprocess.run(["systemctl", "stop", "telegram_bot.service"], check=True)

        shutil.copytree(backup_dir, REPO_ROOT, dirs_exist_ok=True)
        append("Files restored from backup.")

        subprocess.run([sys.executable, "-m", "pip", "install", "-e", "."], check=True)
        append("Dependencies re-installed.")

        append("Restarting bot service...")
        subprocess.run(["systemctl", "start", "telegram_bot.service"], check=True)

        append("Rollback sequence finished.")
    except Exception as e:
        append(f"ERROR: Rollback failed - {e}")

    return rollback_log

def main() -> None:
    parser = argparse.ArgumentParser(description="Secure Telegram Linux Admin Bot Updater")
    parser.add_argument("--auto", action="store_true", help="Run in automated (cron) mode.")
    args = parser.parse_args()

    if args.auto:
        logger.info("Running updater in automated mode.")
        apply_update(is_auto=True)
    else:
        print("--- Secure Bot Updater ---")
        choice = input("Apply update now? (y/N): ").strip().lower()
        if choice == "y":
            print("Starting update...")
            result_log = apply_update(is_auto=False)
            print(result_log)
        else:
            print("Update cancelled.")


if __name__ == "__main__":
    main()
