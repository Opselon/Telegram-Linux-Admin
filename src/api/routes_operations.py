"""Operations routes: monitoring, services, processes, docker, firewall, packages."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ..services import get_services
from ..services.operations_service import OperationError
from ..services.permissions import Actor
from .deps import ApiError, not_found, require_permission
from .schemas import (
    ContainerActionRequest,
    FirewallDeleteRequest,
    FirewallRuleRequest,
    OkOut,
    PackageRequest,
    ProcessKillRequest,
    ServiceActionRequest,
)

router = APIRouter(prefix="/servers/{server_id}", tags=["operations"])


def _op_error(exc: Exception) -> ApiError:
    return ApiError(502, "operation_failed", str(exc)[:300])


# -- monitoring ----------------------------------------------------------------

@router.get("/monitoring")
async def monitoring(server_id: int, refresh: bool = Query(default=False),
                     actor: Actor = Depends(require_permission("monitoring.read"))) -> dict:
    try:
        return await get_services().monitor.snapshot(actor, server_id, force=refresh)
    except OperationError as exc:
        raise _op_error(exc) from exc


@router.get("/monitoring/history")
async def monitoring_history(server_id: int,
                             actor: Actor = Depends(require_permission("monitoring.read"))) -> list:
    return get_services().monitor.history(actor, server_id)


# -- services ---------------------------------------------------------------------

@router.get("/services")
async def list_services(server_id: int,
                        actor: Actor = Depends(require_permission("services.read"))) -> list:
    try:
        return await get_services().operations.list_services(actor, server_id)
    except OperationError as exc:
        raise _op_error(exc) from exc


@router.get("/services/{name}")
async def service_status(server_id: int, name: str,
                         actor: Actor = Depends(require_permission("services.read"))) -> dict:
    try:
        return await get_services().operations.service_status(actor, server_id, name)
    except (OperationError, ValueError) as exc:
        raise _op_error(exc) from exc


@router.post("/services/action", response_model=OkOut)
async def service_action(server_id: int, payload: ServiceActionRequest,
                         actor: Actor = Depends(require_permission("services.control"))) -> OkOut:
    try:
        return OkOut(**await get_services().operations.service_action(
            actor, server_id, payload.name, payload.action))
    except (OperationError, ValueError) as exc:
        raise ApiError(400, "service_action_failed", str(exc)[:300]) from exc


# -- processes -----------------------------------------------------------------------

@router.get("/processes")
async def list_processes(server_id: int, search: str = Query(default=""),
                         sort: str = Query(default="cpu"),
                         actor: Actor = Depends(require_permission("processes.read"))) -> list:
    try:
        return await get_services().operations.list_processes(
            actor, server_id, search=search, sort=sort)
    except OperationError as exc:
        raise _op_error(exc) from exc


@router.post("/processes/kill", response_model=OkOut)
async def kill_process(server_id: int, payload: ProcessKillRequest,
                       actor: Actor = Depends(require_permission("processes.kill"))) -> OkOut:
    try:
        return OkOut(**await get_services().operations.kill_process(
            actor, server_id, payload.pid, signal=payload.signal))
    except (OperationError, ValueError) as exc:
        raise ApiError(400, "kill_failed", str(exc)[:300]) from exc


# -- docker -----------------------------------------------------------------------------

@router.get("/docker/containers")
async def docker_containers(server_id: int,
                            actor: Actor = Depends(require_permission("docker.read"))) -> list:
    try:
        return await get_services().operations.docker_containers(actor, server_id)
    except OperationError as exc:
        raise _op_error(exc) from exc


@router.post("/docker/action", response_model=OkOut)
async def docker_action(server_id: int, payload: ContainerActionRequest,
                        actor: Actor = Depends(require_permission("docker.control"))) -> OkOut:
    try:
        return OkOut(**await get_services().operations.docker_action(
            actor, server_id, payload.container, payload.action))
    except (OperationError, ValueError) as exc:
        raise ApiError(400, "docker_action_failed", str(exc)[:300]) from exc


@router.get("/docker/logs")
async def docker_logs(server_id: int, container: str = Query(min_length=1),
                      tail: int = Query(default=100, ge=1, le=1000),
                      actor: Actor = Depends(require_permission("docker.read"))) -> dict:
    try:
        logs = await get_services().operations.docker_logs(actor, server_id, container, tail=tail)
        return {"container": container, "logs": logs[-20000:]}
    except (OperationError, ValueError) as exc:
        raise _op_error(exc) from exc


@router.get("/docker/images")
async def docker_images(server_id: int,
                        actor: Actor = Depends(require_permission("docker.read"))) -> list:
    try:
        return await get_services().operations.docker_images(actor, server_id)
    except OperationError as exc:
        raise _op_error(exc) from exc


# -- firewall -----------------------------------------------------------------------------

@router.get("/firewall")
async def firewall_rules(server_id: int,
                         actor: Actor = Depends(require_permission("firewall.read"))) -> dict:
    try:
        return await get_services().operations.firewall_rules(actor, server_id)
    except OperationError as exc:
        raise _op_error(exc) from exc


@router.post("/firewall/rules", response_model=OkOut)
async def firewall_add(server_id: int, payload: FirewallRuleRequest,
                       actor: Actor = Depends(require_permission("firewall.modify"))) -> OkOut:
    try:
        return OkOut(**await get_services().operations.firewall_add(
            actor, server_id, rule=payload.rule, direction=payload.direction))
    except (OperationError, ValueError) as exc:
        raise ApiError(400, "firewall_failed", str(exc)[:300]) from exc


@router.post("/firewall/delete", response_model=OkOut)
async def firewall_delete(server_id: int, payload: FirewallDeleteRequest,
                          actor: Actor = Depends(require_permission("firewall.modify"))) -> OkOut:
    try:
        return OkOut(**await get_services().operations.firewall_delete(
            actor, server_id, rule=payload.rule))
    except (OperationError, ValueError) as exc:
        raise ApiError(400, "firewall_failed", str(exc)[:300]) from exc


# -- packages --------------------------------------------------------------------------------

@router.post("/packages/update", response_model=OkOut)
async def package_update(server_id: int,
                         actor: Actor = Depends(require_permission("services.control"))) -> OkOut:
    try:
        return OkOut(**await get_services().operations.package_update(actor, server_id))
    except OperationError as exc:
        raise _op_error(exc) from exc


@router.post("/packages/upgrade", response_model=OkOut)
async def package_upgrade(server_id: int,
                          actor: Actor = Depends(require_permission("services.control"))) -> OkOut:
    try:
        return OkOut(**await get_services().operations.package_upgrade(actor, server_id))
    except OperationError as exc:
        raise _op_error(exc) from exc


@router.post("/packages/install", response_model=OkOut)
async def package_install(server_id: int, payload: PackageRequest,
                          actor: Actor = Depends(require_permission("services.control"))) -> OkOut:
    if not payload.name:
        raise ApiError(400, "validation_error", "package name is required")
    try:
        return OkOut(**await get_services().operations.package_install(actor, server_id, payload.name))
    except (OperationError, ValueError) as exc:
        raise ApiError(400, "install_failed", str(exc)[:300]) from exc
