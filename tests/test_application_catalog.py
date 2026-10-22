"""Application-layer tests: catalog scoping and policy enforcement."""

from __future__ import annotations

from typing import Sequence

import pytest

from src.application.catalog import TargetCatalog
from src.application.commands import CommandService
from src.application.dto import Principal, TargetView
from src.domain.errors import TargetAccessDenied, TargetNotFound
from src.domain.policies import TargetAccessPolicy
from src.domain.targets import LOCAL_ALIAS, ServerTarget, TargetKind, local_target


class FakeRepository:
    """In-memory ServerRepository with strict owner scoping."""

    def __init__(self, rows: dict[int, dict[str, ServerTarget]] | None = None) -> None:
        self.rows = rows or {}

    def list_targets(self, owner_id: int) -> Sequence[ServerTarget]:
        return list(self.rows.get(owner_id, {}).values())

    def find(self, owner_id: int, alias: str) -> ServerTarget | None:
        return self.rows.get(owner_id, {}).get(alias)

    def exists(self, owner_id: int, alias: str) -> bool:
        return alias in self.rows.get(owner_id, {})

    def remove(self, owner_id: int, alias: str) -> bool:
        return self.rows.get(owner_id, {}).pop(alias, None) is not None

    def count(self, owner_id: int) -> int:
        return len(self.rows.get(owner_id, {}))


def ssh(alias: str, owner: int, host: str = "10.0.0.1") -> ServerTarget:
    return ServerTarget(alias=alias, kind=TargetKind.SSH, owner_id=owner, hostname=host, user="root")


def catalog(rows=None, *, local_enabled: bool = True) -> TargetCatalog:
    return TargetCatalog(FakeRepository(rows), TargetAccessPolicy(local_enabled=local_enabled))


def test_admin_sees_self_host_before_remote_servers():
    cat = catalog({1: {"web": ssh("web", 1)}})
    targets = cat.list_targets(Principal(1, is_admin=True))
    assert [t.alias for t in targets] == [LOCAL_ALIAS, "web"]


def test_regular_user_never_sees_self_host():
    cat = catalog({1: {"web": ssh("web", 1)}})
    targets = cat.list_targets(Principal(1, is_admin=False))
    assert [t.alias for t in targets] == ["web"]


def test_listing_is_scoped_to_the_owner():
    cat = catalog({1: {"web": ssh("web", 1)}, 2: {"db": ssh("db", 2)}})
    assert [t.alias for t in cat.list_targets(Principal(2))] == ["db"]
    assert cat.list_targets(Principal(3)) == []


def test_self_host_requires_admin_even_by_alias():
    cat = catalog()
    with pytest.raises(TargetAccessDenied):
        cat.find(Principal(1, is_admin=False), LOCAL_ALIAS)


def test_self_host_is_hidden_when_feature_disabled():
    cat = catalog(local_enabled=False)
    with pytest.raises(Exception) as info:
        cat.find(Principal(1, is_admin=True), LOCAL_ALIAS)
    assert info.value.__class__.__name__ == "TargetDisabled"
    assert cat.self_host_available(Principal(1, is_admin=True)) is False


def test_unknown_alias_raises_not_found_not_leak():
    cat = catalog({2: {"web": ssh("web", 2)}})
    with pytest.raises(TargetNotFound):
        cat.find(Principal(1, is_admin=True), "web")


def test_alias_availability_blocks_reserved_names():
    cat = catalog({1: {"web": ssh("web", 1)}})
    principal = Principal(1, is_admin=True)
    assert cat.alias_available(principal, "db") is True
    assert cat.alias_available(principal, "web") is False
    assert cat.alias_available(principal, LOCAL_ALIAS) is False


def test_views_never_expose_credentials():
    cat = catalog({1: {"web": ssh("web", 1)}})
    view = cat.list_views(Principal(1, is_admin=True))[1]
    assert isinstance(view, TargetView)
    assert not hasattr(view, "password")


def test_remove_self_host_is_rejected():
    cat = catalog()
    with pytest.raises(TargetNotFound):
        cat.remove(Principal(1, is_admin=True), LOCAL_ALIAS)


class FakeTransport:
    def __init__(self, chunks: Sequence[tuple[str, str]] = (("ok\n", "stdout"),)) -> None:
        self.chunks = chunks
        self.commands: list[tuple[str, str]] = []
        self.disconnected: list[str] = []

    async def run(self, target, command):  # noqa: ANN001 - protocol shape
        self.commands.append((target.alias, command))
        for chunk in self.chunks:
            yield chunk

    async def disconnect(self, target):
        self.disconnected.append(target.alias)


class Router:
    def __init__(self, ssh_transport: FakeTransport, local_transport: FakeTransport) -> None:
        self._ssh = ssh_transport
        self._local = local_transport

    def transport_for(self, target):
        return self._local if target.is_local else self._ssh


@pytest.mark.asyncio
async def test_command_service_routes_local_and_ssh_separately():
    cat = catalog({1: {"web": ssh("web", 1)}})
    ssh_transport, local_transport = FakeTransport(), FakeTransport((("local\n", "stdout"),))
    service = CommandService(cat, Router(ssh_transport, local_transport))

    assert await service.snapshot(Principal(1, is_admin=True), LOCAL_ALIAS, "uptime") == "local"
    assert await service.snapshot(Principal(1, is_admin=True), "web", "uptime") == "ok"

    assert local_transport.commands == [(LOCAL_ALIAS, "uptime")]
    assert ssh_transport.commands == [("web", "uptime")]


@pytest.mark.asyncio
async def test_command_service_denies_local_for_non_admin_before_any_execution():
    cat = catalog()
    ssh_transport, local_transport = FakeTransport(), FakeTransport()
    service = CommandService(cat, Router(ssh_transport, local_transport))

    with pytest.raises(TargetAccessDenied):
        await service.snapshot(Principal(1, is_admin=False), LOCAL_ALIAS, "id")
    assert local_transport.commands == []


@pytest.mark.asyncio
async def test_command_service_disconnect_calls_transport():
    cat = catalog()
    ssh_transport, local_transport = FakeTransport(), FakeTransport()
    service = CommandService(cat, Router(ssh_transport, local_transport))
    await service.disconnect(Principal(1, is_admin=True), LOCAL_ALIAS)
    assert local_transport.disconnected == [LOCAL_ALIAS]


def test_local_target_helper_matches_catalog():
    cat = catalog()
    assert cat.find(Principal(1, is_admin=True), LOCAL_ALIAS) == local_target(1)
