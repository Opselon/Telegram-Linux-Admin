"""Security service: access policy, account administration, audit reads.

Wraps the existing PermissionManager (the same source of truth the Telegram
Security Center uses) so a change made on the web is immediately effective in
the bot and vice versa.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..access_control import permissions as telegram_permissions
from ..audit import audit as _audit


def audit_record(event: str, **fields: object) -> None:
    """Audit one security-relevant event (shared helper for all services)."""
    _audit.record(event, **fields)
from . import store
from .auth_service import AuthError, AuthService
from .permissions import VALID_ROLES, Actor

AUDIT_FILE = Path(os.environ.get("TLA_AUDIT_FILE", "var/audit/audit.jsonl"))


class SecurityService:
    def __init__(self, auth: AuthService) -> None:
        self.auth = auth

    # -- access mode / whitelist (shared with the Telegram Security Center) ----
    def overview(self, actor: Actor) -> dict[str, Any]:
        actor.require("security.read")
        return {
            "access_mode": telegram_permissions.mode,
            "admins": list(telegram_permissions.admins),
            "whitelisted": list(telegram_permissions.whitelisted),
            "accounts": [
                {
                    "account_id": a.id,
                    "kind": a.kind,
                    "external_id": a.external_id if a.kind == "web" else "***",
                    "display_name": a.display_name,
                    "role": a.role,
                }
                for a in store.list_accounts()
            ],
        }

    def set_access_mode(self, actor: Actor, mode: str) -> str:
        actor.require("security.manage")
        if mode not in ("open", "whitelist", "admin_only"):
            raise ValueError("mode must be open, whitelist, or admin_only")
        new_mode = telegram_permissions.set_mode(mode)
        audit_record("security.mode_changed", mode=new_mode, account_id=actor.account_id)
        return new_mode

    def update_whitelist(self, actor: Actor, *, whitelisted: list[int] | None,
                         admins: list[int] | None) -> None:
        actor.require("security.manage")
        telegram_permissions.update(
            whitelisted=whitelisted, admins=admins,
        )
        audit_record("security.whitelist_changed", account_id=actor.account_id)

    # -- account administration --------------------------------------------
    def list_users(self, actor: Actor) -> list[dict[str, Any]]:
        actor.require("users.manage")
        users = []
        for account in store.list_accounts():
            web = store.get_web_user_by_account(account.id)
            users.append({
                "account_id": account.id,
                "kind": account.kind,
                "external_id": account.external_id if account.kind == "web" else "***",
                "display_name": account.display_name,
                "role": account.role,
                "has_web_login": web is not None,
                "web_username": web.get("username") if web else None,
                "disabled": bool(web.get("disabled")) if web else False,
            })
        return users

    def create_web_account(self, actor: Actor, *, username: str, password: str,
                           role: str) -> dict[str, Any]:
        actor.require("users.manage")
        if role not in VALID_ROLES or role == "owner":
            raise AuthError("invalid_role", "Role must be admin, operator, or read_only.")
        if store.get_web_user(username) is not None:
            raise AuthError("username_taken", "That username already exists.")
        reason = self.auth.check_password_strength(password)
        if reason:
            raise AuthError(reason, "Password does not meet the strength requirements.")
        account = store.ensure_account(
            store.KIND_WEB, f"web:{username}", display_name=username, role=role
        )
        if store.get_web_user_by_account(account.id) is not None:
            raise AuthError("username_taken", "That username already exists.")
        store.create_web_user(account.id, username, self.auth.hash_password(password))
        audit_record("user.created", username=username, role=role,
                     account_id=actor.account_id)
        return {"account_id": account.id, "username": username, "role": role}

    def set_role(self, actor: Actor, account_id: int, role: str) -> None:
        actor.require("users.manage")
        if role not in VALID_ROLES:
            raise AuthError("invalid_role", "Unknown role.")
        target = store.get_account(account_id)
        if target is None:
            raise LookupError("account not found")
        if target.role == "owner":
            owners_left = sum(1 for a in store.list_accounts() if a.role == "owner")
            if role != "owner" and owners_left <= 1:
                raise AuthError("last_owner", "Cannot demote the last owner.")
        from .auth_service import promote_role
        promote_role(account_id, role)

    def set_disabled(self, actor: Actor, account_id: int, disabled: bool) -> None:
        actor.require("users.manage")
        if not store.set_web_user_disabled(account_id, disabled):
            raise LookupError("account has no web login")
        if disabled:
            store.revoke_all_sessions(account_id)
        audit_record("user.disabled" if disabled else "user.enabled",
                     account_id=account_id, by=actor.account_id)

    def reset_password(self, actor: Actor, account_id: int, new_password: str) -> None:
        actor.require("users.manage")
        if not store.get_web_user_by_account(account_id):
            raise LookupError("account has no web login")
        reason = self.auth.check_password_strength(new_password)
        if reason:
            raise AuthError(reason, "Password does not meet the strength requirements.")
        store.set_web_user_password(account_id, self.auth.hash_password(new_password))
        store.revoke_all_sessions(account_id)
        audit_record("user.password_reset", account_id=account_id, by=actor.account_id)

    def delete_account(self, actor: Actor, account_id: int) -> None:
        actor.require("users.manage")
        target = store.get_account(account_id)
        if target is None:
            raise LookupError("account not found")
        if target.role == "owner":
            raise AuthError("owner_protected", "Owner accounts cannot be deleted.")
        store.revoke_all_sessions(account_id)
        store.delete_account(account_id)
        audit_record("user.deleted", account_id=account_id, by=actor.account_id)

    def create_link_code(self, actor: Actor, account_id: int | None) -> str:
        """Creates a Bale account-link code (admin may target any account)."""
        if account_id is None:
            account_id = actor.account_id
        elif not actor.has("users.manage"):
            from .permissions import PermissionDenied
            raise PermissionDenied(actor, "users.manage")
        if store.get_account(account_id) is None:
            raise LookupError("account not found")
        code = store.create_link_code(account_id)
        audit_record("bale.link_code_created", for_account=account_id,
                     account_id=actor.account_id)
        return code

    # -- audit reads ----------------------------------------------------------
    def audit_tail(self, actor: Actor, *, limit: int = 100) -> list[dict[str, Any]]:
        actor.require("security.read")
        limit = max(1, min(int(limit), 500))
        try:
            lines = AUDIT_FILE.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        events: list[dict[str, Any]] = []
        for line in reversed(lines):
            if len(events) >= limit:
                break
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events

    # -- Bale linking -----------------------------------------------------------
    def link_bale(self, actor: Actor, code: str, bale_user_id: str,
                  display_name: str | None) -> dict[str, Any]:
        """Binds a Bale user to the account that owns *code* (single-use)."""
        account = store.consume_link_code(code)
        if account is None:
            audit_record("bale.link_denied", bale_user_id="***")
            raise AuthError("invalid_code", "The link code is invalid or expired.")
        store.bind_bale_user(bale_user_id, account.id, display_name)
        audit_record("bale.linked", account_id=account.id)
        return {"account_id": account.id, "role": account.role}
