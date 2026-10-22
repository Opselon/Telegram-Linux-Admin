"""Domain-layer tests: target entities and the self-host access policy."""

from __future__ import annotations

import pytest

from src.domain.errors import TargetAccessDenied, TargetDisabled, TargetNotFound
from src.domain.policies import TargetAccessPolicy, is_reserved_alias, local_target_enabled
from src.domain.targets import LOCAL_ALIAS, ServerTarget, TargetKind, local_target, ssh_target_from_row


def test_local_target_is_reserved_and_local():
    target = local_target(7)
    assert target.alias == LOCAL_ALIAS
    assert target.kind is TargetKind.LOCAL
    assert target.is_local
    assert target.for_owner(7)
    assert not target.for_owner(8)


def test_ssh_row_projection_keeps_owner_and_credential_flag():
    target = ssh_target_from_row(
        {"alias": "web", "owner_id": 3, "hostname": "10.0.0.5", "user": "root", "password": "x"}
    )
    assert target.kind is TargetKind.SSH
    assert target.has_credentials
    assert target.for_owner(3) and not target.for_owner(4)


@pytest.mark.parametrize("alias", ["self-host", "SELF-HOST", " self-host "])
def test_reserved_alias_is_case_and_space_insensitive(alias):
    assert is_reserved_alias(alias)


def test_reserved_alias_does_not_match_similar_names():
    assert not is_reserved_alias("self-hosted")
    assert not is_reserved_alias("host")


def test_policy_allows_local_only_for_admins():
    policy = TargetAccessPolicy(local_enabled=True)
    policy.ensure_can_use(local_target(1), is_admin=True)  # does not raise

    with pytest.raises(TargetAccessDenied):
        policy.ensure_can_use(local_target(1), is_admin=False)


def test_policy_is_fail_closed_when_feature_disabled(monkeypatch):
    monkeypatch.setenv("TLA_LOCAL_TARGET_ENABLED", "0")
    assert local_target_enabled() is False
    policy = TargetAccessPolicy()

    with pytest.raises(TargetDisabled):
        policy.ensure_can_use(local_target(1), is_admin=True)
    assert policy.default_target_for(1) is None


def test_policy_never_blocks_ssh_targets():
    policy = TargetAccessPolicy(local_enabled=False)
    ssh = ServerTarget(alias="web", kind=TargetKind.SSH, owner_id=1, hostname="h")
    policy.ensure_can_use(ssh, is_admin=False)  # does not raise


def test_disabled_policy_hides_self_host():
    policy = TargetAccessPolicy(local_enabled=False)
    assert policy.default_target_for(42) is None


def test_target_not_found_carries_translation_context():
    exc = TargetNotFound("ghost")
    assert exc.i18n_key == "server_not_found"
    assert exc.context == {"alias": "ghost"}
