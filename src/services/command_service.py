"""Command execution service — one policy for every interface.

Flow: permission check → policy/profile evaluation → risk classification →
SAFE executes through the bounded JobQueue; CONFIRM creates a single-use,
TTL-bound, actor-bound token; BLOCKED is refused everywhere, for every role.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .. import database
from ..audit import audit as _audit


def audit_record(event: str, **fields: object) -> None:
    """Audit one security-relevant event (shared helper for all services)."""
    _audit.record(event, **fields)
from ..command_policy import CONFIRMATION_TTL, Risk, classify, validate_command_text
from ..jobs.models import Job, JobSpec, JobState
from ..jobs.queue import JobQueue, QueueFull
from ..metrics import METRICS
from ..config import config as app_config
from ..policy import PolicyEngine, Profile, load_profiles, profile_for
from ..ssh_manager import SSHManager
from .permissions import Actor
from .server_service import ServerService


class CommandDenied(Exception):
    """The command was refused by policy; ``reason`` is user-safe."""

    def __init__(self, reason: str, *, risk: str = "blocked", rule: str = "") -> None:
        self.reason = reason
        self.risk = risk
        self.rule = rule
        super().__init__(reason)


class ConfirmationError(Exception):
    """The confirmation token was invalid, expired, replayed, or foreign."""


@dataclass(frozen=True, slots=True)
class PendingCommand:
    token: str
    account_id: int
    owner_id: int
    server_id: int
    alias: str
    command: str
    risk: str
    reason: str
    created_at: float
    expires_at: float

    @property
    def fingerprint(self) -> str:
        """SHA-256 binding of (account, server, command) — replay/modification guard."""
        material = f"{self.account_id}\0{self.server_id}\0{self.command}".encode()
        return hashlib.sha256(material).hexdigest()


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    """What the adapter should do next after :meth:`CommandService.request`."""

    kind: str            # 'executed' | 'confirm' | 'denied'
    job: Job | None = None
    pending: PendingCommand | None = None
    reason: str = ""
    risk: str = ""
    rule: str = ""


@dataclass
class CommandService:
    queue: JobQueue
    manager: SSHManager
    servers: ServerService
    confirmation_ttl: float = CONFIRMATION_TTL
    _pending: dict[str, PendingCommand] = field(default_factory=dict, repr=False)
    _actor_pending: dict[int, str] = field(default_factory=dict, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    # -- policy -----------------------------------------------------------------
    def _engine(self, alias: str) -> PolicyEngine:
        """PolicyEngine for the server's profile (STANDARD when unconfigured)."""
        profiles: dict[str, Profile] = {}
        try:
            extra = getattr(app_config, "extra", {})
            profiles = load_profiles(data=extra.get("command_profiles")) if extra else {}
        except Exception:  # config not available in some test contexts
            profiles = {}
        profile = profile_for(alias, profiles, Profile.STANDARD)
        return PolicyEngine(profile=profile)

    # -- confirmations ------------------------------------------------------------
    def _create_confirmation(
        self, actor: Actor, server_id: int, alias: str, command: str, reason: str
    ) -> PendingCommand:
        with self._lock:
            # One pending confirmation per actor: a new request supersedes the old.
            previous = self._actor_pending.pop(actor.account_id, None)
            if previous is not None:
                self._pending.pop(previous, None)
            now = time.time()
            pending = PendingCommand(
                token=secrets.token_urlsafe(24),
                account_id=actor.account_id,
                owner_id=actor.owner_id,
                server_id=server_id,
                alias=alias,
                command=command,
                risk="confirm",
                reason=reason,
                created_at=now,
                expires_at=now + self.confirmation_ttl,
            )
            self._pending[pending.token] = pending
            self._actor_pending[actor.account_id] = pending.token
        return pending

    def peek_confirmation(self, actor: Actor, token: str) -> PendingCommand | None:
        with self._lock:
            pending = self._pending.get(token)
        if pending is None or pending.account_id != actor.account_id:
            return None
        if time.time() >= pending.expires_at:
            self.cancel_confirmation(actor, token)
            return None
        return pending

    def cancel_confirmation(self, actor: Actor, token: str) -> bool:
        with self._lock:
            pending = self._pending.get(token)
            if pending is None or pending.account_id != actor.account_id:
                return False
            self._pending.pop(token, None)
            if self._actor_pending.get(actor.account_id) == token:
                self._actor_pending.pop(actor.account_id, None)
        return True

    def sweep(self) -> int:
        """Drops expired confirmations; returns how many were removed."""
        now = time.time()
        removed = 0
        with self._lock:
            for token in [t for t, p in self._pending.items() if now >= p.expires_at]:
                pending = self._pending.pop(token)
                if self._actor_pending.get(pending.account_id) == token:
                    self._actor_pending.pop(pending.account_id, None)
                removed += 1
        return removed

    # -- execution -----------------------------------------------------------------
    async def request(self, actor: Actor, server_id: int, command: str) -> ExecutionPlan:
        """Classify + execute or stage for confirmation. Never raises for policy outcomes."""
        actor.require("commands.execute")
        try:
            command = validate_command_text(command)
        except ValueError as exc:
            raise CommandDenied(str(exc)) from exc

        try:
            self.servers.get_server(actor, server_id)  # ownership + servers.read
        except LookupError as exc:
            raise CommandDenied("server not found") from exc
        server = self.servers.get_server(actor, server_id)
        alias = str(server["alias"])

        decision = self._engine(alias).evaluate(command, alias=alias)
        assessment = classify(command)

        if not decision.allowed and not decision.requires_confirmation:
            audit_record("command.denied", alias=alias, rule=decision.rule,
                         account_id=actor.account_id, kind=actor.kind)
            METRICS.inc("commands_denied_total")
            return ExecutionPlan(kind="denied", reason=decision.explanation or decision.reason,
                                 risk="blocked", rule=decision.rule)
        if assessment.blocked or decision.risk == "blocked":
            audit_record("command.blocked", alias=alias, rule=assessment.rule,
                         command_hash=hashlib.sha256(command.encode()).hexdigest(),
                         account_id=actor.account_id, kind=actor.kind)
            METRICS.inc("commands_blocked_total")
            return ExecutionPlan(kind="denied",
                                 reason=assessment.reason or "command is blocked by policy",
                                 risk="blocked", rule=assessment.rule)

        needs_confirmation = (
            assessment.needs_confirmation or decision.requires_confirmation
        )
        if needs_confirmation:
            pending = self._create_confirmation(actor, server_id, alias, command,
                                                assessment.reason)
            audit_record("command.confirmation_created", alias=alias,
                         account_id=actor.account_id, kind=actor.kind)
            return ExecutionPlan(kind="confirm", pending=pending,
                                 risk="confirm", rule=assessment.rule)

        job = await self._enqueue(actor, server_id, alias, command)
        return ExecutionPlan(kind="executed", job=job, risk=assessment.risk.value,
                             rule=assessment.rule)

    async def confirm(self, actor: Actor, token: str) -> Job:
        """Single-use, TTL-checked, binding-checked confirmation → queued job."""
        actor.require("commands.confirm")
        with self._lock:
            pending = self._pending.get(token)
        if pending is None or pending.account_id != actor.account_id:
            raise ConfirmationError("invalid or unknown confirmation token")
        with self._lock:
            self._pending.pop(token, None)
            if self._actor_pending.get(actor.account_id) == token:
                self._actor_pending.pop(actor.account_id, None)
        if time.time() >= pending.expires_at:
            raise ConfirmationError("confirmation token has expired")
        # Re-verify ownership at confirmation time (the server may have moved).
        try:
            self.servers.get_server(actor, pending.server_id)
        except LookupError as exc:
            raise ConfirmationError("the target server no longer exists") from exc
        job = await self._enqueue(actor, pending.server_id, pending.alias, pending.command)
        audit_record("command.confirmed", alias=pending.alias,
                     account_id=actor.account_id, kind=actor.kind)
        return job

    async def _enqueue(self, actor: Actor, server_id: int, alias: str, command: str) -> Job:
        spec = JobSpec(
            user_id=actor.owner_id,
            alias=alias,
            command=command,
            label=f"{actor.kind}:{actor.display_name}",
            metadata={"account_id": actor.account_id, "server_id": server_id},
        )

        async def runner(job_spec: JobSpec) -> str:
            stdout, stderr, _status = await self.manager.execute(
                job_spec.user_id, job_spec.alias, job_spec.command
            )
            return stdout + (f"\n[stderr]\n{stderr}" if stderr else "")

        try:
            job = await self.queue.submit_nowait(spec, runner)
        except QueueFull as exc:
            raise CommandDenied(str(exc)) from exc
        audit_record("command.queued", alias=alias, job_id=job.id,
                     account_id=actor.account_id, kind=actor.kind)
        METRICS.inc("commands_executed_total")
        # Result auditing happens off the caller's request path: the adapter
        # already has the job handle and polls / streams the outcome.
        asyncio.create_task(self._audit_result(job))
        return job

    async def _audit_result(self, job: Job) -> None:
        future = self.queue._futures.get(job.id)  # noqa: SLF001 - completion waiter
        if future is None:
            return
        try:
            result = await asyncio.shield(future)
        except Exception:
            return
        audit_record("command.finished", alias=job.spec.alias, job_id=job.id,
                     exit_ok=result.exit_ok)

    # -- inspection -----------------------------------------------------------------
    def list_jobs(self, actor: Actor, *, only_own: bool = True) -> list[Job]:
        actor.require("jobs.read")
        owner = actor.owner_id if only_own else None
        return [*self.queue.active(owner), *self.queue.list_pending(owner)]

    def get_job(self, actor: Actor, job_id: str) -> Job | None:
        actor.require("jobs.read")
        for job in self.queue.active(actor.owner_id) + self.queue.list_pending(actor.owner_id):
            if job.id == job_id:
                return job
        # Admins may inspect any job.
        if actor.role in ("owner", "admin"):
            for job in self.queue.active() + self.queue.list_pending():
                if job.id == job_id:
                    return job
        return None

    async def cancel_job(self, actor: Actor, job_id: str) -> bool:
        actor.require("jobs.cancel")
        job = self.get_job(actor, job_id)
        if job is None:
            return False
        if job.spec.user_id != actor.owner_id and actor.role not in ("owner", "admin"):
            audit_record("security.denied", action="jobs.cancel", job_id=job_id,
                         account_id=actor.account_id)
            return False
        cancelled = self.queue.cancel(actor.owner_id, job_id)
        if not cancelled and actor.role in ("owner", "admin"):
            cancelled = self.queue.cancel(job.spec.user_id, job_id)
        if cancelled:
            audit_record("job.cancelled", job_id=job_id, account_id=actor.account_id)
        return cancelled

    def output_of(self, job: Job, result_output: str = "") -> str:
        """Best-effort output; finished results are delivered by the awaiting caller."""
        return result_output
