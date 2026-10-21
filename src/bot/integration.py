"""Purpose: wire the 3.2 subsystems (runtime, jobs, policy, observability) in.

The remaster added four self-contained packages — ``src/runtime`` (local and
SSH execution targets behind one contract), ``src/jobs`` (bounded, per-user
serialized queue), ``src/policy`` (operator authority profiles over the risk
classifier) and ``src/observability`` (structured logs, correlation ids,
redaction). Each of them is unit-tested in isolation, which is not the same as
being used: this module is the single place where they meet the handlers, so a
handler never imports a subsystem directly and there is exactly one wiring
story to read.

OWNER: src/bot/integration.py.
CONSUMES: src/runtime, src/jobs, src/policy, src/observability,
  src/bot/state.py, src/metrics.py.
PROVIDES: install_subsystems(), run_target(), submit_job(), check_policy(),
  observe(), shutdown_subsystems().
INVARIANTS:
  * Every accessor degrades to the 3.1 behaviour when its subsystem is absent
    (``state.target_resolver is None`` ⇒ fall back to ``ssh_manager``), so the
    bot is never harder to run than it was before.
  * Nothing here imports a handler module, and importing this module performs
    no I/O and reads no configuration.
  * A policy denial raises ``PolicyDenied``; callers translate it to a user
    message. Execution errors are never re-wrapped as policy denials.
EXTEND: wire a new subsystem by adding its builder to install_subsystems() and
  a thin accessor below; keep handler-facing signatures stable.
"""

from __future__ import annotations

import logging
from typing import Any

from ..metrics import METRICS
from . import state

logger = logging.getLogger(__name__)

#: Reserved alias meaning "the machine hosting the bot" (see src.runtime).
LOCAL_ALIAS = "self"


class PolicyDenied(RuntimeError):
    """Raised when an operator profile forbids a requested command.

    Carries the engine's decision so a handler can show the reason without
    re-running the check.
    """

    def __init__(self, message: str, decision: Any = None) -> None:
        super().__init__(message)
        self.decision = decision


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------
def _job_metric(name: str, fields: Any) -> None:
    """Bridges the queue's ``on_metric`` callback to the metrics registry.

    Kept as a named function (not a lambda with ``**fields``) so mypy can see
    that no label is named ``value`` and the counter is always incremented by 1.
    """
    labels = {str(key): value for key, value in dict(fields or {}).items()}
    labels.pop("value", None)
    METRICS.inc(f"jobs_{name}_total", **labels)


def install_subsystems(*, manager: Any = None, local_enabled: bool = True) -> None:
    """Builds and publishes the 3.2 subsystems on ``state``.

    ``local_enabled`` defaults to True: managing the host the bot runs on is the
    headline 3.2 feature ("self VPS"), and it stays opt-out for deployments that
    do not want the bot touching its own machine.
    """
    # --- runtime: one contract for "run here" and "run over SSH" -----------
    try:
        from ..runtime import TargetResolver

        state.target_resolver = TargetResolver(
            manager if manager is not None else state.ssh_manager,
            local_enabled=local_enabled,
        )
        logger.info("runtime: resolver ready (local_enabled=%s)", local_enabled)
    except Exception as exc:  # pragma: no cover - defensive
        state.target_resolver = None
        logger.warning("runtime: resolver unavailable (%s); using ssh_manager directly", exc)

    # --- jobs: bounded queue, per-user serialization ------------------------
    try:
        from ..jobs import JobQueue

        state.job_queue = JobQueue(
            max_concurrent=4,
            max_pending_per_user=8,
            max_pending_total=64,
            on_metric=_job_metric,
        )
        logger.info("jobs: queue ready (max_concurrent=4)")
    except Exception as exc:  # pragma: no cover - defensive
        state.job_queue = None
        logger.warning("jobs: queue unavailable (%s); commands run inline", exc)

    # --- policy: operator profiles over the risk classifier ----------------
    try:
        from ..config import config
        from ..policy import load_profiles

        # The engine is per-profile (one bounded authority set at a time), so the
        # alias -> profile map is what we publish; check_policy() caches the
        # engine it needs per alias.
        state.policy_profiles = load_profiles(
            None, data=getattr(config, "extra", None) or {}
        )
        state.policy_engine = None
        if state.policy_profiles:
            logger.info("policy: %d operator profile(s) loaded", len(state.policy_profiles))
        else:
            logger.info("policy: no per-alias profiles configured (risk classifier only)")
    except Exception as exc:
        state.policy_profiles = {}
        state.policy_engine = None
        logger.warning("policy: engine unavailable (%s); risk classifier only", exc)


