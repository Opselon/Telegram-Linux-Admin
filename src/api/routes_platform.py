"""File manager, backup, security/users, and realtime event routes."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi import UploadFile, File as UploadFileField

from ..services import get_services
from ..services.backup_service import BackupError
from ..services.file_service import FilePolicyError
from ..services.permissions import Actor, PermissionDenied
from ..services.auth_service import AuthError
from .deps import ApiError, current_actor, not_found, require_permission
from .schemas import (
    AccessModeRequest,
    CreateUserRequest,
    DisabledRequest,
    FileDeleteRequest,
    FileMkdirRequest,
    FileRenameRequest,
    LinkCodeRequest,
    NotificationPrefsRequest,
    ResetPasswordRequest,
    RoleRequest,
    WhitelistRequest,
)

router = APIRouter(prefix="/servers/{server_id}/files", tags=["files"])
platform = APIRouter(tags=["platform"])

UPLOAD_LIMIT = 50 * 1024 * 1024


def _policy_error(exc: Exception) -> ApiError:
    return ApiError(400, "file_policy", str(exc))


# ===========================================================================
# Files
# ===========================================================================

@router.get("")
async def list_files(server_id: int, path: str = Query(...),
                     actor: Actor = Depends(require_permission("files.read"))) -> dict:
    try:
        return await get_services().files.list_dir(actor, server_id, path)
    except FilePolicyError as exc:
        raise _policy_error(exc) from exc


@router.get("/content")
async def download_file(server_id: int, path: str = Query(...),
                        actor: Actor = Depends(require_permission("files.download"))) -> FileResponse:
    try:
        get_services().files._check_path(actor, path)  # noqa: SLF001 - pre-validate before disk write
    except FilePolicyError as exc:
        raise _policy_error(exc) from exc
    fd, local = tempfile.mkstemp(prefix="tla-dl-")
    os.close(fd)
    try:
        await get_services().files.manager_download(actor, server_id, path, local)
    except Exception as exc:
        os.unlink(local)
        raise ApiError(502, "download_failed", str(exc)[:300]) from exc

    name = os.path.basename(path.rstrip("/")) or "download"
    return FileResponse(local, filename=name, background=_cleanup_task(local))


from starlette.background import BackgroundTask  # noqa: E402


def _cleanup_task(local: str) -> BackgroundTask:
    """Deletes the temp download after the response finishes streaming."""
    import pathlib

    def _run() -> None:
        try:
            pathlib.Path(local).unlink(missing_ok=True)
        except OSError:
            pass
    return BackgroundTask(_run)


@router.post("/content")
async def upload_file(server_id: int, path: str = Query(...),
                      file: UploadFile = UploadFileField(...),
                      actor: Actor = Depends(require_permission("files.upload"))) -> dict:
    if (file.size or 0) > UPLOAD_LIMIT:
        raise ApiError(413, "upload_too_large", "File exceeds the upload size limit.")
    filename = os.path.basename(file.filename or "upload")
    fd, local = tempfile.mkstemp(prefix="tla-ul-")
    try:
        with os.fdopen(fd, "wb") as out:
            written = 0
            while chunk := await file.read(1 << 20):
                written += len(chunk)
                if written > UPLOAD_LIMIT:
                    raise ApiError(413, "upload_too_large", "File exceeds the upload size limit.")
                out.write(chunk)
        remote = await get_services().files.manager_upload(actor, server_id, local, path, filename)
    except FilePolicyError as exc:
        raise _policy_error(exc) from exc
    finally:
        try:
            os.unlink(local)
        except OSError:
            pass
    return {"ok": True, "path": remote}


@router.post("/mkdir")
async def mkdir(server_id: int, payload: FileMkdirRequest,
                actor: Actor = Depends(require_permission("files.write"))) -> dict:
    try:
        await get_services().files.mkdir(actor, server_id, payload.path)
    except FilePolicyError as exc:
        raise _policy_error(exc) from exc
    return {"ok": True}


@router.post("/rename")
async def rename(server_id: int, payload: FileRenameRequest,
                 actor: Actor = Depends(require_permission("files.write"))) -> dict:
    try:
        await get_services().files.rename(actor, server_id, payload.path, payload.new_name)
    except FilePolicyError as exc:
        raise _policy_error(exc) from exc
    return {"ok": True}


@router.post("/delete")
async def delete_file(server_id: int, payload: FileDeleteRequest,
                      actor: Actor = Depends(require_permission("files.delete"))) -> dict:
    try:
        await get_services().files.delete_file(actor, server_id, payload.path)
    except FilePolicyError as exc:
        raise _policy_error(exc) from exc
    return {"ok": True}


# ===========================================================================
# Backups
# ===========================================================================

@platform.get("/backups")
async def list_backups(actor: Actor = Depends(require_permission("backups.read"))) -> list:
    return get_services().backups.list_backups(actor)


@platform.post("/backups", status_code=201)
async def create_backup(actor: Actor = Depends(require_permission("backups.create"))) -> dict:
    return get_services().backups.create_backup(actor)


@platform.get("/backups/{backup_id}")
async def get_backup(backup_id: str,
                     actor: Actor = Depends(require_permission("backups.read"))) -> dict:
    try:
        return get_services().backups.get_backup(actor, backup_id)
    except BackupError as exc:
        raise ApiError(404, "not_found", str(exc)) from exc


@platform.get("/backups/{backup_id}/download")
async def download_backup(backup_id: str,
                          actor: Actor = Depends(require_permission("backups.read"))) -> FileResponse:
    try:
        path = get_services().backups._resolve(actor, backup_id)  # noqa: SLF001
    except BackupError as exc:
        raise ApiError(404, "not_found", str(exc)) from exc
    return FileResponse(path, filename=path.name)


@platform.post("/backups/{backup_id}/verify")
async def verify_backup(backup_id: str,
                        actor: Actor = Depends(require_permission("backups.read"))) -> dict:
    try:
        return get_services().backups.verify_backup(actor, backup_id)
    except BackupError as exc:
        raise ApiError(404, "not_found", str(exc)) from exc


@platform.post("/backups/{backup_id}/restore")
async def restore_backup(backup_id: str, payload: dict,
                         actor: Actor = Depends(require_permission("backups.restore"))) -> dict:
    if payload.get("confirm") is not True:
        raise ApiError(400, "confirmation_required", "Set confirm=true to restore.")
    try:
        return get_services().backups.restore_backup(actor, backup_id)
    except BackupError as exc:
        raise ApiError(400, "restore_failed", str(exc)[:300]) from exc


@platform.delete("/backups/{backup_id}")
async def delete_backup(backup_id: str,
                        actor: Actor = Depends(require_permission("backups.delete"))) -> dict:
    try:
        get_services().backups.delete_backup(actor, backup_id)
    except BackupError as exc:
        raise ApiError(404, "not_found", str(exc)) from exc
    return {"ok": True}


# ===========================================================================
# Security center + users
# ===========================================================================

@platform.get("/security/overview")
async def security_overview(actor: Actor = Depends(require_permission("security.read"))) -> dict:
    return get_services().security.overview(actor)


@platform.get("/security/events")
async def security_events(limit: int = Query(default=100, ge=1, le=500),
                          actor: Actor = Depends(require_permission("security.read"))) -> list:
    return get_services().security.audit_tail(actor, limit=limit)


@platform.put("/settings/access-mode")
async def set_access_mode(payload: AccessModeRequest,
                          actor: Actor = Depends(require_permission("security.manage"))) -> dict:
    try:
        mode = get_services().security.set_access_mode(actor, payload.mode)
    except ValueError as exc:
        raise ApiError(400, "invalid_mode", str(exc)) from exc
    return {"ok": True, "mode": mode}


@platform.put("/settings/whitelist")
async def set_whitelist(payload: WhitelistRequest,
                        actor: Actor = Depends(require_permission("security.manage"))) -> dict:
    get_services().security.update_whitelist(
        actor, whitelisted=payload.whitelisted, admins=payload.admins)
    return {"ok": True}


@platform.get("/users")
async def list_users(actor: Actor = Depends(require_permission("users.manage"))) -> list:
    return get_services().security.list_users(actor)


@platform.post("/users", status_code=201)
async def create_user(payload: CreateUserRequest,
                      actor: Actor = Depends(require_permission("users.manage"))) -> dict:
    try:
        return get_services().security.create_web_account(
            actor, username=payload.username, password=payload.password, role=payload.role)
    except AuthError as exc:
        raise ApiError(400, exc.key, str(exc)) from exc


@platform.post("/users/{account_id}/role")
async def set_role(account_id: int, payload: RoleRequest,
                   actor: Actor = Depends(require_permission("users.manage"))) -> dict:
    try:
        get_services().security.set_role(actor, account_id, payload.role)
    except AuthError as exc:
        raise ApiError(400, exc.key, str(exc)) from exc
    except LookupError as exc:
        raise not_found() from exc
    return {"ok": True}


@platform.post("/users/{account_id}/disabled")
async def set_disabled(account_id: int, payload: DisabledRequest,
                       actor: Actor = Depends(require_permission("users.manage"))) -> dict:
    try:
        get_services().security.set_disabled(actor, account_id, payload.disabled)
    except LookupError as exc:
        raise not_found() from exc
    return {"ok": True}


@platform.post("/users/{account_id}/password")
async def reset_password(account_id: int, payload: ResetPasswordRequest,
                         actor: Actor = Depends(require_permission("users.manage"))) -> dict:
    try:
        get_services().security.reset_password(actor, account_id, payload.password)
    except (AuthError, LookupError) as exc:
        raise ApiError(400, "reset_failed", str(exc)) from exc
    return {"ok": True, "sessions_revoked": True}


@platform.delete("/users/{account_id}")
async def delete_user(account_id: int,
                      actor: Actor = Depends(require_permission("users.manage"))) -> dict:
    try:
        get_services().security.delete_account(actor, account_id)
    except (AuthError, LookupError) as exc:
        status = 400 if isinstance(exc, AuthError) else 404
        raise ApiError(status, "delete_failed", str(exc)) from exc
    return {"ok": True}


@platform.post("/bale/link-code")
async def create_link_code(payload: LinkCodeRequest,
                           actor: Actor = Depends(require_permission("security.read"))) -> dict:
    try:
        code = get_services().security.create_link_code(actor, payload.account_id)
    except (LookupError, PermissionDenied) as exc:
        raise ApiError(403, "forbidden", str(exc)) from exc
    return {"code": code, "ttl_seconds": 300}


# NOTE: Bale-side account linking happens in the Bale bot conversation
# (``src/adapters/bale``), which consumes link codes created here.  There is
# deliberately no web endpoint that accepts a raw link code: the code is the
# proof of authorization and must be consumed by the bot that issued the
# enrollment conversation.


@platform.get("/notifications/prefs")
async def get_notification_prefs(actor: Actor = Depends(current_actor)) -> dict:
    from ..services import store
    return {"disabled_categories": sorted(store.get_disabled_categories(actor.account_id))}


@platform.put("/notifications/prefs")
async def set_notification_prefs(payload: NotificationPrefsRequest,
                                 actor: Actor = Depends(current_actor)) -> dict:
    from ..services import store
    from ..services.notification_service import KNOWN_CATEGORIES
    unknown = [c for c in payload.disabled_categories if c not in KNOWN_CATEGORIES]
    if unknown:
        raise ApiError(400, "unknown_category", f"Unknown categories: {unknown}")
    store.set_disabled_categories(actor.account_id, set(payload.disabled_categories))
    return {"ok": True}


@platform.get("/notifications/recent")
async def recent_alerts(limit: int = Query(default=50, ge=1, le=200),
                        actor: Actor = Depends(require_permission("security.read"))) -> list:
    return await get_services().notifications.recent(limit)


# ===========================================================================
# Realtime: SSE alert stream (auth via session cookie; per-event authorization)
# ===========================================================================

@platform.get("/events/stream")
async def event_stream(request: Request,
                       actor: Actor = Depends(require_permission("monitoring.read"))) -> StreamingResponse:
    from .events import subscribe

    queue: asyncio.Queue = asyncio.Queue(maxsize=64)

    def _listener(kind: str, payload: dict) -> None:
        try:
            queue.put_nowait({"kind": kind, **payload})
        except asyncio.QueueFull:
            pass  # drop rather than block the emitter

    unsubscribe = subscribe(_listener)

    async def _gen():
        try:
            yield ": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            unsubscribe()

    return StreamingResponse(_gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
