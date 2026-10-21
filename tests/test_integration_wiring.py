"""End-to-end proof that the 3.2 subsystems are wired into the live bot.

The lane tests prove each package in isolation (``test_runtime_*``,
``test_jobs_*``, ``test_policy_*``, ``test_observability_*``). This module
proves the OTHER half — that a Telegram handler actually reaches them:

* ``install_subsystems`` publishes the resolver, queue and profiles on ``state``;
* ``execute_command`` routes a "self" alias to the local target and a normal
  alias to the SSH manager, and renders the real output;
* a policy denial blocks the command before anything executes;
* the observability layer emits a structured record with the handler's fields.

Everything here drives the real handler functions from ``src.bot.commands``
with mocked Telegram objects and a real ``LocalTarget`` running a real
subprocess, so a regression in the wiring fails here and not in production.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.bot import integration, state
from src.bot.commands import execute_command
from src.runtime import LOCAL_ALIAS, TargetError, TargetResolver


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _update(text: str = "echo hello", user_id: int = 12345):
    """Builds the minimal PTB Update ``execute_command`` reads."""
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = user_id
    update.message.text = text
    update.message.chat = MagicMock()
    update.message.chat.id = user_id
    return update


class _Recorder:
    """Captures every message the handler renders, in order.

    The handler funnels all user-visible text through ``_send_message_safely``
    and ``_edit_message_safely``, so recording those two is both sufficient and
    far more stable than reverse-engineering Telegram stub call graphs.
    """

    def __init__(self) -> None:
        self.texts: list[str] = []

    async def send(self, chat, text, *args, **kwargs):
        self.texts.append(str(text))
        return MagicMock()

    async def edit(self, message, text, *args, **kwargs):
        self.texts.append(str(text))
        return MagicMock()

    def joined(self) -> str:
        return "\n".join(self.texts)


@pytest.fixture
def recorder(monkeypatch):
    """Redirects the handler's rendering helpers into a recorder."""
    rec = _Recorder()
    monkeypatch.setattr("src.bot.commands._send_message_safely", rec.send)
    monkeypatch.setattr("src.bot.commands._edit_message_safely", rec.edit)
    return rec


@pytest.fixture(autouse=True)
def _clean_subsystems():
    """Every test starts with no subsystems and leaves none behind."""
    saved = (
        state.ssh_manager,
        state.target_resolver,
        state.job_queue,
        state.policy_engine,
        getattr(state, "policy_profiles", {}),
        getattr(state, "policy_engines", {}),
    )
    state.target_resolver = None
    state.job_queue = None
    state.policy_engine = None
    state.policy_profiles = {}
    state.policy_engines = {}
    state.ssh_manager = MagicMock()
    state.user_connections[12345] = LOCAL_ALIAS
    yield
    (
        state.ssh_manager,
        state.target_resolver,
        state.job_queue,
        state.policy_engine,
        state.policy_profiles,
        state.policy_engines,
    ) = saved
    state.user_connections.pop(12345, None)


# ---------------------------------------------------------------------------
# install_subsystems
# ---------------------------------------------------------------------------
async def test_install_subsystems_publishes_every_subsystem():
    """Startup wiring leaves a usable resolver, queue and (empty) policy map."""
    integration.install_subsystems(manager=state.ssh_manager, local_enabled=True)

    assert isinstance(state.target_resolver, TargetResolver)
    assert state.target_resolver.local_enabled is True
    assert state.job_queue is not None
    assert state.policy_profiles == {}  # no profiles configured in a bare env


async def test_local_execution_can_be_disabled():
    """TLA_LOCAL_EXECUTION=0 equivalent: the resolver refuses 'self'."""
    integration.install_subsystems(manager=state.ssh_manager, local_enabled=False)

    assert state.target_resolver.local_enabled is False
    with pytest.raises(TargetError):
        state.target_resolver.resolve(12345, LOCAL_ALIAS)


async def test_shutdown_is_idempotent_and_never_raises():
    """Shutdown runs twice without complaint and clears the queue handle."""
    integration.install_subsystems(manager=state.ssh_manager, local_enabled=True)
    await integration.start_subsystems()

    await integration.shutdown_subsystems()
    await integration.shutdown_subsystems()

    assert state.job_queue is None


# ---------------------------------------------------------------------------
# Local execution through the real handler
# ---------------------------------------------------------------------------
async def test_handler_runs_a_command_on_the_bot_host(recorder):
    """The headline 3.2 feature: manage the machine the bot runs on."""
    state.target_resolver = TargetResolver(state.ssh_manager, local_enabled=True)
    state.user_connections[12345] = LOCAL_ALIAS
    update = _update("echo 3.2-local-works")

    await execute_command(update, MagicMock())

    shown = recorder.joined()
    assert "3.2-local-works" in shown
    assert "Command completed" in shown


