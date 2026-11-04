"""Platform roles and the static role → permission mapping.

Roles are coarse; permissions are the unit of enforcement.  Every adapter
(Telegram, Bale, web) resolves its caller into an :class:`Actor` and every
service call checks a permission string via :func:`require`.

Permission naming follows ``<resource>.<action>`` so new resources can be
added without changing the model.
"""

from __future__ import annotations

from dataclasses import dataclass, field

ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLE_OPERATOR = "operator"
ROLE_READONLY = "read_only"

VALID_ROLES = (ROLE_OWNER, ROLE_ADMIN, ROLE_OPERATOR, ROLE_READONLY)

_READ_PERMISSIONS = (
    "servers.read",
    "commands.read",
    "monitoring.read",
    "services.read",
    "processes.read",
    "docker.read",
    "firewall.read",
    "files.read",
    "backups.read",
    "jobs.read",
    "security.read",
)

_ROLE_PERMISSIONS: dict[str, set[str]] = {
    ROLE_OWNER: {"*"},
    ROLE_ADMIN: {
        "servers.create", "servers.update", "servers.delete", "servers.connect",
        "commands.execute", "commands.confirm", "commands.cancel",
        "services.control", "processes.kill", "docker.control",
        "firewall.modify", "files.write", "files.upload", "files.download", "files.delete",
        "backups.create", "backups.restore", "backups.delete",
        "users.manage", "security.manage", "jobs.cancel",
        *_READ_PERMISSIONS,
    },
    ROLE_OPERATOR: {
        "servers.create", "servers.connect",
        "commands.execute", "commands.confirm", "commands.cancel",
        "services.control", "processes.kill", "docker.control",
        "files.write", "files.upload", "files.download",
        "backups.create", "jobs.cancel",
        *_READ_PERMISSIONS,
    },
    ROLE_READONLY: set(_READ_PERMISSIONS),
}


def permissions_for_role(role: str) -> frozenset[str]:
    """Returns the permission set for *role* (unknown roles get read-only)."""
    perms = _ROLE_PERMISSIONS.get(role, _ROLE_PERMISSIONS[ROLE_READONLY])
    return frozenset(perms)


def role_has_permission(role: str, permission: str) -> bool:
    """True when *role* grants *permission* (``*`` grants everything)."""
    perms = _ROLE_PERMISSIONS.get(role, ())
    if "*" in perms:
        return True
    if permission in perms:
        return True
    # A role holding servers.* style wildcards would match here; the static
    # table does not use resource wildcards today, but keep the check cheap
    # and future-proof.
    resource, _, _action = permission.partition(".")
    return f"{resource}.*" in perms


class PermissionDenied(Exception):
    """Raised by :func:`require` when an actor lacks a permission."""

    def __init__(self, actor: "Actor", permission: str) -> None:
        self.actor = actor
        self.permission = permission
        super().__init__(f"account {actor.account_id} lacks permission '{permission}'")


@dataclass(frozen=True)
class Actor:
    """The resolved identity behind one operation, regardless of interface.

    ``owner_id`` is the *legacy* numeric owner key used by ``src.database``
    (the Telegram id for Telegram accounts, a negative synthetic id for
    platform accounts — see ``services.store``).  ``account_id`` is the
    canonical identity.
    """

    account_id: int
    kind: str                 # 'telegram' | 'bale' | 'web'
    display_name: str
    role: str
    owner_id: int
    permissions: frozenset[str] = field(default_factory=frozenset)

    def has(self, permission: str) -> bool:
        return role_has_permission(self.role, permission)

    def require(self, permission: str) -> None:
        if not self.has(permission):
            raise PermissionDenied(self, permission)
