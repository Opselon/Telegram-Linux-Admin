"""FastAPI dependencies: session auth, CSRF, permissions, rate limits, errors.

Error model: every failure surfaces as ``{"error": {"code", "message",
"details"}}`` via the handlers registered in ``app.py``.  Ownership denials
map to 404 (never leak existence); permission denials to 403.
"""

from __future__ import annotations

import os
import secrets
from typing import Any

from fastapi import Depends, HTTPException, Request, Response

from ..access_control import rate_limiter
from ..audit import audit
from ..services import store
from ..services.permissions import Actor, PermissionDenied  # noqa: F401 (re-export)

SESSION_COOKIE = "tla_session"
CSRF_COOKIE = "tla_csrf"
CSRF_HEADER = "x-csrf-token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class ApiError(HTTPException):
    def __init__(self, status_code: int, code: str, message: str, details: Any = None) -> None:
        super().__init__(status_code=status_code, detail=message)
        self.code = code
        self.details = details


def _client_key(request: Request) -> str:
    """Coarse client identity for lockout/rate-limit keys (proxy-aware)."""
    forwarded = request.headers.get("x-forwarded-for")
    ip = forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else "?")
    return ip


def get_session_token(request: Request) -> str | None:
    return request.cookies.get(SESSION_COOKIE)


async def current_actor(request: Request) -> Actor:
    """Authenticates the request via the session cookie; 401 when invalid.

    Also applies the default-on per-account API budget (240 req/min, env
    ``TLA_API_RATE_LIMIT``) — per-route limits stack on top of this.
    """
    account = store.resolve_session(request.cookies.get(SESSION_COOKIE))
    if account is None:
        raise ApiError(401, "unauthenticated", "Authentication required.")
    actor = store.actor_from_account(account)
    max_calls = int(os.environ.get("TLA_API_RATE_LIMIT", "240"))
    allowed, retry_after = rate_limiter.check(
        actor.account_id, "api:global", max_calls=max_calls, period=60.0
    )
    if not allowed:
        audit.record("security.rate_limited", account_id=actor.account_id)
        raise ApiError(429, "rate_limited", "Too many requests; slow down.",
                       details={"retry_after": round(retry_after, 1)})
    if actor.role == "read_only" and account.kind == "web":
        web = store.get_web_user_by_account(account.id)
        if web is not None and web.get("disabled"):
            raise ApiError(403, "account_disabled", "This account is disabled.")
    return actor


async def require_csrf(request: Request, actor: Actor = Depends(current_actor)) -> Actor:
    """Double-submit CSRF check for all mutating methods."""
    if request.method in SAFE_METHODS:
        return actor
    cookie = request.cookies.get(CSRF_COOKIE)
    header = request.headers.get(CSRF_HEADER)
    if not cookie or not header or not secrets.compare_digest(cookie, header):
        audit.record("security.csrf_denied", path=request.url.path)
        raise ApiError(403, "csrf_failed", "Missing or invalid CSRF token.")
    return actor


def require_permission(permission: str):
    """Dependency factory: enforces a permission on the authenticated actor."""

    async def _dependency(actor: Actor = Depends(require_csrf)) -> Actor:
        if not actor.has(permission):
            audit.record(
                "security.denied",
                permission=permission,
                account_id=actor.account_id,
                kind=actor.kind,
            )
            raise ApiError(403, "forbidden", f"Permission '{permission}' is required.")
        return actor

    return _dependency


def rate_limit(scope: str, *, max_calls: int, period: float = 60.0):
    """Dependency factory: per-account sliding-window rate limit."""

    async def _dependency(actor: Actor = Depends(current_actor)) -> Actor:
        allowed, retry_after = rate_limiter.check(
            actor.account_id, f"api:{scope}", max_calls=max_calls, period=period
        )
        if not allowed:
            raise ApiError(
                429, "rate_limited", "Too many requests; slow down.",
                details={"retry_after": round(retry_after, 1)},
            )
        return actor

    return _dependency


def login_rate_limit():
    """Pre-auth limiter: per-IP bucket on the login route."""

    async def _dependency(request: Request) -> None:
        ip = _client_key(request)
        import hashlib
        bucket = int(hashlib.sha256(ip.encode()).hexdigest()[:12], 16)
        ok, retry = rate_limiter.check(bucket, "api:login", max_calls=10, period=60.0)
        if not ok:
            raise ApiError(429, "rate_limited", "Too many login attempts.",
                           details={"retry_after": round(retry, 1)})

    return _dependency


def _cookie_secure() -> bool:
    """Secure-cookie default: on unless the web host is loopback (plain HTTP dev)."""
    override = os.environ.get("TLA_WEB_SECURE_COOKIES")
    if override == "1":
        return True
    if override == "0":
        return False
    return os.environ.get("TLA_WEB_HOST", "127.0.0.1") not in ("127.0.0.1", "localhost")


def set_csrf_cookie(response: Response) -> str:
    """Generates the CSRF token and sets the non-HttpOnly cookie."""
    token = secrets.token_urlsafe(24)
    response.set_cookie(
        CSRF_COOKIE, token, httponly=False, samesite="strict",
        secure=_cookie_secure(), path="/",
    )
    return token


def session_cookie_kwargs() -> dict[str, Any]:
    return {
        "key": SESSION_COOKIE,
        "httponly": True,
        "samesite": "strict",
        "secure": _cookie_secure(),
        "path": "/",
        "max_age": int(store.SESSION_ABSOLUTE_TTL),
    }


def not_found() -> ApiError:
    return ApiError(404, "not_found", "Resource not found.")