async def test_local_command_failure_is_reported_not_raised(recorder):
    """A non-zero exit surfaces as output, never as an exception to PTB."""
    state.target_resolver = TargetResolver(state.ssh_manager, local_enabled=True)
    state.user_connections[12345] = LOCAL_ALIAS
    update = _update("exit 3")

    await execute_command(update, MagicMock())

    assert recorder.joined()  # the user got a message either way


async def test_remote_alias_still_goes_over_ssh(recorder):
    """A normal alias is unchanged: it delegates to the SSH manager."""

    async def fake_run(user_id, alias, command, **kwargs):
        assert alias == "prod-web"
        yield "1234", "pid"
        yield "remote-output", "stdout"

    state.ssh_manager.run_command = fake_run
    state.target_resolver = TargetResolver(state.ssh_manager, local_enabled=True)
    state.user_connections[12345] = "prod-web"
    update = _update("uptime")

    await execute_command(update, MagicMock())

    assert "remote-output" in recorder.joined()


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------
async def test_policy_denial_blocks_before_execution(recorder):
    """A restrictive profile stops the command and tells the operator why."""
    from src.policy import PolicyEngine, Profile

    state.policy_engine = PolicyEngine(Profile.READONLY)
    state.user_connections[12345] = LOCAL_ALIAS
    update = _update("rm -rf /var/tmp/not-allowed")

    await execute_command(update, MagicMock())

    shown = recorder.joined().lower()
    assert "not permitted" in shown or "blocked" in shown or "read-only" in shown


async def test_policy_allow_lets_a_read_only_command_through(recorder):
    """The same engine permits a harmless read."""
    from src.policy import PolicyEngine, Profile

    state.policy_engine = PolicyEngine(Profile.READONLY)
    state.target_resolver = TargetResolver(state.ssh_manager, local_enabled=True)
    state.user_connections[12345] = LOCAL_ALIAS
    update = _update("echo allowed")

    await execute_command(update, MagicMock())

    assert "allowed" in recorder.joined()


async def test_check_policy_is_a_noop_without_profiles():
    """No profiles configured ⇒ None, and the 3.1 classifier stays in charge."""
    state.policy_engine = None
    state.policy_profiles = {}

    assert integration.check_policy(12345, LOCAL_ALIAS, "echo hi") is None


async def test_check_policy_counts_a_denial():
    """A denial increments the metric operators watch."""
    from src.metrics import METRICS
    from src.policy import PolicyEngine, Profile

    state.policy_engine = PolicyEngine(Profile.LOCKED)
    before = METRICS.get("commands_policy_denied_total")

    with pytest.raises(integration.PolicyDenied):
        integration.check_policy(12345, LOCAL_ALIAS, "echo hi")

    assert METRICS.get("commands_policy_denied_total") == before + 1


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------
async def test_observe_emits_the_event_and_its_fields(caplog):
    """observe() puts the event and fields on the record for the JSON formatter."""
    with caplog.at_level(logging.INFO, logger="src.bot.integration"):
        integration.observe("command.test", user_id=42, alias="self")

    record = caplog.records[-1]
    assert record.ctx_event == "command.test"
    assert record.ctx_user_id == 42
    assert record.ctx_alias == "self"


async def test_observe_never_raises_when_observability_is_missing():
    """A broken observability layer must not take a command down with it."""
    with patch.dict(sys.modules, {"src.observability": None}):
        integration.observe("command.test", user_id=1)  # must not raise


async def test_redaction_keeps_tokens_out_of_the_log_file(tmp_path):
    """A bot token in a command never reaches the JSON log."""
    from src.observability import configure_logging

    log_file = tmp_path / "bot.jsonl"
    configure_logging(json_file=True, log_file=log_file, force=True)
    try:
        integration.observe("command.executed", command="curl -H 'token 123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11'")
        for handler in logging.getLogger().handlers:
            handler.flush()
    finally:
        configure_logging(force=True)

    text = log_file.read_text(encoding="utf-8")
    payload = json.loads(text.strip().splitlines()[-1])
    assert "ABC-DEF1234ghIkl-zyx57W2v1u123ew11" not in text
    assert payload["event"] == "command.executed"


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
async def test_job_queue_executes_work_and_returns_its_result():
    """The queue built at startup really runs a runner and resolves."""
    from src.jobs import JobQueue, JobSpec

    queue = JobQueue(max_concurrent=2)
    await queue.start()

    async def runner(spec: JobSpec) -> str:
        return f"ran {spec.command}"

    try:
        result = await queue.submit(
            JobSpec(user_id=12345, alias=LOCAL_ALIAS, command="uptime"), runner
        )
        assert result.exit_ok is True
        assert "ran uptime" in result.output
    finally:
        await queue.stop()
