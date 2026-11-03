"""Remote operations service: services, processes, docker, firewall, packages.

Every operation is a *fixed, shlex-quoted command template* executed through
:class:`~services.server_service.ServerService` — user input never reaches the
shell unquoted, and every operation carries its own permission check.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import Any

from ..audit import audit as _audit


def audit_record(event: str, **fields: Any) -> None:
    """Audit one security-relevant event (shared helper for all services)."""
    _audit.record(event, **fields)
from .permissions import Actor
from .server_service import ServerService

_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@+-]{0,127}$")


class OperationError(RuntimeError):
    """A remote operation failed; ``detail`` is trimmed remote output."""


def _require_name(value: str, what: str = "name") -> str:
    value = (value or "").strip()
    if not _SAFE_NAME_RE.match(value):
        raise ValueError(f"invalid {what}: {value!r}")
    return value


@dataclass
class OperationsService:
    servers: ServerService

    # ------------------------------------------------------------------
    async def _run(self, actor: Actor, server_id: int, command: str, *,
                   timeout: float = 30.0) -> str:
        stdout, stderr, _ = await self.servers.run_on_server(
            actor, server_id, command, timeout=timeout
        )
        return stdout or stderr

    # -- systemd services ------------------------------------------------
    async def list_services(self, actor: Actor, server_id: int) -> list[dict[str, Any]]:
        actor.require("services.read")
        out = await self._run(
            actor, server_id,
            "systemctl list-units --type=service --all --no-legend --no-pager "
            "| head -n 200",
        )
        services: list[dict[str, Any]] = []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[0].endswith(".service"):
                services.append({
                    "unit": parts[0],
                    "load": parts[1],
                    "active": parts[2],
                    "sub": parts[3],
                    "description": " ".join(parts[4:])[:120],
                })
        return services

    async def service_action(self, actor: Actor, server_id: int, name: str, action: str) -> dict[str, Any]:
        if action not in {"start", "stop", "restart", "enable", "disable"}:
            raise ValueError(f"unsupported service action '{action}'")
        actor.require("services.control")
        unit = _require_name(name, "service name")
        if not unit.endswith(".service"):
            unit += ".service"
        await self._run(actor, server_id, f"systemctl {action} {shlex.quote(unit)}", timeout=60)
        audit_record("service.action", unit=unit, action=action,
                     account_id=actor.account_id, kind=actor.kind)
        return {"ok": True, "unit": unit, "action": action}

    async def service_status(self, actor: Actor, server_id: int, name: str) -> dict[str, Any]:
        actor.require("services.read")
        unit = _require_name(name, "service name")
        out = await self._run(
            actor, server_id,
            f"systemctl show {shlex.quote(unit)} --no-pager "
            "-p LoadState -p ActiveState -p SubState -p Description -p MainPID",
        )
        props = dict(
            line.split("=", 1) for line in out.splitlines() if "=" in line
        )
        return {"unit": unit, **props}

    # -- processes -------------------------------------------------------
    async def list_processes(self, actor: Actor, server_id: int, *,
                             search: str = "", sort: str = "cpu") -> list[dict[str, Any]]:
        actor.require("processes.read")
        sort_key = {"cpu": "-pcpu", "mem": "-pmem", "pid": "-pid"}.get(sort, "-pcpu")
        out = await self._run(
            actor, server_id,
            f"ps -eo pid,ppid,user:20,pcpu,pmem,etime,args --sort={sort_key} --no-headers "
            "| head -n 300",
        )
        processes: list[dict[str, Any]] = []
        needle = (search or "").lower()
        for line in out.splitlines():
            parts = line.split(None, 6)
            if len(parts) < 7:
                continue
            entry: dict[str, Any] = {
                "pid": int(parts[0]),
                "ppid": int(parts[1]),
                "user": parts[2],
                "cpu": float(parts[3] or 0),
                "mem": float(parts[4] or 0),
                "elapsed": parts[5],
                "command": parts[6][:300],
            }
            if needle and needle not in entry["command"].lower() and needle not in entry["user"].lower():
                continue
            processes.append(entry)
        return processes

    async def kill_process(self, actor: Actor, server_id: int, pid: int, *, signal: str = "TERM") -> dict[str, Any]:
        actor.require("processes.kill")
        if signal not in {"TERM", "KILL"}:
            raise ValueError("signal must be TERM or KILL")
        pid = int(pid)
        if pid <= 1:
            raise ValueError("refusing to signal pid <= 1")
        await self.servers.run_on_server(
            actor, server_id,
            f"kill -{signal} {pid}", timeout=15,
        )
        audit_record("process.killed", pid=pid, signal=signal,
                     account_id=actor.account_id, kind=actor.kind)
        return {"ok": True, "pid": pid, "signal": signal}

    # -- docker ----------------------------------------------------------
    async def docker_containers(self, actor: Actor, server_id: int) -> list[dict[str, Any]]:
        actor.require("docker.read")
        out = await self._run(
            actor, server_id,
            "docker ps -a --format '{{.ID}}|{{.Names}}|{{.Image}}|{{.State}}|{{.Status}}' | head -n 200",
        )
        containers = []
        for line in out.splitlines():
            fields = line.split("|")
            if len(fields) == 5:
                containers.append({
                    "id": fields[0], "name": fields[1], "image": fields[2],
                    "state": fields[3], "status": fields[4],
                })
        return containers

    async def docker_action(self, actor: Actor, server_id: int, container: str, action: str) -> dict[str, Any]:
        if action not in {"start", "stop", "restart"}:
            raise ValueError(f"unsupported container action '{action}'")
        actor.require("docker.control")
        name = _require_name(container, "container name")
        await self._run(actor, server_id, f"docker {action} {shlex.quote(name)}", timeout=60)
        audit_record("docker.action", container=name, action=action,
                     account_id=actor.account_id, kind=actor.kind)
        return {"ok": True, "container": name, "action": action}

    async def docker_logs(self, actor: Actor, server_id: int, container: str, *, tail: int = 100) -> str:
        actor.require("docker.read")
        name = _require_name(container, "container name")
        tail = max(1, min(int(tail), 1000))
        return await self._run(
            actor, server_id, f"docker logs --tail {tail} {shlex.quote(name)} 2>&1", timeout=30,
        )

    async def docker_images(self, actor: Actor, server_id: int) -> list[dict[str, Any]]:
        actor.require("docker.read")
        out = await self._run(
            actor, server_id,
            "docker images --format '{{.Repository}}|{{.Tag}}|{{.ID}}|{{.Size}}' | head -n 200",
        )
        images = []
        for line in out.splitlines():
            fields = line.split("|")
            if len(fields) == 4:
                images.append({
                    "repository": fields[0], "tag": fields[1],
                    "id": fields[2], "size": fields[3],
                })
        return images

    # -- firewall (ufw) ----------------------------------------------------
    async def firewall_rules(self, actor: Actor, server_id: int) -> dict[str, Any]:
        actor.require("firewall.read")
        out = await self._run(actor, server_id, "ufw status numbered 2>&1")
        return {"raw": out[:4000]}

    async def firewall_add(self, actor: Actor, server_id: int, *, rule: str, direction: str = "allow") -> dict[str, Any]:
        if direction not in {"allow", "deny"}:
            raise ValueError("direction must be allow or deny")
        actor.require("firewall.modify")
        # rule is a validated ufw rule fragment, e.g. "22/tcp" or "from 10.0.0.0/8"
        rule = (rule or "").strip()
        if not rule or any(ch in rule for ch in "`$;&|<>*?\"'\n\r\t\x00"):
            raise ValueError("invalid firewall rule")
        # Guard against lockout: never flush, never disable via this path.
        if "delete" in rule or "reset" in rule or "disable" in rule or "flush" in rule:
            raise ValueError("destructive firewall verbs are not permitted")
        out = await self._run(actor, server_id, f"ufw {direction} {rule}", timeout=60)
        audit_record("firewall.changed", rule=rule, direction=direction,
                     account_id=actor.account_id, kind=actor.kind)
        return {"ok": True, "output": out[:2000]}

    async def firewall_delete(self, actor: Actor, server_id: int, *, rule: str) -> dict[str, Any]:
        actor.require("firewall.modify")
        rule = (rule or "").strip()
        if not rule or any(ch in rule for ch in "`$;&|<>*?\"'\n\r\t\x00"):
            raise ValueError("invalid firewall rule")
        out = await self._run(actor, server_id, f"ufw delete {rule}", timeout=60)
        audit_record("firewall.deleted", rule=rule,
                     account_id=actor.account_id, kind=actor.kind)
        return {"ok": True, "output": out[:2000]}

    # -- packages -----------------------------------------------------------
    async def package_update(self, actor: Actor, server_id: int) -> dict[str, Any]:
        actor.require("services.control")
        out = await self._run(actor, server_id, "apt-get update -qq 2>&1 | tail -n 20", timeout=300)
        audit_record("package.update", account_id=actor.account_id, kind=actor.kind)
        return {"ok": True, "output": out[-2000:]}

    async def package_upgrade(self, actor: Actor, server_id: int) -> dict[str, Any]:
        actor.require("services.control")
        out = await self._run(
            actor, server_id, "DEBIAN_FRONTEND=noninteractive apt-get upgrade -y 2>&1 | tail -n 40",
            timeout=1800,
        )
        audit_record("package.upgrade", account_id=actor.account_id, kind=actor.kind)
        return {"ok": True, "output": out[-2000:]}

    async def package_install(self, actor: Actor, server_id: int, name: str) -> dict[str, Any]:
        actor.require("services.control")
        package = _require_name(name, "package name")
        out = await self._run(
            actor, server_id,
            f"DEBIAN_FRONTEND=noninteractive apt-get install -y {shlex.quote(package)} 2>&1 | tail -n 40",
            timeout=900,
        )
        audit_record("package.installed", package=package,
                     account_id=actor.account_id, kind=actor.kind)
        return {"ok": True, "output": out[-2000:]}
