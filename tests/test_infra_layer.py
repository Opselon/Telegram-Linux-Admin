"""Tests for destination policy, audit log, metrics registry and health server."""

from __future__ import annotations

import asyncio
import json

import pytest

from src.audit import AuditLogger
from src.destination_policy import (
    ALLOW_ALL,
    ALLOWLIST,
    DENY_PUBLIC,
    DestinationDenied,
    DestinationPolicy,
)
from src.health import start_health_server, stop_health_server
from src.metrics import Metrics


# ---------------------------------------------------------------------------
# Destination policy
# ---------------------------------------------------------------------------
def _resolver(mapping):
    return lambda host: mapping.get(host, [])


class TestDestinationPolicy:
    def test_allow_all_accepts_public_hosts(self):
        policy = DestinationPolicy(ALLOW_ALL, resolver=_resolver({"example.com": ["93.184.216.34"]}))
        assert policy.evaluate("example.com").allowed is True

    def test_metadata_endpoint_always_blocked(self):
        policy = DestinationPolicy(ALLOW_ALL, resolver=_resolver({"meta": ["169.254.169.254"]}))
        decision = policy.evaluate("meta")
        assert decision.allowed is False
        assert "metadata" in decision.reason
        with pytest.raises(DestinationDenied):
            policy.check("meta")

    def test_deny_public_blocks_internet_but_allows_lan(self):
        policy = DestinationPolicy(
            DENY_PUBLIC,
            resolver=_resolver({"lan": ["192.168.1.10"], "wan": ["8.8.8.8"], "v6": ["fd00::5"]}),
        )
        assert policy.evaluate("lan").allowed is True
        assert policy.evaluate("v6").allowed is True
        assert policy.evaluate("wan").allowed is False

    def test_allowlist_supports_cidr_hostname_and_wildcard(self):
        policy = DestinationPolicy(
            ALLOWLIST,
            allowlist=("10.0.0.0/24", "db.internal", "*.example.com"),
            resolver=_resolver(
                {
                    "10.0.0.7": ["10.0.0.7"],
                    "db.internal": ["10.9.9.9"],
                    "api.example.com": ["203.0.113.5"],
                    "evil.com": ["203.0.113.99"],
                }
            ),
        )
        assert policy.evaluate("10.0.0.7").allowed is True
        assert policy.evaluate("db.internal").allowed is True
        assert policy.evaluate("api.example.com").allowed is True
        assert policy.evaluate("evil.com").allowed is False

    def test_unresolvable_host_is_denied(self):
        policy = DestinationPolicy(ALLOW_ALL, resolver=_resolver({}))
        decision = policy.evaluate("does-not-exist.invalid")
        assert decision.allowed is False
        assert "unresolvable" in decision.reason

    def test_reconfigure_switches_mode_at_runtime(self):
        policy = DestinationPolicy(ALLOW_ALL, resolver=_resolver({"wan": ["8.8.8.8"]}))
        policy.reconfigure(DENY_PUBLIC)
        assert policy.evaluate("wan").allowed is False
        policy.reconfigure(ALLOWLIST, ("8.8.8.0/24",))
        assert policy.evaluate("wan").allowed is True

    def test_ipv4_mapped_ipv6_is_treated_as_private(self):
        policy = DestinationPolicy(DENY_PUBLIC, resolver=_resolver({"x": ["::ffff:192.168.0.1"]}))
        assert policy.evaluate("x").allowed is True


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------
class TestAuditLogger:
    def test_records_and_tails(self, tmp_path):
        logger = AuditLogger(tmp_path, max_bytes=10_000)
        logger.record("command.executed", user_id=1, alias="web", command="uptime")
        logger.record("access_denied", user_id=2, outcome="denied", reason="not whitelisted")

        records = logger.tail(10)
        assert [r["event"] for r in records] == ["command.executed", "access_denied"]
        assert records[0]["user_id"] == 1
        assert records[0]["alias"] == "web"
        assert records[1]["outcome"] == "denied"
        assert records[1]["context"]["reason"] == "not whitelisted"
        assert records[0]["timestamp"].endswith("Z")

    def test_rotation_keeps_bounded_files(self, tmp_path):
        logger = AuditLogger(tmp_path, max_bytes=64 * 1024, keep_files=2)
        for index in range(3000):
            logger.record("noise", user_id=1, index=index)
        files = sorted(p.name for p in tmp_path.iterdir())
        assert files == ["audit.log", "audit.log.1", "audit.log.2"]

    def test_disabled_logger_writes_nothing(self, tmp_path):
        logger = AuditLogger(tmp_path)
        logger.set_enabled(False)
        record = logger.record("event", user_id=1)
        assert record["event"] == "event"
        assert not (tmp_path / "audit.log").exists()

    def test_counts_per_event(self, tmp_path):
        logger = AuditLogger(tmp_path)
        logger.record("a", user_id=1)
        logger.record("a", user_id=2)
        logger.record("b", user_id=3)
        assert logger.counts() == {"a": 2, "b": 1}

    def test_errors_never_propagate(self, tmp_path, monkeypatch):
        logger = AuditLogger(tmp_path)

        def boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(logger, "_write", boom)
        record = logger.record("event", user_id=1)  # must not raise
        assert record["event"] == "event"


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
class TestMetrics:
    def test_counters_and_gauges_render(self):
        metrics = Metrics()
        metrics.inc("commands_total")
        metrics.inc("commands_total")
        metrics.inc("commands_total", rule="rm-root")
        metrics.set("ready", 1)
        text = metrics.render()
        assert "tla_commands_total 2.0" in text
        assert 'tla_commands_total{rule="rm-root"} 1.0' in text
        assert "tla_ready 1.0" in text
        assert metrics.get("commands_total") == 2.0

    def test_label_escaping(self):
        metrics = Metrics()
        metrics.inc("x_total", reason='evil"\nlabel')
        assert 'reason="evil\\"\\nlabel"' in metrics.render()

    def test_reset(self):
        metrics = Metrics()
        metrics.inc("x_total")
        metrics.reset()
        assert metrics.get("x_total") == 0.0


# ---------------------------------------------------------------------------
# Health endpoint (real sockets, real HTTP)
# ---------------------------------------------------------------------------
async def _get(port: int, path: str) -> tuple[int, str]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
    await writer.drain()
    raw = await reader.read(65536)
    writer.close()
    await writer.wait_closed()
    head, _, body = raw.partition(b"\r\n\r\n")
    status = int(head.split(b" ")[1])
    return status, body.decode()


@pytest.mark.asyncio
async def test_health_readyz_and_metrics():
    async def readiness():
        return True, {"database": True}

    server = await start_health_server(0, "127.0.0.1", readiness=readiness)
    port = server.sockets[0].getsockname()[1]
    try:
        status, body = await _get(port, "/healthz")
        assert status == 200 and json.loads(body)["status"] == "ok"

        status, body = await _get(port, "/readyz")
        assert status == 200 and json.loads(body)["database"] is True

        status, body = await _get(port, "/metrics")
        assert status == 200
        assert "tla_health_server_up 1.0" in body
        assert "tla_uptime_seconds" in body

        status, _ = await _get(port, "/nope")
        assert status == 404
    finally:
        await stop_health_server(server)


@pytest.mark.asyncio
async def test_readyz_reports_failure():
    async def readiness():
        return False, {"database": False}

    server = await start_health_server(0, "127.0.0.1", readiness=readiness)
    port = server.sockets[0].getsockname()[1]
    try:
        status, body = await _get(port, "/readyz")
        assert status == 503
        assert json.loads(body)["status"] == "not_ready"
    finally:
        await stop_health_server(server)
