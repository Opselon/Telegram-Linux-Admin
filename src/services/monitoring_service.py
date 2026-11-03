"""Monitoring service: bounded, cached resource snapshots per server.

Snapshots run at most every ``interval`` seconds per (owner, alias); concurrent
requests coalesce onto one refresh.  A 120-sample in-memory ring buffer gives
the dashboard short history without any new persistent tables.  Explicit
"refresh now" requests bypass the cooldown under a per-actor rate limit.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from ..access_control import rate_limiter
from ..audit import audit as _audit


def audit_record(event: str, **fields: Any) -> None:
    """Audit one security-relevant event (shared helper for all services)."""
    _audit.record(event, **fields)
from .operations_service import OperationError
from .permissions import Actor
from .server_service import ServerService

MAX_HISTORY = 120
DEFAULT_INTERVAL = 15.0


def _parse_meminfo(text: str) -> dict[str, float]:
    values: dict[str, float] = {}
    for line in text.splitlines():
        parts = line.split(":")
        if len(parts) != 2:
            continue
        try:
            values[parts[0].strip()] = float(parts[1].strip().split()[0])
        except (ValueError, IndexError):
            continue
    total = values.get("MemTotal", 0.0)
    available = values.get("MemAvailable", values.get("MemFree", 0.0))
    swap_total = values.get("SwapTotal", 0.0)
    swap_free = values.get("SwapFree", 0.0)
    used = max(0.0, total - available)
    return {
        "ram_total_mb": round(total / 1024, 1),
        "ram_used_mb": round(used / 1024, 1),
        "ram_percent": round(used / total * 100, 1) if total else 0.0,
        "swap_total_mb": round(swap_total / 1024, 1),
        "swap_percent": round((swap_total - swap_free) / swap_total * 100, 1) if swap_total else 0.0,
    }


def _parse_cpu(top: str) -> float:
    """Extracts idle% from a ``top -bn1`` %Cpu(s) line; returns 100-idle."""
    for line in top.splitlines():
        if not line.startswith("%Cpu"):
            continue
        # Line looks like: %Cpu(s):  1.9 us,  0.6 sy,  0.0 ni, 96.9 id,  0.5 wa, ...
        for segment in line.split(","):
            parts = segment.split()
            if len(parts) == 2 and parts[1] == "id,":
                try:
                    idle = float(parts[0])
                    return round(max(0.0, min(100.0, 100.0 - idle)), 1)
                except ValueError:
                    continue
    return 0.0


def _parse_df(text: str) -> list[dict[str, Any]]:
    filesystems: list[dict[str, Any]] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 6 and parts[0] != "Filesystem":
            try:
                filesystems.append({
                    "filesystem": parts[0],
                    "mount": parts[5],
                    "total_gb": round(float(parts[1].rstrip("G")) , 1) if parts[1].endswith("G") else parts[1],
                    "used_percent": float(parts[4].rstrip("%")),
                })
            except (ValueError, IndexError):
                continue
    return filesystems


def _parse_load(uptime: str) -> tuple[float, float, float]:
    try:
        head = uptime.split("load average:")[1].strip()
        one, five, fifteen = (float(x) for x in head.split(",")[:3])
        return one, five, fifteen
    except (IndexError, ValueError):
        return 0.0, 0.0, 0.0


@dataclass
class MonitoringService:
    servers: ServerService
    interval: float = DEFAULT_INTERVAL
    _cache: dict[tuple[int, str], dict[str, Any]] = field(default_factory=dict, repr=False)
    _history: dict[tuple[int, str], deque] = field(default_factory=dict, repr=False)
    _locks: dict[tuple[int, str], asyncio.Lock] = field(default_factory=dict, repr=False)
    _last: dict[tuple[int, str], float] = field(default_factory=dict, repr=False)

    async def snapshot(self, actor: Actor, server_id: int, *, force: bool = False) -> dict[str, Any]:
        """Returns the cached snapshot, refreshing when stale or forced."""
        actor.require("monitoring.read")
        if force:
            allowed, retry_after = rate_limiter.check(
                actor.account_id, "monitoring.force", max_calls=4, period=60.0
            )
            if not allowed:
                from .operations_service import OperationError as _Op
                raise _Op(f"refresh rate limit; retry in {retry_after:.0f}s")
        server = self.servers.get_server(actor, server_id)
        key = (actor.owner_id, str(server["alias"]))
        now = time.monotonic()
        if not force and key in self._last and now - self._last[key] < self.interval:
            cached = self._cache.get(key)
            if cached is not None:
                return cached
        lock = self._locks.setdefault(key, asyncio.Lock())
        if lock.locked():
            # Another refresh is in flight; serve the current value.
            return self._cache.get(key) or {"stale": True}
        async with lock:
            if not force and key in self._last and time.monotonic() - self._last[key] < self.interval:
                return self._cache[key]
            try:
                sample = await self._collect(actor, server_id)
            except Exception as exc:
                audit_record("monitoring.failed", server_id=server_id,
                             account_id=actor.account_id, error=str(exc)[:200])
                cached = self._cache.get(key)
                if cached is not None:
                    cached = dict(cached)
                    cached["stale"] = True
                    cached["error"] = str(exc)[:200]
                    return cached
                raise OperationError(str(exc)[:300]) from exc
            self._cache[key] = sample
            self._last[key] = time.monotonic()
            history = self._history.setdefault(key, deque(maxlen=MAX_HISTORY))
            history.append({
                "t": time.time(),
                "cpu": sample["cpu_percent"],
                "ram": sample["ram_percent"],
                "disk": sample["disks"][0]["used_percent"] if sample["disks"] else 0.0,
            })
            return sample

    async def _collect(self, actor: Actor, server_id: int) -> dict[str, Any]:
        stdout, _stderr, _exit = await self.servers.run_on_server(
            actor, server_id,
            "cat /proc/meminfo; echo '---TOP---'; top -bn1 | head -n 3; "
            "echo '---DF---'; df -h -x tmpfs -x devtmpfs; "
            "echo '---UP---'; uptime; "
            "echo '---OS---'; cat /etc/os-release 2>/dev/null | head -n 2; uname -r; "
            "echo '---NET---'; cat /proc/net/dev | tail -n +3 | head -n 8",
            timeout=30,
        )
        sections = stdout.split("---")
        meminfo = top = df = up = osinfo = net = ""
        for section in sections:
            label = section.split("\n", 1)[0].strip()
            body = section.split("\n", 1)[1] if "\n" in section else ""
            if label == "TOP---":
                top = body
            elif label == "DF---":
                df = body
            elif label == "UP---":
                up = body
            elif label == "OS---":
                osinfo = body
            elif label == "NET---":
                net = body

        mem = _parse_meminfo(meminfo)
        cpu = _parse_cpu(top)
        disks = _parse_df(df)
        load1, load5, load15 = _parse_load(up)
        os_name, kernel = "", ""
        os_lines = [l for l in osinfo.splitlines() if l.strip()]
        for line in os_lines:
            if line.startswith("PRETTY_NAME="):
                os_name = line.split("=", 1)[1].strip().strip('"')
        kernel_lines = [l for l in os_lines if not l.startswith("PRETTY_NAME=")]
        if kernel_lines:
            kernel = kernel_lines[-1].strip()
        interfaces = []
        for line in net.splitlines():
            fields = line.split()
            if len(fields) >= 16:
                interfaces.append({
                    "name": fields[0].rstrip(":"),
                    "rx_mb": round(float(fields[1]) / 1024 / 1024, 1),
                    "tx_mb": round(float(fields[9]) / 1024 / 1024, 1),
                })
        return {
            "cpu_percent": cpu,
            "load": {"1": load1, "5": load5, "15": load15},
            **mem,
            "disks": disks,
            "os": os_name,
            "kernel": kernel,
            "interfaces": interfaces,
            "sampled_at": time.time(),
        }

    def history(self, actor: Actor, server_id: int) -> list[dict[str, float]]:
        actor.require("monitoring.read")
        conn_server = self.servers.get_server(actor, server_id)
        return list(self._history.get((actor.owner_id, str(conn_server["alias"])), []))

    @property
    def cache_size(self) -> int:
        return len(self._cache)
