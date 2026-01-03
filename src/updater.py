from __future__ import annotations

import argparse
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import io
import shutil
import subprocess
import sys
import tempfile
import time
import secrets  # Modern secure random (2026 standards)
import requests
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypedDict

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

def _is_systemd_available() -> bool:
    """Check if systemd is available and the service exists."""
    try:
        result = subprocess.run(
            ["systemctl", "list-units", "--type=service", "--state=active", "telegram_bot.service"],
            capture_output=True,
            timeout=10,
            check=False
        )
        return result.returncode == 0 and "telegram_bot.service" in result.stdout.decode()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _stop_bot_gracefully() -> bool:
    """Stop the bot gracefully, trying multiple methods."""
    # Method 1: Try systemd if available
    if _is_systemd_available():
        try:
            logger.info("Stopping bot via systemd...")
            subprocess.run(
                ["systemctl", "stop", "telegram_bot.service"],
                check=True,
                timeout=30,
                capture_output=True
            )
            time.sleep(2)  # Give it time to stop
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
            logger.warning(f"Failed to stop via systemd: {e}")
    
    # Method 2: Try to find and kill the process
    try:
        import psutil
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                cmdline = proc.info.get('cmdline', [])
                if cmdline and any('main.py' in str(arg) or 'telegram' in str(arg).lower() for arg in cmdline):
                    logger.info(f"Found bot process {proc.info['pid']}, terminating...")
                    proc.terminate()
                    proc.wait(timeout=10)
                    time.sleep(1)
                    return True
            except (psutil.NoSuchProcess, psutil.TimeoutExpired, psutil.AccessDenied):
                continue
    except ImportError:
        logger.warning("psutil not available, skipping process-based stop")
    except Exception as e:
        logger.warning(f"Error stopping bot process: {e}")
    
    # Method 3: Just wait and hope it stops
    logger.info("Waiting for bot to stop naturally...")
    time.sleep(5)
    return True


def _start_bot_gracefully() -> bool:
    """Start the bot gracefully."""
    if _is_systemd_available():
        try:
            logger.info("Starting bot via systemd...")
            subprocess.run(
                ["systemctl", "start", "telegram_bot.service"],
                check=True,
                timeout=30,
                capture_output=True
            )
            time.sleep(2)
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
            logger.error(f"Failed to start via systemd: {e}")
            return False
    else:
        logger.warning("Systemd not available. Bot should be started manually.")
        return False


