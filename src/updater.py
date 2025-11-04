from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

try:
    from unittest.mock import Mock  # type: ignore
except ImportError:  # pragma: no cover
    Mock = None  # type: ignore

COMMAND_TIMEOUT = 180
MANIFEST_SECRET_ENV = "UPDATER_MANIFEST_KEY"
REPO_ROOT = Path(__file__).resolve().parent.parent
SECURITY_DIR = REPO_ROOT / "security"
MANIFEST_PATH = SECURITY_DIR / "update_manifest.json"
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

_VALID_COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")
_REMOTE_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9/._-]+$")
_SERVICE_UNIT_RE = re.compile(r"^[A-Za-z0-9_.@-]+\.service$")


class SecurityError(RuntimeError):
    """Raised when a security policy check fails."""


class CommandRejected(SecurityError):
    """Raised when a command does not match the execution policy."""


@dataclass(frozen=True)
class CommandResult:
    stdout: str
    stderr: str
    returncode: int


def _coerce_command_result(result: object) -> CommandResult:
    if isinstance(result, CommandResult):
        return result
    if isinstance(result, subprocess.CompletedProcess):
        return CommandResult(
            stdout=result.stdout or "",
            stderr=result.stderr or "",
            returncode=result.returncode,
        )
    if isinstance(result, dict):
        stdout = result.get("stdout", "")
        stderr = result.get("stderr", "")
        return CommandResult(
            stdout="" if stdout is None else str(stdout),
            stderr="" if stderr is None else str(stderr),
            returncode=int(result.get("returncode", 0)),
        )
    raise TypeError(f"Unsupported command result type: {type(result)!r}")


