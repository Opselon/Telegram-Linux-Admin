"""Authentication routes: setup, login, MFA, logout, me, password change."""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, Request, Response

from ..audit import audit
from ..services import store
from ..services.auth_service import AuthError, MFARequired, generate_totp_secret, set_totp, verify_totp
from ..services.permissions import Actor
from .deps import (
    ApiError,
    current_actor,
    login_rate_limit,
    session_cookie_kwargs,
    set_csrf_cookie,
)
from .schemas import AccountOut, LoginRequest, MFARequest, PasswordChangeRequest, SetupRequest

router = APIRouter(prefix="/auth", tags=["auth"])


def _issue_session(response: Response, token: str) -> None:
    kwargs = session_cookie_kwargs()
    response.set_cookie(**kwargs, value=token)


def _account_out(actor: Actor, username: str) -> AccountOut:
    return AccountOut(
        account_id=actor.account_id,
        username=username,
        role=actor.role,
        permissions=sorted(actor.permissions),
    )


@router.get("/setup")
async def setup_status() -> dict:
    from ..services import get_services
    return get_services().auth.setup_status()


@router.post("/setup", response_model=AccountOut)
async def setup(payload: SetupRequest, response: Response) -> AccountOut:
    from ..services import get_services
    try:
        token, info = get_services().auth.bootstrap_owner(
            payload.username, payload.password, setup_token=payload.setup_token
        )
    except AuthError as exc:
        raise ApiError(403 if exc.key in {"setup_unavailable", "setup_unauthorized"} else 400,
                       exc.key, str(exc)) from exc
    _issue_session(response, token)
    set_csrf_cookie(response)
    actor = store.actor_from_account(store.resolve_session(token))
    return _account_out(actor, payload.username)


@router.post("/login", response_model=AccountOut)
async def login(payload: LoginRequest, request: Request, response: Response) -> AccountOut:
    from ..services import get_services
    try:
        token, info = get_services().auth.login(
            payload.username, payload.password,
            client_key=request.client.host if request.client else "?",
        )
    except MFARequired:
        raise ApiError(401, "mfa_required", "Verification code required.",
                       details={"pending_token": None})  # no token leak in body
    except AuthError as exc:
        status = 429 if exc.key == "login_locked_out" else 401
        raise ApiError(status, exc.key, str(exc),
                       details={"retry_after": 900} if status == 429 else None) from exc
    _issue_session(response, token)
    set_csrf_cookie(response)
    actor = store.actor_from_account(store.resolve_session(token))
    return _account_out(actor, info["username"])


@router.post("/mfa", response_model=AccountOut)
async def mfa(payload: MFARequest, response: Response) -> AccountOut:
    from ..services import get_services
    try:
        token, info = get_services().auth.verify_mfa(payload.pending_token, payload.code)
    except AuthError as exc:
        raise ApiError(401, exc.key, str(exc)) from exc
    _issue_session(response, token)
    set_csrf_cookie(response)
    actor = store.actor_from_account(store.resolve_session(token))
    return _account_out(actor, info["username"])


@router.post("/logout")
async def logout(request: Request, response: Response,
                 actor: Actor = Depends(current_actor)) -> dict:
    token = request.cookies.get("tla_session")
    if token:
        store.revoke_session(token)
    response.delete_cookie("tla_session", path="/")
    response.delete_cookie("tla_csrf", path="/")
    audit.record("auth.logout", account_id=actor.account_id)
    return {"ok": True}


@router.get("/me", response_model=AccountOut)
async def me(actor: Actor = Depends(current_actor)) -> AccountOut:
    return AccountOut(
        account_id=actor.account_id, username=actor.display_name, role=actor.role,
        permissions=sorted(actor.permissions),
    )


@router.post("/password")
async def change_password(payload: PasswordChangeRequest,
                          actor: Actor = Depends(current_actor)) -> dict:
    from ..services import get_services
    try:
        from ..services.auth_service import change_password
        change_password(actor.account_id, payload.current, payload.new, get_services().auth)
    except AuthError as exc:
        raise ApiError(400, exc.key, str(exc)) from exc
    return {"ok": True, "sessions_revoked": True}


@router.post("/mfa/enable")
async def enable_mfa(actor: Actor = Depends(current_actor)) -> dict:
    """Generates a TOTP secret; call /mfa/activate with the first code to arm."""
    web = store.get_web_user_by_account(actor.account_id)
    if web is None:
        raise ApiError(400, "no_web_login", "This account has no web password.")
    secret = generate_totp_secret()
    set_totp(actor.account_id, secret, enabled=False)
    return {"secret": secret, "otpauth": _otpauth_url(secret, actor.display_name)}


@router.post("/mfa/activate")
async def activate_mfa(code: str, actor: Actor = Depends(current_actor)) -> dict:
    web = store.get_web_user_by_account(actor.account_id)
    if web is None or not web.get("totp_secret"):
        raise ApiError(400, "mfa_not_pending", "Call /auth/mfa/enable first.")
    if not verify_totp(str(web["totp_secret"]), code):
        raise ApiError(400, "invalid_code", "Verification code did not match.")
    set_totp(actor.account_id, str(web["totp_secret"]), enabled=True)
    audit.record("auth.mfa.enabled", account_id=actor.account_id)
    return {"ok": True}


@router.post("/mfa/disable")
async def disable_mfa(actor: Actor = Depends(current_actor)) -> dict:
    set_totp(actor.account_id, None, enabled=False)
    audit.record("auth.mfa.disabled", account_id=actor.account_id)
    return {"ok": True}


def _otpauth_url(secret: str, account: str) -> str:
    issuer = os.environ.get("TLA_BRAND", "LinuxAdmin")
    from urllib.parse import quote
    return f"otpauth://totp/{quote(issuer)}:{quote(account)}?secret={secret}&issuer={quote(issuer)}"