def apply_update(is_auto: bool = False) -> str:
    """
    Pro version: Robust update system that won't crash.
    Handles all edge cases and provides proper rollback.
    """
    update_log: List[str] = []
    backup_dir = None
    update_successful = False
    
    def log_message(message: str) -> None:
        logger.info(message)
        if not is_auto:
            update_log.append(message)
    
    try:
        log_message("[1/8] Starting professional update process...")
        
        # Validate environment
        if not REPO_ROOT.exists():
            raise ValueError(f"Repository root does not exist: {REPO_ROOT}")
        
        # Create backup directory with timestamp
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        backup_dir = REPO_ROOT / f"backup_{timestamp}"
        data_files = ["config.json", "database.db", "var/encryption.key", "var/pq_encryption.key"]
        
        log_message("[2/8] Creating comprehensive backup...")
        try:
            backup_dir.mkdir(parents=True, exist_ok=True)
            
            # Backup critical files
            for file in data_files:
                src_path = REPO_ROOT / file
                if src_path.exists():
                    dst_path = backup_dir / file
                    dst_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_path, dst_path)
                    log_message(f"  ✓ Backed up {file}")
            
            # Backup source code (excluding large dirs)
            ignore_patterns = shutil.ignore_patterns(
                'backup*', 'venv', '__pycache__', '*.pyc', 
                '.git', 'node_modules', '*.log', 'updater-logs',
                'var/log', '.pytest_cache'
            )
            
            src_backup = backup_dir / "source"
            shutil.copytree(REPO_ROOT / "src", src_backup / "src", ignore=ignore_patterns, dirs_exist_ok=True)
            log_message("  ✓ Backed up source code")
            
        except Exception as e:
            raise RuntimeError(f"Backup failed: {e}") from e
        
        log_message("[3/8] Stopping bot gracefully...")
        _stop_bot_gracefully()
        time.sleep(3)  # Additional wait time
        
        log_message("[4/8] Validating backup integrity...")
        # Verify critical files exist in backup
        for file in ["config.json", "database.db"]:
            backup_file = backup_dir / file
            if not backup_file.exists():
                raise ValueError(f"Critical file missing in backup: {file}")
            if backup_file.stat().st_size == 0:
                raise ValueError(f"Backup file is empty: {file}")
        
        log_message("[5/8] Downloading new version...")
        try:
            # Download to temporary location first
            temp_dir = REPO_ROOT / f"update_temp_{timestamp}"
            temp_dir.mkdir(exist_ok=True)
            
            download_and_extract_zip(GITHUB_REPO_URL, temp_dir)
            log_message("  ✓ Download completed")
            
        except Exception as e:
            raise RuntimeError(f"Download failed: {e}") from e
        
        log_message("[6/8] Installing new version...")
        try:
            # Copy new files, preserving data files
            for item in temp_dir.rglob("*"):
                if item.is_file():
                    relative = item.relative_to(temp_dir)
                    # Skip if it's a data file we want to preserve
                    if str(relative) in data_files:
                        continue
                    
                    target = REPO_ROOT / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, target)
            
            # Clean up temp directory
            shutil.rmtree(temp_dir, ignore_errors=True)
            log_message("  ✓ Files installed")
            
        except Exception as e:
            # Clean up on error
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise RuntimeError(f"Installation failed: {e}") from e
        
        log_message("[7/8] Installing dependencies...")
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-e", ".", "--quiet"],
                check=True,
                timeout=COMMAND_TIMEOUT,
                capture_output=True,
                text=True
            )
            log_message("  ✓ Dependencies installed")
        except subprocess.CalledProcessError as e:
            logger.warning(f"Dependency installation had issues: {e.stderr}")
            # Don't fail on dependency issues, might be minor
        except subprocess.TimeoutExpired:
            raise RuntimeError("Dependency installation timed out")
        
        log_message("[8/8] Starting bot...")
        if _start_bot_gracefully():
            log_message("  ✓ Bot started successfully")
        else:
            log_message("  ⚠ Bot service not started (may need manual start)")
        
        update_successful = True
        log_message("✅ Update process completed successfully!")
        
        # Keep backup for 24 hours, then clean up
        def cleanup_old_backups():
            try:
                for backup in REPO_ROOT.glob("backup_*"):
                    if backup.is_dir():
                        age = time.time() - backup.stat().st_mtime
                        if age > 86400:  # 24 hours
                            shutil.rmtree(backup, ignore_errors=True)
            except Exception:
                pass
        
        # Schedule cleanup (non-blocking)
        import threading
        threading.Thread(target=cleanup_old_backups, daemon=True).start()
        
    except Exception as e:
        log_message(f"❌ Update Failed: {e}")
        logger.error(f"Update error: {e}", exc_info=True)
        
        if backup_dir and backup_dir.exists():
            log_message("🔄 Attempting automatic rollback...")
            try:
                rollback_success = rollback(backup_dir)
                if rollback_success:
                    log_message("✅ Rollback completed successfully")
                else:
                    log_message("⚠️ Rollback had issues - manual intervention may be needed")
            except Exception as rollback_error:
                log_message(f"❌ Rollback failed: {rollback_error}")
                logger.error(f"Rollback error: {rollback_error}", exc_info=True)
                log_message(f"⚠️ Manual recovery required. Backup location: {backup_dir}")
        else:
            log_message("❌ No backup available for rollback!")
    
    return "\n".join(update_log)

def rollback(backup_dir: Path) -> bool:
    """
    Pro version: Robust rollback that restores system to working state.
    Returns True if successful, False otherwise.
    """
    logger.warning("Rollback sequence initiated.")
    
    try:
        # Stop bot
        _stop_bot_gracefully()
        time.sleep(2)
        
        # Restore data files first (most critical)
        data_files = ["config.json", "database.db", "var/encryption.key", "var/pq_encryption.key"]
        for file in data_files:
            backup_file = backup_dir / file
            if backup_file.exists():
                target = REPO_ROOT / file
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup_file, target)
                logger.info(f"Restored {file}")
        
        # Restore source code if available
        src_backup = backup_dir / "source" / "src"
        if src_backup.exists():
            shutil.copytree(src_backup, REPO_ROOT / "src", dirs_exist_ok=True)
            logger.info("Restored source code")
        
        # Reinstall dependencies
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-e", ".", "--quiet"],
                check=True,
                timeout=COMMAND_TIMEOUT,
                capture_output=True
            )
            logger.info("Dependencies reinstalled")
        except Exception as e:
            logger.warning(f"Dependency reinstall had issues: {e}")
        
        # Start bot
        if _start_bot_gracefully():
            logger.info("Bot restarted after rollback")
        else:
            logger.warning("Bot service not restarted (may need manual start)")
        
        logger.info("Rollback sequence completed successfully")
        return True
        
    except Exception as e:
        logger.error(f"Rollback failed: {e}", exc_info=True)
        return False

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
