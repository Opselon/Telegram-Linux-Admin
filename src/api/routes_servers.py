"""Server management routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from ..services import get_services
from ..services.permissions import Actor
from .deps import ApiError, not_found, rate_limit, require_csrf, require_permission
from .schemas import OkOut, RenameRequest, ServerCreate, ServerOut, ServerUpdate

router = APIRouter(prefix="/servers", tags=["servers"])


def _to_out(server: dict, actor: Actor) -> ServerOut:
    return ServerOut(
        id=int(server["id"]),
        alias=server["alias"],
        hostname=server["hostname"],
        user=server["user"],
        port=int(server.get("port", 22)),
        owner=bool(server.get("owner", False)),
        host_key=server.get("host_key"),
        created_at=str(server.get("created_at")) if server.get("created_at") else None,
    )


@router.get("", response_model=list[ServerOut])
async def list_servers(actor: Actor = Depends(require_permission("servers.read"))) -> list[ServerOut]:
    return [_to_out(s, actor) for s in get_services().servers.list_servers(actor)]


@router.post("", response_model=ServerOut, status_code=201)
async def create_server(payload: ServerCreate,
                        actor: Actor = Depends(require_permission("servers.create"))) -> ServerOut:
    try:
        server = get_services().servers.create_server(
            actor, alias=payload.alias, hostname=payload.hostname, user=payload.user,
            password=payload.password or None, key_path=payload.key_path or None,
            port=payload.port,
        )
    except ValueError as exc:
        raise ApiError(400, "validation_error", str(exc)) from exc
    return _to_out(server, actor)


@router.get("/{server_id}", response_model=ServerOut)
async def get_server(server_id: int,
                     actor: Actor = Depends(require_permission("servers.read"))) -> ServerOut:
    try:
        return _to_out(get_services().servers.get_server(actor, server_id), actor)
    except LookupError as exc:
        raise not_found() from exc


@router.patch("/{server_id}", response_model=ServerOut)
async def update_server(server_id: int, payload: ServerUpdate,
                        actor: Actor = Depends(require_permission("servers.update"))) -> ServerOut:
    changes = payload.model_dump(exclude_none=True)
    if not changes:
        raise ApiError(400, "validation_error", "no fields to update")
    try:
        return _to_out(get_services().servers.update_server(actor, server_id, changes), actor)
    except LookupError as exc:
        raise not_found() from exc
    except ValueError as exc:
        raise ApiError(400, "validation_error", str(exc)) from exc


@router.post("/{server_id}/rename", response_model=ServerOut)
async def rename_server(server_id: int, payload: RenameRequest,
                        actor: Actor = Depends(require_permission("servers.update"))) -> ServerOut:
    try:
        return _to_out(get_services().servers.rename_server(actor, server_id, payload.alias), actor)
    except LookupError as exc:
        raise not_found() from exc
    except ValueError as exc:
        raise ApiError(400, "validation_error", str(exc)) from exc


@router.delete("/{server_id}", response_model=OkOut)
async def delete_server(server_id: int,
                        actor: Actor = Depends(require_permission("servers.delete"))) -> OkOut:
    try:
        get_services().servers.delete_server(actor, server_id)
    except LookupError as exc:
        raise not_found() from exc
    return OkOut()


@router.post("/{server_id}/connect", response_model=OkOut)
async def connect(server_id: int,
                  actor: Actor = Depends(require_permission("servers.connect"))) -> OkOut:
    result = await get_services().servers.connect(actor, server_id)
    return OkOut(ok=result["ok"], error=result.get("error"))


@router.post("/{server_id}/disconnect", response_model=OkOut)
async def disconnect(server_id: int,
                     actor: Actor = Depends(require_permission("servers.connect"))) -> OkOut:
    result = await get_services().servers.disconnect(actor, server_id)
    return OkOut(ok=result["ok"], error=result.get("error"))


@router.post("/{server_id}/test", response_model=OkOut)
async def test_connection(server_id: int,
                          actor: Actor = Depends(require_permission("servers.connect"))) -> OkOut:
    result = await get_services().servers.test_connection(actor, server_id)
    return OkOut(ok=result["ok"], error=result.get("error"))


@router.get("/{server_id}/hostkey")
async def hostkey(server_id: int,
                  actor: Actor = Depends(require_permission("servers.read"))) -> dict:
    try:
        return get_services().servers.host_key_status(actor, server_id)
    except LookupError as exc:
        raise not_found() from exc
