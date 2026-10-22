"""Purpose: Admin backup and restore flows.

OWNER: src/bot/<this module>.
CONSUMES: src/bot/state.py (mutable singletons, conversation states,
  leaf helpers) and the sibling handler modules listed below.
PROVIDES: python-telegram-bot handlers registered by
  src/bot/application.py.
INVARIANTS: handler logic and user-visible behavior are unchanged from
  3.1; only the import graph was made explicit. Mutable runtime state
  is read/written through ``state`` so a patched singleton is seen here.
EXTEND: add a handler in this module, then register it in
  src/bot/application.py.
"""

from __future__ import annotations

from . import state
from .state import (
    AWAIT_RESTORE_CONFIRMATION, AWAIT_RESTORE_FILE,
)

import logging

from telegram import InlineKeyboardButton
from telegram import InlineKeyboardMarkup
from telegram import Update
from telegram.ext import ContextTypes
from telegram.ext import ConversationHandler

from ..parse_mode import MessageBuilder
from datetime import datetime
import asyncio
import hashlib
import json
import os
import shutil
import sys
import tempfile
import zipfile

from .support import _edit_message_safely, _send_message_safely

logger = logging.getLogger(__name__)


async def backup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Pro version: Creates a comprehensive backup with validation, metadata, and progress indication.
    Maximum speed and reliability.
    """
    query = update.callback_query
    user_id = update.effective_user.id
    parse_mode = state._get_user_parse_mode(user_id)

    await query.answer()

    # Show progress message
    progress_msg = await _send_message_safely(query.message.chat, "🔄 **Creating backup...**", user_id, preformatted=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = tempfile.mkdtemp(prefix="tla_backup_")
    backup_filename = os.path.join(backup_dir, f"tla_backup_{timestamp}.zip")
    
    files_to_backup = [
        "config.json",
        "database.db",
        "var/encryption.key",
        "var/pq_encryption.key"
    ]
    backup_metadata = {
        "timestamp": timestamp,
        "version": "3.0",
        "bot_version": "Professional",
        "files": {},
        "checksums": {},
        "system_info": {
            "platform": sys.platform,
            "python_version": sys.version.split()[0]
        }
    }
    
    try:
        # Update progress
        builder = MessageBuilder(parse_mode)
        builder.add_text("📦 **Backup in Progress**")
        builder.add_line()
        builder.add_text("Validating files...")
        await progress_msg.edit_text(builder.build(), parse_mode=parse_mode)
        
        # Validate and collect files with metadata
        validated_files = []
        total_size = 0
        max_backup_size = 100 * 1024 * 1024  # 100MB limit
        critical_files = ["config.json", "database.db"]
        optional_files = ["var/encryption.key", "var/pq_encryption.key"]
        
        # Check critical files first
        for file_path in critical_files:
            if not os.path.exists(file_path):
                raise ValueError(f"Critical file missing: {file_path}")
        
        # Process all files
        for file_path in files_to_backup:
            if not os.path.exists(file_path):
                if file_path in optional_files:
                    logger.warning(f"Optional file {file_path} not found - skipping")
                    continue
                else:
                    raise ValueError(f"Required file missing: {file_path}")
            
            file_size = os.path.getsize(file_path)
            if file_size == 0:
                logger.warning(f"File {file_path} is empty - skipping")
                continue
            
            if file_size > max_backup_size:
                raise ValueError(f"File {file_path} is too large ({file_size} bytes). Maximum: {max_backup_size} bytes")
            
            total_size += file_size
            if total_size > max_backup_size:
                raise ValueError(f"Total backup size exceeds limit ({total_size} bytes). Maximum: {max_backup_size} bytes")
            
            # Calculate checksum for integrity verification (chunked for large files)
            file_hash = hashlib.sha256()
            with open(file_path, 'rb') as f:
                while chunk := f.read(8192):
                    file_hash.update(chunk)
            file_hash_hex = file_hash.hexdigest()
            
            backup_metadata["files"][file_path] = {
                "size": file_size,
                "modified": os.path.getmtime(file_path),
                "is_critical": file_path in critical_files
            }
            backup_metadata["checksums"][file_path] = file_hash_hex
            validated_files.append((file_path, file_hash_hex))
        
        if not validated_files:
            raise ValueError("No valid files found to backup")
        
        # Add summary to metadata
        backup_metadata["summary"] = {
            "total_files": len(validated_files),
            "total_size_bytes": total_size,
            "total_size_mb": round(total_size / (1024 * 1024), 2),
            "critical_files_count": sum(1 for f in validated_files if f[0] in critical_files)
        }
        
        # Update progress
        builder.clear()
        builder.add_text("📦 **Backup in Progress**")
        builder.add_line()
        builder.add_text(f"Compressing {len(validated_files)} file(s)...")
        await progress_msg.edit_text(builder.build(), parse_mode=parse_mode)
        
        # Create compressed backup with maximum compression
        with zipfile.ZipFile(backup_filename, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as zipf:
            for file_path, _file_hash in validated_files:
                zipf.write(file_path, arcname=os.path.basename(file_path))
            
            # Add metadata as JSON
            metadata_json = json.dumps(backup_metadata, indent=2)
            zipf.writestr("backup_metadata.json", metadata_json)
        
        # Verify backup integrity
        backup_size = os.path.getsize(backup_filename)
        if backup_size == 0:
            raise ValueError("Backup file is empty")
        
        # Update progress
        builder.clear()
        builder.add_text("📦 **Backup in Progress**")
        builder.add_line()
        builder.add_text("Verifying backup integrity...")
        await progress_msg.edit_text(builder.build(), parse_mode=parse_mode)
        
        # Verify ZIP file integrity
        with zipfile.ZipFile(backup_filename, 'r') as zipf:
            if zipf.testzip() is not None:
                raise ValueError("Backup file integrity check failed")
        
        # Update progress
        builder.clear()
        builder.add_text("📦 **Backup in Progress**")
        builder.add_line()
        builder.add_text("Uploading backup...")
        await progress_msg.edit_text(builder.build(), parse_mode=parse_mode)
        
        # Send backup file
        with open(backup_filename, 'rb') as backup_file:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=backup_file,
                filename=f"tla_backup_{timestamp}.zip",
                caption=f"✅ Backup created successfully\n📅 {timestamp}\n📦 {len(validated_files)} file(s)\n💾 {backup_size / 1024:.2f} KB",
                parse_mode=parse_mode
            )
        
        # Success message
        keyboard = [[InlineKeyboardButton("🔙 Back to Main Menu", callback_data='main_menu')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        builder.clear()
        builder.add_text("✅ ")
        builder.add_bold("Backup Complete!")
        builder.add_line()
        builder.add_line()
        builder.add_text(f"📅 Created: {timestamp}")
        builder.add_line()
        builder.add_text(f"📦 Files: {len(validated_files)}")
        builder.add_line()
        builder.add_text(f"💾 Size: {backup_size / 1024:.2f} KB")
        builder.add_line()
        builder.add_text("🔒 Integrity: Verified")
        
        await progress_msg.edit_text(builder.build(), reply_markup=reply_markup, parse_mode=parse_mode)
        logger.info(f"Backup created successfully: {backup_filename} ({backup_size} bytes)")

    except Exception as e:
        logger.error(f"Error creating backup: {e}", exc_info=True)
        builder = MessageBuilder(parse_mode)
        builder.add_text("❌ ")
        builder.add_bold("Backup Failed")
        builder.add_line()
        builder.add_line()
        builder.add_text("Error: ")
        builder.add_code(str(e))
        await progress_msg.edit_text(builder.build(), parse_mode=parse_mode)
    finally:
        # Cleanup
        try:
            if os.path.exists(backup_filename):
                os.remove(backup_filename)
            if os.path.exists(backup_dir):
                shutil.rmtree(backup_dir, ignore_errors=True)
        except Exception as cleanup_error:
            logger.warning(f"Error during backup cleanup: {cleanup_error}")

async def restore_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Pro version: Starts the restore process with safety warnings."""
    query = update.callback_query
    user_id = update.effective_user.id
    parse_mode = state._get_user_parse_mode(user_id)
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("⚠️ Yes, I'm sure", callback_data='restore_yes')],
        [InlineKeyboardButton("❌ Cancel", callback_data='restore_no')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    builder = MessageBuilder(parse_mode)
    builder.add_text("⚠️ ")
    builder.add_bold("DANGER ZONE: RESTORE")
    builder.add_line()
    builder.add_line()
    builder.add_text("Restoring from a backup will:")
    builder.add_line()
    builder.add_text("• Overwrite your current configuration")
    builder.add_line()
    builder.add_text("• Overwrite your current database")
    builder.add_line()
    builder.add_text("• Replace all server settings")
    builder.add_line()
    builder.add_text("⚠️ This action is ")
    builder.add_bold("IRREVERSIBLE")
    builder.add_text("!")
    builder.add_line()
    builder.add_line()
    builder.add_text("A backup of your current state will be created automatically.")
    builder.add_line()
    builder.add_text("Are you sure you want to continue?")
    
    await _send_message_safely(query.message.chat, builder.build(), user_id, reply_markup=reply_markup, preformatted=True)
    return AWAIT_RESTORE_CONFIRMATION

async def restore_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Pro version: Confirms the restore and asks for the backup file."""
    query = update.callback_query
    user_id = update.effective_user.id
    parse_mode = state._get_user_parse_mode(user_id)
    await query.answer()

    if query.data == 'restore_yes':
        builder = MessageBuilder(parse_mode)
        builder.add_text("📤 ")
        builder.add_bold("Ready to Restore")
        builder.add_line()
        builder.add_line()
        builder.add_text("Please upload the backup file (")
        builder.add_code(".zip")
        builder.add_text(").")
        builder.add_line()
        builder.add_line()
        builder.add_text("The file will be validated before restore.")
        await _edit_message_safely(query.message, builder.build(), user_id, preformatted=True)
        return AWAIT_RESTORE_FILE
    else:
        builder = MessageBuilder(parse_mode)
        builder.add_text("❌ Restore cancelled.")
        await _edit_message_safely(query.message, builder.build(), user_id, preformatted=True)
        return ConversationHandler.END

async def restore_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Pro version: Receives the backup file, validates it thoroughly, creates safety backup,
    and performs the restore with rollback capability.
    Maximum speed and reliability.
    """
    user_id = update.effective_user.id
    parse_mode = state._get_user_parse_mode(user_id)
    
    document = update.message.document
    
    # Validate file extension
    if not document.file_name or not document.file_name.lower().endswith('.zip'):
        builder = MessageBuilder(parse_mode)
        builder.add_text("❌ ")
        builder.add_bold("Invalid File Format")
        builder.add_line()
        builder.add_line()
        builder.add_text("Please upload a ")
        builder.add_code(".zip")
        builder.add_text(" file.")
        await _send_message_safely(update.message.chat, builder.build(), user_id, preformatted=True)
        return AWAIT_RESTORE_FILE
    
    # Check file size (max 100MB)
    max_file_size = 100 * 1024 * 1024
    if document.file_size and document.file_size > max_file_size:
        builder = MessageBuilder(parse_mode)
        builder.add_text("❌ ")
        builder.add_bold("File Too Large")
        builder.add_line()
        builder.add_line()
        builder.add_text(f"File size ({document.file_size / 1024 / 1024:.2f} MB) exceeds maximum (100 MB).")
        await _send_message_safely(update.message.chat, builder.build(), user_id, preformatted=True)
        return AWAIT_RESTORE_FILE
    
    # Show progress
    progress_msg = await _send_message_safely(update.message.chat, "🔄 **Starting restore process...**", user_id, preformatted=True)
    
    backup_dir = tempfile.mkdtemp(prefix="tla_restore_")
    downloaded_file = os.path.join(backup_dir, document.file_name)
    safety_backup_dir = os.path.join(backup_dir, "safety_backup")
    os.makedirs(safety_backup_dir, exist_ok=True)
    
    try:
        # Step 1: Download file
        builder = MessageBuilder(parse_mode)
        builder.add_text("🔄 **Restore Progress**")
        builder.add_line()
        builder.add_text("📥 Downloading backup file...")
        await progress_msg.edit_text(builder.build(), parse_mode=parse_mode)
        
        # Download file with proper error handling
        backup_file = await document.get_file()
        try:
            await backup_file.download_to_drive(downloaded_file)
        except Exception as download_error:
            raise ValueError(f"Failed to download backup file: {download_error}") from download_error
        
        if not os.path.exists(downloaded_file) or os.path.getsize(downloaded_file) == 0:
            raise ValueError("Downloaded file is empty or corrupted")
        
        # Step 2: Validate ZIP file
        builder.clear()
        builder.add_text("🔄 **Restore Progress**")
        builder.add_line()
        builder.add_text("🔍 Validating backup file...")
        await progress_msg.edit_text(builder.build(), parse_mode=parse_mode)
        
        try:
            with zipfile.ZipFile(downloaded_file, 'r') as zipf:
                # Test ZIP integrity
                bad_file = zipf.testzip()
                if bad_file:
                    raise ValueError(f"Backup file is corrupted. Bad file: {bad_file}")
                
                # Check for required files
                namelist = zipf.namelist()
                required_files = ["config.json", "database.db"]
                missing_files = [f for f in required_files if f not in namelist]
                
                if missing_files:
                    raise ValueError(f"Backup file is missing required files: {', '.join(missing_files)}")
                
                # Check for metadata
                has_metadata = "backup_metadata.json" in namelist
                metadata = None
                if has_metadata:
                    try:
                        metadata_json = zipf.read("backup_metadata.json").decode('utf-8')
                        metadata = json.loads(metadata_json)
                    except Exception as e:
                        logger.warning(f"Could not read backup metadata: {e}")
        except zipfile.BadZipFile:
            raise ValueError("Invalid ZIP file format") from None
        
        # Step 3: Create safety backup of current state
        builder.clear()
        builder.add_text("🔄 **Restore Progress**")
        builder.add_line()
        builder.add_text("💾 Creating safety backup of current state...")
        await progress_msg.edit_text(builder.build(), parse_mode=parse_mode)
        
        safety_backup_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safety_backup_file = os.path.join(safety_backup_dir, f"safety_backup_{safety_backup_timestamp}.zip")
        
        current_files = ["config.json", "database.db"]
        with zipfile.ZipFile(safety_backup_file, 'w', zipfile.ZIP_DEFLATED) as safety_zip:
            for file_path in current_files:
                if os.path.exists(file_path):
                    safety_zip.write(file_path, arcname=os.path.basename(file_path))
        
        # Step 4: Extract backup files
        builder.clear()
        builder.add_text("🔄 **Restore Progress**")
        builder.add_line()
        builder.add_text("📦 Extracting backup files...")
        await progress_msg.edit_text(builder.build(), parse_mode=parse_mode)
        
        extract_dir = os.path.join(backup_dir, "extracted")
        os.makedirs(extract_dir, exist_ok=True)
        
        with zipfile.ZipFile(downloaded_file, 'r') as zipf:
            zipf.extractall(extract_dir)
        
        # Step 5: Verify extracted files
        builder.clear()
        builder.add_text("🔄 **Restore Progress**")
        builder.add_line()
        builder.add_text("✅ Verifying extracted files...")
        await progress_msg.edit_text(builder.build(), parse_mode=parse_mode)
        
        extracted_config = os.path.join(extract_dir, "config.json")
        extracted_db = os.path.join(extract_dir, "database.db")
        
        if not os.path.exists(extracted_config):
            raise ValueError("Extracted config.json not found")
        if not os.path.exists(extracted_db):
            raise ValueError("Extracted database.db not found")
        
        # Validate JSON structure
        try:
            with open(extracted_config, 'r', encoding='utf-8') as f:
                json.load(f)  # Validate JSON
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in config.json: {e}") from e
        
        # Step 6: Backup current files and restore
        builder.clear()
        builder.add_text("🔄 **Restore Progress**")
        builder.add_line()
        builder.add_text("🔄 Restoring files...")
        await progress_msg.edit_text(builder.build(), parse_mode=parse_mode)
        
        # Backup and replace files atomically
        files_to_restore = [
            ("config.json", extracted_config),
            ("database.db", extracted_db)
        ]
        
        for target_file, source_file in files_to_restore:
            # Backup current file if exists
            if os.path.exists(target_file):
                backup_path = os.path.join(safety_backup_dir, os.path.basename(target_file))
                shutil.copy2(target_file, backup_path)
            
            # Copy new file
            shutil.copy2(source_file, target_file)
            os.chmod(target_file, 0o600)  # Secure permissions
        
        # Step 7: Verify restore
        builder.clear()
        builder.add_text("🔄 **Restore Progress**")
        builder.add_line()
        builder.add_text("✅ Verifying restore...")
        await progress_msg.edit_text(builder.build(), parse_mode=parse_mode)
        
        # Verify files exist and are valid
        for target_file, _ in files_to_restore:
            if not os.path.exists(target_file):
                raise ValueError(f"Restored file {target_file} not found after restore")
            if os.path.getsize(target_file) == 0:
                raise ValueError(f"Restored file {target_file} is empty")
        
        # Success message
        builder.clear()
        builder.add_text("✅ ")
        builder.add_bold("Restore Successful!")
        builder.add_line()
        builder.add_line()
        if metadata:
            builder.add_text(f"📅 Backup Date: {metadata.get('timestamp', 'Unknown')}")
            builder.add_line()
        builder.add_text("✅ Files restored successfully")
        builder.add_line()
        builder.add_text("💾 Safety backup created")
        builder.add_line()
        builder.add_line()
        builder.add_text("🔄 The bot will restart to apply changes...")
        
        await progress_msg.edit_text(builder.build(), parse_mode=parse_mode)
        
        logger.info(f"Restore completed successfully. Safety backup: {safety_backup_file}")
        
        # Restart bot gracefully
        # Give time for message to be sent (modern async pattern)
        try:
            async with asyncio.timeout(2.0):
                await asyncio.sleep(2.0)
        except TimeoutError:
            pass
        
        # Use proper restart mechanism
        python = sys.executable
        os.execv(python, [python] + sys.argv)
        
    except Exception as e:
        logger.error(f"Error during restore: {e}", exc_info=True)
        
        # Try to restore from safety backup if available
        safety_backup_files = []
        if os.path.exists(safety_backup_dir):
            for file in os.listdir(safety_backup_dir):
                if file.endswith('.zip'):
                    safety_backup_files.append(os.path.join(safety_backup_dir, file))
        
        if safety_backup_files:
            try:
                logger.info("Attempting to restore from safety backup...")
                latest_backup = max(safety_backup_files, key=os.path.getmtime)
                with zipfile.ZipFile(latest_backup, 'r') as safety_zip:
                    safety_zip.extractall()
                logger.info("Safety backup restored successfully")
            except Exception as rollback_error:
                logger.error(f"Failed to restore from safety backup: {rollback_error}", exc_info=True)
        
        builder = MessageBuilder(parse_mode)
        builder.add_text("❌ ")
        builder.add_bold("Restore Failed")
        builder.add_line()
        builder.add_line()
        builder.add_text("Error: ")
        builder.add_code(str(e))
        builder.add_line()
        builder.add_line()
        if safety_backup_files:
            builder.add_text("⚠️ Attempted to restore from safety backup.")
        
        await progress_msg.edit_text(builder.build(), parse_mode=parse_mode)
        
    finally:
        # Cleanup temporary files (with delay to ensure files are closed)
        try:
            # Cleanup delay with timeout protection
            try:
                async with asyncio.timeout(1.0):
                    await asyncio.sleep(1.0)
            except TimeoutError:
                pass
            if os.path.exists(backup_dir):
                shutil.rmtree(backup_dir, ignore_errors=True)
        except Exception as cleanup_error:
            logger.warning(f"Error during restore cleanup: {cleanup_error}")

    return ConversationHandler.END

async def cancel_restore(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Pro version: Cancels the restore conversation."""
    user_id = update.effective_user.id
    
    builder = MessageBuilder(state._get_user_parse_mode(user_id))
    builder.add_text("❌ Restore cancelled.")
    await _send_message_safely(update.message.chat, builder.build(), user_id, preformatted=True)
    return ConversationHandler.END