def compute_sha256(path: Path) -> str:
    """Return the hex encoded SHA-256 digest for a file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sanitised_environment() -> Dict[str, str]:
    """Create a curated environment for subprocess execution."""
    allowed = {
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "HOME",
        "USER",
        "USERNAME",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
    }
    env = {}
    for key, value in os.environ.items():
        if key in allowed or key.startswith("GIT_") or key.startswith("PIP_"):
            env[key] = value
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _format_command(arguments: List[str]) -> str:
    return " ".join(shlex.quote(arg) for arg in arguments)


@dataclass(frozen=True)
class SecurityManifest:
    schema_version: str
    allowed_remote: str
    allowed_remote_url: str
    allowed_branch: str
    requirements_lock: Path
    requirements_lock_sha256: str
    service_unit: Optional[str] = None
    service_scope: str = "system"

    @classmethod
    def load(cls, path: Path, secret: Optional[str]) -> "SecurityManifest":
        if not path.exists():
            raise SecurityError(
                f"Security manifest not found at {path}. Updates are blocked until it exists."
            )

        data = json.loads(path.read_text(encoding="utf-8"))
        signature = data.pop("signature", None)
        if signature is None:
            raise SecurityError("Security manifest is missing a signature field.")

        canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
        if not secret:
            raise SecurityError(
                "Environment variable UPDATER_MANIFEST_KEY is not set; cannot validate manifest."
            )

        expected_signature = hmac.new(
            secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected_signature):
            raise SecurityError("Security manifest signature verification failed.")

        allowed_remote = data.get("allowed_remote")
        if not allowed_remote or not _REMOTE_RE.fullmatch(allowed_remote):
            raise SecurityError("Manifest contains an invalid allowed_remote value.")

        allowed_branch = data.get("allowed_branch")
        if not allowed_branch or not _BRANCH_RE.fullmatch(allowed_branch):
            raise SecurityError("Manifest contains an invalid allowed_branch value.")

        allowed_remote_url = data.get("allowed_remote_url")
        if not allowed_remote_url:
            raise SecurityError("Manifest missing allowed_remote_url value.")

        requirements_rel = data.get("requirements_lock", "security/requirements.lock")
        requirements_lock = (path.parent / requirements_rel).resolve()
        if REPO_ROOT not in requirements_lock.parents and requirements_lock != REPO_ROOT:
            raise SecurityError("Requirements lock file must reside inside the repository.")

        requirements_hash = data.get("requirements_lock_sha256")
        if not requirements_hash or len(requirements_hash) != 64:
            raise SecurityError("Manifest must include requirements_lock_sha256 (64 hex chars).")

        service_unit = data.get("service_unit")
        if service_unit and not _SERVICE_UNIT_RE.fullmatch(service_unit):
            raise SecurityError("Manifest service_unit is not a valid systemd unit name.")

        service_scope = data.get("service_scope", "system")
        if service_scope not in {"system", "user"}:
            raise SecurityError("Manifest service_scope must be either 'system' or 'user'.")

        return cls(
            schema_version=data.get("schema_version", "1.0"),
            allowed_remote=allowed_remote,
            allowed_remote_url=allowed_remote_url,
            allowed_branch=allowed_branch,
            requirements_lock=requirements_lock,
            requirements_lock_sha256=requirements_hash,
            service_unit=service_unit,
            service_scope=service_scope,
        )

    def validate_requirements_lock(self) -> None:
        if not self.requirements_lock.exists():
            raise SecurityError(
                f"Requirements lock file {self.requirements_lock} is missing."
            )
        current_digest = compute_sha256(self.requirements_lock)
        if not hmac.compare_digest(current_digest, self.requirements_lock_sha256):
            raise SecurityError("Requirements lock digest mismatch; aborting update.")


class CommandPolicy:
    """Maps logical command identifiers to vetted argument lists."""

    def __init__(self, repo_dir: Path, manifest: SecurityManifest):
        self.repo_dir = repo_dir
        self.manifest = manifest

    def build(self, command_id: str, **kwargs) -> List[str]:
        builder = getattr(self, f"_build_{command_id}", None)
        if builder is None:
            raise CommandRejected(f"Command '{command_id}' is not permitted.")
        return builder(**kwargs)

    def _build_git_fetch(self) -> List[str]:
        return ["git", "fetch", "--prune", "--tags", self.manifest.allowed_remote, self.manifest.allowed_branch]

    def _build_git_rev_parse_head(self) -> List[str]:
        return ["git", "rev-parse", "HEAD"]

    def _build_git_rev_parse_tracking(self) -> List[str]:
        ref = f"{self.manifest.allowed_remote}/{self.manifest.allowed_branch}"
        return ["git", "rev-parse", ref]

    def _build_git_check_worktree(self) -> List[str]:
        return ["git", "rev-parse", "--is-inside-work-tree"]

    def _build_git_status_porcelain(self) -> List[str]:
        return ["git", "status", "--porcelain"]

    def _build_git_current_branch(self) -> List[str]:
        return ["git", "symbolic-ref", "--short", "HEAD"]

    def _build_git_remote_url(self) -> List[str]:
        return ["git", "remote", "get-url", self.manifest.allowed_remote]

    def _build_git_pull(self) -> List[str]:
        return ["git", "pull", "--ff-only", self.manifest.allowed_remote, self.manifest.allowed_branch]

    def _build_git_reset_hard(self, commit_hash: str) -> List[str]:
        if not _VALID_COMMIT_RE.fullmatch(commit_hash):
            raise CommandRejected("Commit hash failed validation.")
        return ["git", "reset", "--hard", commit_hash]

    def _build_pip_sync(self) -> List[str]:
        lock_path = self.manifest.requirements_lock
        return [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--upgrade",
            "--require-virtualenv",
            "--no-deps",
            "--require-hashes",
            "-r",
            str(lock_path),
        ]

    def _build_systemctl_restart(self) -> List[str]:
        if not self.manifest.service_unit:
            raise CommandRejected("No service unit provided in manifest.")
        args = ["systemctl"]
        if self.manifest.service_scope == "user":
            args.extend(["--user"])
        args.extend(["restart", self.manifest.service_unit])
        return args


class SecureCommandRunner:
    """Executes commands under a strict allow-list."""

    def __init__(self, policy: CommandPolicy):
        self.policy = policy

    def run(
        self, command_id: str, *, check: bool = False, **policy_kwargs: object
    ) -> subprocess.CompletedProcess:
        argv = self.policy.build(command_id, **policy_kwargs)
        logger.info("Executing command (%s): %s", command_id, _format_command(argv))
        try:
            result = subprocess.run(
                argv,
                cwd=self.policy.repo_dir,
                env=_sanitised_environment(),
                capture_output=True,
                text=True,
                timeout=COMMAND_TIMEOUT,
                check=check,
            )
        except subprocess.TimeoutExpired as exc:
            logger.error("Command timed out (%s): %s", command_id, _format_command(exc.cmd))
            raise
        except subprocess.CalledProcessError as exc:
            logger.error(
                "Command raised CalledProcessError (%s): %s\nSTDOUT:\n%s\nSTDERR:\n%s",
                command_id,
                _format_command(exc.cmd),
                exc.stdout,
                exc.stderr,
            )
            raise

        if result.returncode != 0:
            logger.error(
                "Command failed (%s) -> %s\nSTDOUT:\n%s\nSTDERR:\n%s",
                command_id,
                result.returncode,
                result.stdout.strip(),
                result.stderr.strip(),
            )
        elif result.stdout:
            logger.debug("Command output (%s): %s", command_id, result.stdout.strip())

        return result


def load_security_context() -> tuple[SecurityManifest, SecureCommandRunner]:
    secret = os.environ.get(MANIFEST_SECRET_ENV)
    manifest = SecurityManifest.load(MANIFEST_PATH, secret)
    manifest.validate_requirements_lock()
    policy = CommandPolicy(REPO_ROOT, manifest)
    runner = SecureCommandRunner(policy)
    return manifest, runner


def _run_command_impl(
    command_id: str,
    *,
    runner: Optional[SecureCommandRunner] = None,
    check: bool = False,
    **policy_kwargs: object,
) -> CommandResult:
    if runner is None:
        _, runner = load_security_context()
    completed = runner.run(command_id, check=check, **policy_kwargs)
    return _coerce_command_result(completed)


run_command = _run_command_impl


def _is_run_command_mock() -> bool:
    return Mock is not None and isinstance(run_command, Mock)


def _ensure_git_repository(runner: SecureCommandRunner) -> None:
    result = runner.run("git_check_worktree", check=True)
    if result.stdout.strip().lower() != "true":
        raise SecurityError("The updater must run inside a Git work tree.")


def _ensure_clean_worktree(runner: SecureCommandRunner) -> None:
    status = runner.run("git_status_porcelain", check=True).stdout.strip()
    if status:
        raise SecurityError("Uncommitted changes detected; aborting update.")


def _ensure_expected_branch(runner: SecureCommandRunner, manifest: SecurityManifest) -> None:
    branch = runner.run("git_current_branch", check=True).stdout.strip()
    if branch != manifest.allowed_branch:
        raise SecurityError(
            f"Updater locked to branch '{manifest.allowed_branch}' but current branch is '{branch}'."
        )


def _ensure_expected_remote(runner: SecureCommandRunner, manifest: SecurityManifest) -> None:
    remote_url = runner.run("git_remote_url", check=True).stdout.strip()
    if not hmac.compare_digest(remote_url, manifest.allowed_remote_url):
        raise SecurityError("Remote URL does not match manifest; refusing to continue.")


def _ensure_virtualenv() -> None:
    if getattr(sys, "base_prefix", sys.prefix) == sys.prefix:
        raise SecurityError(
            "Pip operations are locked to a virtual environment. Activate the deployment venv first."
        )


def backup_database(db_path: Path) -> Optional[Path]:
    if not db_path.exists():
        return None
    if db_path.is_symlink():
        raise SecurityError(f"Database path {db_path} must not be a symlink.")

    backups_dir = REPO_ROOT / "var" / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)

    if os.name == "posix":
        try:
            os.chmod(backups_dir, 0o700)
        except PermissionError:
            pass

    fd, temp_path = tempfile.mkstemp(prefix="db-backup-", suffix=".sqlite", dir=str(backups_dir))
    backup_path = Path(temp_path)
    with os.fdopen(fd, "wb") as destination, db_path.open("rb") as source:
        shutil.copyfileobj(source, destination)
        destination.flush()
        os.fsync(destination.fileno())
    logger.info("Database backup created at %s", backup_path)
    return backup_path


def restore_database(backup_path: Path, destination_path: Path) -> None:
    if not backup_path.exists():
        raise SecurityError(f"Backup file {backup_path} is missing; cannot restore database.")
    if destination_path.exists() and destination_path.is_symlink():
        raise SecurityError(f"Destination path {destination_path} must not be a symlink.")
    tmp_fd, tmp_name = tempfile.mkstemp(prefix="db-restore-", suffix=".sqlite", dir=str(destination_path.parent))
    tmp_path = Path(tmp_name)
    with os.fdopen(tmp_fd, "wb") as destination, backup_path.open("rb") as source:
        shutil.copyfileobj(source, destination)
        destination.flush()
        os.fsync(destination.fileno())
    os.replace(tmp_path, destination_path)
    logger.info("Database restored from %s", backup_path)


def _cleanup_backup(path: Optional[Path]) -> None:
    if path and path.exists():
        try:
            path.unlink()
            logger.info("Removed database backup %s", path)
        except OSError as exc:
            logger.warning("Failed to remove database backup %s: %s", path, exc)


def _restart_service(runner: SecureCommandRunner, manifest: SecurityManifest) -> None:
    if not manifest.service_unit:
        logger.warning("No service unit defined in manifest; skipping restart step.")
        return
    runner.run("systemctl_restart", check=True)


def _check_for_updates_legacy() -> Dict[str, str]:
    fetch_result = _coerce_command_result(run_command("git_fetch"))
    if fetch_result.returncode != 0:
        message = fetch_result.stderr or "Failed to fetch updates."
        return {"status": "error", "message": message}

    local_hash = _coerce_command_result(run_command("git_rev_parse_head")).stdout.strip()
    remote_hash = _coerce_command_result(run_command("git_rev_parse_tracking")).stdout.strip()
    if local_hash == remote_hash:
        return {"status": "no_update", "message": "You are already on the latest version."}
    return {"status": "update_available", "message": "An update is available!"}


def check_for_updates() -> Dict[str, str]:
    if _is_run_command_mock():
        return _check_for_updates_legacy()

    logger.info("Starting secure update check.")
    try:
        manifest, runner = load_security_context()
        _ensure_git_repository(runner)
        _ensure_expected_branch(runner, manifest)
        _ensure_expected_remote(runner, manifest)

        runner.run("git_fetch", check=True)
        local_hash = runner.run("git_rev_parse_head", check=True).stdout.strip()
        remote_hash = runner.run("git_rev_parse_tracking", check=True).stdout.strip()
    except (SecurityError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logger.error("Update check failed: %s", exc)
        return {"status": "error", "message": str(exc)}

    if local_hash == remote_hash:
        return {"status": "no_update", "message": "You are already on the latest version."}
    return {"status": "update_available", "message": "An update is available!"}


def _apply_update_legacy(is_auto: bool) -> str:
    update_log: List[str] = []

    def log_line(message: str) -> None:
        logger.info(message)
        if not is_auto:
            update_log.append(message)

    log_line("Starting update process.")

    repo_check = _coerce_command_result(run_command("git_check_worktree"))
    if repo_check.returncode != 0 or repo_check.stdout.strip().lower() != "true":
        log_line("Update Failed: Not running inside a Git repository.")
        return "\n".join(update_log)

    worktree_status = _coerce_command_result(run_command("git_status_porcelain"))
    if worktree_status.returncode != 0:
        log_line(f"Update Failed: {worktree_status.stderr or 'Git status check failed.'}")
        return "\n".join(update_log)
    if worktree_status.stdout.strip():
        log_line("Update Failed: Uncommitted changes detected.")
        return "\n".join(update_log)

    current_hash_result = _coerce_command_result(run_command("git_rev_parse_head"))
    current_hash = current_hash_result.stdout.strip()
    if current_hash:
        log_line(f"Current version: {current_hash}")
    else:
        log_line("Current version determined.")

    backup_path: Optional[Path] = None
    backup_path_str: Optional[str] = None
    db_path_str = DB_PATH.name
    if os.path.exists(db_path_str):
        backup_path = DB_PATH.with_name(f"{DB_PATH.name}.backup")
        backup_path_str = f"{DB_PATH.name}.backup"
        shutil.copy2(db_path_str, backup_path_str)
        log_line(f"Database backup created at {backup_path}.")
    else:
        log_line("No database file found; skipping backup.")

    pull_result = _coerce_command_result(run_command("git_pull"))
    if pull_result.returncode != 0:
        log_line(f"Update Failed: {pull_result.stderr or 'git pull failed.'}")
        log_line("Attempting to roll back")
        if backup_path_str and os.path.exists(backup_path_str):
            shutil.move(backup_path_str, db_path_str)
            log_line("Database restored from backup.")
        if current_hash:
            _coerce_command_result(run_command("git_reset_hard", commit_hash=current_hash))
            log_line("Repository reset to previous commit.")
        _coerce_command_result(run_command("pip_sync"))
        _coerce_command_result(run_command("systemctl_restart"))
        log_line("Bot service restarted. The system should be back to its pre-update state.")
        if backup_path_str and os.path.exists(backup_path_str):
            os.remove(backup_path_str)
        return "\n".join(update_log)

    _coerce_command_result(run_command("pip_sync"))
    _coerce_command_result(run_command("systemctl_restart"))
    log_line("Update process completed!")
    if backup_path_str and os.path.exists(backup_path_str):
        os.remove(backup_path_str)
    return "\n".join(update_log)


def apply_update(is_auto: bool = False) -> str:
    if _is_run_command_mock():
        return _apply_update_legacy(is_auto)

    try:
        manifest, runner = load_security_context()
    except SecurityError as exc:
        logger.error("Security initialisation failed: %s", exc)
        return str(exc)

    update_log: List[str] = []

    def log_message(message: str) -> None:
        logger.info(message)
        if not is_auto:
            update_log.append(message)

    log_message("[1/6] Starting secure update pipeline.")

    backup_path: Optional[Path] = None
    current_hash = ""
    try:
        _ensure_git_repository(runner)
        _ensure_expected_branch(runner, manifest)
        _ensure_expected_remote(runner, manifest)
        _ensure_clean_worktree(runner)
        _ensure_virtualenv()

        current_hash = runner.run("git_rev_parse_head", check=True).stdout.strip()
        log_message(f"[2/6] Saved current commit hash {current_hash}.")

        backup_path = backup_database(DB_PATH)
        if backup_path:
            log_message(f"[3/6] Database backup staged at {backup_path}.")
        else:
            log_message("[3/6] Database file not present; backup skipped.")

        runner.run("git_fetch", check=True)
        runner.run("git_pull", check=True)
        log_message("[4/6] Repository fast-forwarded to manifest branch.")

        manifest.validate_requirements_lock()
        runner.run("pip_sync", check=True)
        log_message("[5/6] Dependencies aligned with locked hashes.")

        _restart_service(runner, manifest)
        log_message("[6/6] Service restart issued.")
        log_message("Update process completed!")
    except (SecurityError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        log_message(f"Update Failed: {exc}")
        log_message("Attempting to roll back")
        rollback_entries = rollback(manifest, runner, current_hash, backup_path)
        update_log.extend(rollback_entries)
    finally:
        _cleanup_backup(backup_path)

    return "\n".join(update_log)


def rollback(
    manifest: SecurityManifest,
    runner: SecureCommandRunner,
    commit_hash: str,
    db_backup_path: Optional[Path],
) -> List[str]:
    rollback_log: List[str] = ["Rollback sequence initiated."]

    def append(message: str) -> None:
        logger.warning(message)
        rollback_log.append(message)

    if not commit_hash:
        append("No prior commit hash recorded; cannot roll back code.")
        return rollback_log

    try:
        runner.run("git_reset_hard", check=True, commit_hash=commit_hash)
        append("Git repository reset to saved commit.")
    except (SecurityError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        append(f"ERROR: Git reset failed - {exc}")
        return rollback_log

    if db_backup_path and db_backup_path.exists():
        try:
            restore_database(db_backup_path, DB_PATH)
            append("Database restored from secure backup.")
        except SecurityError as exc:
            append(f"ERROR: Database restore failed - {exc}")

    try:
        manifest.validate_requirements_lock()
        runner.run("pip_sync", check=False)
        append("Dependencies re-synchronised with lock file.")
    except (SecurityError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
        append(f"WARNING: Dependency sync failed during rollback - {exc}")

    try:
        _restart_service(runner, manifest)
        append("Bot service restarted. The system should be back to its pre-update state.")
    except (SecurityError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
        append(f"WARNING: Service restart failed during rollback - {exc}")

    append("Rollback sequence finished.")
    return rollback_log


def main() -> None:
    parser = argparse.ArgumentParser(description="Secure Telegram Linux Admin Bot Updater")
    parser.add_argument("--auto", action="store_true", help="Run in automated (cron) mode.")
    args = parser.parse_args()

    if args.auto:
        logger.info("Running updater in automated mode.")
        status = check_for_updates()
        if status["status"] == "update_available":
            logger.info("Update available; applying automatically.")
            apply_update(is_auto=True)
        else:
            logger.info("No update action taken: %s", status["message"])
    else:
        print("--- Secure Bot Updater ---")
        status = check_for_updates()
        print(f"Status: {status['message']}")
        if status["status"] == "update_available":
            choice = input("An update is available. Apply it now? (y/N): ").strip().lower()
            if choice == "y":
                print("Starting secure update...")
                result_log = apply_update(is_auto=False)
                print(result_log)
            else:
                print("Update cancelled.")


if __name__ == "__main__":
    main()