async def start_subsystems() -> None:
    """Starts the async parts (the job queue). Safe to call twice."""
    queue = state.job_queue
    if queue is not None:
        await queue.start()


async def shutdown_subsystems() -> None:
    """Stops the job queue within its grace period. Never raises."""
    queue = state.job_queue
    if queue is not None:
        try:
            await queue.stop()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("jobs: queue did not stop cleanly (%s)", exc)
    state.job_queue = None


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
def run_target(user_id: int, alias: str):
    """Returns the execution target for ``alias`` as an async iterator factory.

    Falls back to the raw ``state.ssh_manager`` when the runtime package could
    not be installed, so a 3.1 deployment keeps working unchanged.
    """
    resolver = state.target_resolver
    if resolver is not None:
        target = resolver.resolve(user_id, alias)
        return target.run
    manager = state.ssh_manager
    if manager is None:
        raise RuntimeError("No SSH manager configured and no runtime resolver available.")
    return lambda command, **kwargs: manager.run_command(user_id, alias, command, **kwargs)


def is_local(alias: str) -> bool:
    """True when ``alias`` refers to the bot host rather than a remote server."""
    return str(alias or "").strip().lower() == LOCAL_ALIAS


def local_available() -> bool:
    """True when the runtime resolver exists and permits local execution."""
    resolver = state.target_resolver
    return resolver is not None and getattr(resolver, "local_enabled", False)


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------
def check_policy(user_id: int, alias: str, command: str) -> Any:
    """Raises :class:`PolicyDenied` when an operator profile forbids *command*.

    The engine is optional: with no profiles configured this is a no-op and the
    risk classifier in ``src.command_policy`` remains the only gate (3.1
    behaviour). A denial is logged and counted so operators can see attempts.
    """
    decision = _evaluate_policy(alias, command)
    if decision is None:
        return None
    if not getattr(decision, "allowed", True):
        METRICS.inc("commands_policy_denied_total")
        observe(
            "command.policy_denied",
            level=logging.WARNING,
            user_id=user_id,
            alias=alias,
            command=command,
            reason=str(getattr(decision, "reason", "") or ""),
        )
        raise PolicyDenied(
            str(getattr(decision, "reason", "") or "Command not permitted by your profile."),
            decision,
        )
    return decision


def _evaluate_policy(alias: str, command: str) -> Any:
    """Returns the profile verdict for *alias*, or None when unconfigured.

    An explicitly patched ``state.policy_engine`` (tests, embedders) always
    wins; otherwise the engine is built from the alias's profile and cached,
    because constructing one compiles deny patterns and that must not happen
    per keystroke.
    """
    engine = state.policy_engine
    if engine is not None:
        return engine.evaluate(command, alias=str(alias or ""))

    profiles = getattr(state, "policy_profiles", None)
    if not profiles:
        return None

    from ..policy import PolicyEngine, profile_for

    profile = profile_for(alias, profiles)
    cached = state.policy_engines.get(profile)
    if cached is None:
        cached = PolicyEngine(profile)
        state.policy_engines[profile] = cached
    return cached.evaluate(command, alias=str(alias or ""))


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------
def observe(event: str, *, level: int = logging.INFO, **fields: object) -> None:
    """Emits one structured log record, degrading to a plain log when absent."""
    try:
        from ..observability import log_event

        log_event(logger, event, level=level, **fields)
    except Exception:  # pragma: no cover - observability must never break a handler
        logger.log(level, "%s %s", event, fields)


def configure_observability(*, json_file: bool = True, log_file: str | None = None) -> None:
    """Installs the structured formatter; a no-op if the package is unavailable."""
    try:
        from ..observability import configure_logging as _configure

        _configure(json_file=json_file, log_file=log_file)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("observability: configure_logging unavailable (%s)", exc)


__all__ = [
    "LOCAL_ALIAS",
    "PolicyDenied",
    "check_policy",
    "configure_observability",
    "install_subsystems",
    "is_local",
    "local_available",
    "observe",
    "run_target",
    "shutdown_subsystems",
    "start_subsystems",
]
