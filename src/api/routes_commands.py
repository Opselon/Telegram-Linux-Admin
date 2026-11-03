"""Command execution center + job routes. The policy core is shared."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..jobs.models import Job, JobState
from ..services import get_services
from ..services.command_service import CommandDenied, ConfirmationError
from ..services.permissions import Actor
from .deps import ApiError, not_found, rate_limit, require_permission
from .schemas import CommandRequest, ConfirmRequest, JobOut, PlanOut

router = APIRouter(tags=["commands"])


def _job_out(job: Job) -> JobOut:
    return JobOut(
        id=job.id,
        alias=job.spec.alias,
        state=job.state.value if isinstance(job.state, JobState) else str(job.state),
        command=job.spec.command,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        exit_ok=job.exit_ok,
        error=job.error,
    )


@router.post("/commands", response_model=PlanOut)
async def request_command(payload: CommandRequest,
                          actor: Actor = Depends(rate_limit("commands", max_calls=20, period=60.0))) -> PlanOut:
    try:
        plan = await get_services().commands.request(actor, payload.server_id, payload.command)
    except CommandDenied as exc:
        status = 404 if "server not found" in exc.reason else 403
        raise ApiError(status, "command_denied", exc.reason) from exc
    if plan.kind == "denied":
        # BLOCKED/refused by policy — an explicit 403, never a 200 "success".
        raise ApiError(403, "command_denied", plan.reason or "command refused by policy",
                       details={"risk": plan.risk, "rule": plan.rule})
    pending = None
    if plan.pending is not None:
        pending = {
            "token": plan.pending.token,
            "server_id": plan.pending.server_id,
            "alias": plan.pending.alias,
            "command": plan.pending.command,
            "risk": plan.pending.risk,
            "reason": plan.pending.reason,
            "expires_at": plan.pending.expires_at,
        }
    return PlanOut(
        kind=plan.kind, reason=plan.reason, risk=plan.risk, rule=plan.rule,
        job=_job_out(plan.job) if plan.job is not None else None,
        pending=pending,
    )


@router.post("/commands/confirm", response_model=PlanOut)
async def confirm_command(payload: ConfirmRequest,
                          actor: Actor = Depends(rate_limit("confirm", max_calls=30, period=60.0))) -> PlanOut:
    try:
        job = await get_services().commands.confirm(actor, payload.token)
    except ConfirmationError as exc:
        raise ApiError(400, "confirmation_invalid", str(exc)) from exc
    except CommandDenied as exc:
        raise ApiError(403, "command_denied", exc.reason) from exc
    return PlanOut(kind="executed", job=_job_out(job), risk="confirm")


@router.get("/jobs", response_model=list[JobOut])
async def list_jobs(actor: Actor = Depends(require_permission("jobs.read"))) -> list[JobOut]:
    return [_job_out(j) for j in get_services().commands.list_jobs(actor)]


@router.get("/jobs/{job_id}", response_model=JobOut)
async def get_job(job_id: str,
                  actor: Actor = Depends(require_permission("jobs.read"))) -> JobOut:
    job = get_services().commands.get_job(actor, job_id)
    if job is None:
        raise not_found()
    return _job_out(job)


@router.post("/jobs/{job_id}/cancel", response_model=JobOut)
async def cancel_job(job_id: str,
                     actor: Actor = Depends(require_permission("jobs.cancel"))) -> JobOut:
    cancelled = await get_services().commands.cancel_job(actor, job_id)
    if not cancelled:
        job = get_services().commands.get_job(actor, job_id)
        if job is None:
            raise not_found()
        raise ApiError(409, "not_cancellable", "Job is already finished or owned by another user.")
    job = get_services().commands.get_job(actor, job_id)
    if job is None:
        raise not_found()
    return _job_out(job)
