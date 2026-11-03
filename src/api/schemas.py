"""Typed request/response schemas for the /api/v1 surface."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

CommandPlanKind = Literal["executed", "confirm", "denied"]


# --- auth -----------------------------------------------------------------

class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class SetupRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=10, max_length=256)
    setup_token: str | None = None


class MFARequest(BaseModel):
    pending_token: str = Field(min_length=8, max_length=128)
    code: str = Field(min_length=6, max_length=8)


class PasswordChangeRequest(BaseModel):
    current: str = Field(min_length=1, max_length=256)
    new: str = Field(min_length=10, max_length=256)


class AccountOut(BaseModel):
    account_id: int
    username: str
    role: str
    permissions: list[str] = []


# --- servers ----------------------------------------------------------------

class ServerCreate(BaseModel):
    alias: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    hostname: str = Field(min_length=1, max_length=253)
    user: str = Field(min_length=1, max_length=32)
    password: str | None = Field(default=None, max_length=256)
    key_path: str | None = Field(default=None, max_length=512)
    port: int = Field(default=22, ge=1, le=65535)


class ServerUpdate(BaseModel):
    hostname: str | None = Field(default=None, min_length=1, max_length=253)
    user: str | None = Field(default=None, min_length=1, max_length=32)
    password: str | None = Field(default=None, max_length=256)
    key_path: str | None = Field(default=None, max_length=512)
    port: int | None = Field(default=None, ge=1, le=65535)


class ServerOut(BaseModel):
    id: int
    alias: str
    hostname: str
    user: str
    port: int
    owner: bool
    host_key: str | None = None
    created_at: str | None = None


class RenameRequest(BaseModel):
    alias: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class OkOut(BaseModel):
    ok: bool = True
    error: str | None = None


# --- commands / jobs ---------------------------------------------------------

class CommandRequest(BaseModel):
    server_id: int
    command: str = Field(min_length=1, max_length=4096)


class PendingOut(BaseModel):
    token: str
    server_id: int
    alias: str
    command: str
    risk: str
    reason: str
    expires_at: float


class PlanOut(BaseModel):
    kind: CommandPlanKind
    reason: str = ""
    risk: str = ""
    rule: str = ""
    job: "JobOut | None" = None
    pending: PendingOut | None = None


class ConfirmRequest(BaseModel):
    token: str = Field(min_length=8, max_length=128)


class JobOut(BaseModel):
    id: str
    alias: str
    state: str
    command: str
    created_at: float
    started_at: float | None = None
    finished_at: float | None = None
    exit_ok: bool | None = None
    error: str | None = None


# --- operations ---------------------------------------------------------------

class ServiceActionRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    action: Literal["start", "stop", "restart", "enable", "disable"]


class ProcessKillRequest(BaseModel):
    pid: int = Field(ge=2)
    signal: Literal["TERM", "KILL"] = "TERM"


class ContainerActionRequest(BaseModel):
    container: str = Field(min_length=1, max_length=128)
    action: Literal["start", "stop", "restart"]


class FirewallRuleRequest(BaseModel):
    rule: str = Field(min_length=1, max_length=128)
    direction: Literal["allow", "deny"] = "allow"


class FirewallDeleteRequest(BaseModel):
    rule: str = Field(min_length=1, max_length=128)


class PackageRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)


# --- files ----------------------------------------------------------------------

class FileMkdirRequest(BaseModel):
    path: str = Field(min_length=1, max_length=1024)


class FileRenameRequest(BaseModel):
    path: str = Field(min_length=1, max_length=1024)
    new_name: str = Field(min_length=1, max_length=255)


class FileDeleteRequest(BaseModel):
    path: str = Field(min_length=1, max_length=1024)


# --- security ----------------------------------------------------------------

class AccessModeRequest(BaseModel):
    mode: Literal["open", "whitelist", "admin_only"]


class WhitelistRequest(BaseModel):
    whitelisted: list[int] | None = None
    admins: list[int] | None = None


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=10, max_length=256)
    role: Literal["admin", "operator", "read_only"]


class RoleRequest(BaseModel):
    role: Literal["owner", "admin", "operator", "read_only"]


class DisabledRequest(BaseModel):
    disabled: bool


class ResetPasswordRequest(BaseModel):
    password: str = Field(min_length=10, max_length=256)


class LinkCodeRequest(BaseModel):
    account_id: int | None = None


class BaleLinkRequest(BaseModel):
    code: str = Field(min_length=4, max_length=12)
    display_name: str | None = Field(default=None, max_length=128)


class NotificationPrefsRequest(BaseModel):
    disabled_categories: list[str] = []


# --- backups -----------------------------------------------------------------

class RestoreRequest(BaseModel):
    confirm: bool


PlanOut.model_rebuild()
