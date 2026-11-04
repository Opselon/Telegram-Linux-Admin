"""Platform services package — the shared management core.

``get_services()`` returns the process-wide :class:`PlatformServices`
instance built by the runtime (``src.runtime_app``).  Adapters (Telegram
handlers where they call into the core, the Bale bot, the FastAPI backend)
must resolve behavior through these services; none of them may bypass
authorization, command policy, audit logging, or the job engine.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from ..access_control import permissions as _telegram_permissions  # noqa: F401 (re-export)
from ..access_control import rate_limiter  # noqa: F401 (re-export for adapters)
from ..jobs.queue import JobQueue
from ..ssh_manager import SSHManager

from .auth_service import AuthError, AuthService, MFARequired
from .backup_service import BackupError, BackupService
from .command_service import CommandDenied, CommandService, ConfirmationError
from .file_service import FilePolicyError, FileService
from .monitoring_service import MonitoringService
from .notification_service import NotificationService
from .operations_service import OperationError, OperationsService
from .permissions import Actor, PermissionDenied
from .security_service import SecurityService
from .server_service import ServerService
from .store import StoreError


@dataclass
class PlatformServices:
    """The assembled management core. Build with :func:`build_services`."""

    auth: AuthService
    servers: ServerService
    commands: CommandService
    jobs: CommandService  # job query/cancel surface (same service, explicit name)
    monitor: MonitoringService
    operations: OperationsService
    files: FileService
    backups: BackupService
    security: SecurityService
    notifications: NotificationService
    manager: SSHManager
    queue: JobQueue


def build_services(
    *,
    manager: SSHManager | None = None,
    queue: JobQueue | None = None,
) -> PlatformServices:
    """Assembles the service graph over the existing singletons.

    The SSH manager and job queue default to fresh instances so tests and the
    runtime can both control lifecycle; adapters always receive the same
    objects the rest of the process uses.
    """
    manager = manager or SSHManager()
    queue = queue or JobQueue()
    servers = ServerService(manager=manager)
    auth = AuthService()
    operations = OperationsService(servers=servers)
    commands = CommandService(queue=queue, manager=manager, servers=servers)
    return PlatformServices(
        auth=auth,
        servers=servers,
        commands=commands,
        jobs=commands,  # job query/cancel surface shares the command service
        monitor=MonitoringService(servers=servers),
        operations=operations,
        files=FileService(servers=servers),
        backups=BackupService(),
        security=SecurityService(auth=auth),
        notifications=NotificationService(),
        manager=manager,
        queue=queue,
    )


_services: PlatformServices | None = None
_lock = threading.Lock()


def set_services(services: PlatformServices) -> None:
    """Installs the process-wide service instance (runtime startup)."""
    global _services
    with _lock:
        _services = services


def get_services() -> PlatformServices:
    """Returns the process-wide services, building them on first use.

    Lazy build keeps CLI tools, tests, and adapters safe: the runtime still
    calls :func:`set_services` explicitly so every component shares one
    SSHManager and one JobQueue — a lazily built instance only ever happens
    once, and only when the runtime has not initialized first.
    """
    global _services
    if _services is None:
        with _lock:
            if _services is None:
                _services = build_services()
    return _services


__all__ = [
    "Actor",
    "AuthError",
    "BackupError",
    "BackupService",
    "CommandDenied",
    "CommandService",
    "ConfirmationError",
    "FilePolicyError",
    "FileService",
    "MFARequired",
    "MonitoringService",
    "NotificationService",
    "OperationError",
    "OperationsService",
    "PermissionDenied",
    "PlatformServices",
    "SecurityService",
    "ServerService",
    "StoreError",
    "build_services",
    "get_services",
    "set_services",
]
