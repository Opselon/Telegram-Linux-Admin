"""FastAPI application factory for the /api/v1 management surface.

Security headers, correlation IDs, the JSON error envelope, and the SPA
static mount live here; routers live in sibling ``routes_*.py`` modules.
"""

from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from ..audit import audit
from ..observability.middleware import instrument
from .deps import ApiError
from .events import wire_notifications

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1"

_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)


def create_app(*, services=None) -> FastAPI:
    from ..services import build_services, set_services

    if services is not None:
        set_services(services)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        from ..services import get_services
        try:
            get_services()
        except RuntimeError:
            set_services(build_services())
        wire_notifications()
        yield

    app = FastAPI(
        title="Linux Admin Platform API",
        version="4.0",
        docs_url="/api/docs", openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.services_built_here = services is None

    # --- observability + correlation ID -----------------------------------
    @app.middleware("http")
    async def correlation(request: Request, call_next):
        request.state.correlation_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.correlation_id
        return response

    # --- security headers ---------------------------------------------------
    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Content-Security-Policy", _CSP)
        response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        return response

    # --- error envelope ------------------------------------------------------
    @app.exception_handler(ApiError)
    async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.detail, "details": exc.details}},
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        correlation_id = getattr(request.state, "correlation_id", "?")
        # Log server-side with enough detail to debug; never leak internals.
        audit.record("api.error", path=request.url.path, error=type(exc).__name__)
        logger.exception("unhandled API error (%s)", correlation_id)
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "internal_error",
                               "message": "Internal error (reference " + correlation_id + ")",
                               "details": None}},
        )

    # --- health/readiness (must work without auth) ----------------------------
    @app.get("/api/health")
    async def health() -> dict:
        return {"ok": True}

    @app.get("/api/ready")
    async def ready() -> JSONResponse:
        from .. import database
        from ..services import get_services
        checks: dict[str, bool] = {"database": False}
        try:
            conn = database.get_db_connection()
            conn.execute("SELECT 1")
            checks["database"] = True
        except Exception:
            pass
        try:
            get_services()
            checks["services"] = True
        except RuntimeError:
            checks["services"] = False
        ok = all(checks.values())
        return JSONResponse(status_code=200 if ok else 503, content={"ready": ok, **checks})

    # --- routers ---------------------------------------------------------------
    from .routes_auth import router as auth_router
    from .routes_servers import router as servers_router
    from .routes_commands import router as commands_router
    from .routes_operations import router as operations_router
    from .routes_platform import platform, router as files_router

    app.include_router(auth_router, prefix=API_PREFIX)
    app.include_router(servers_router, prefix=API_PREFIX)
    app.include_router(commands_router, prefix=API_PREFIX)
    app.include_router(operations_router, prefix=API_PREFIX)
    app.include_router(files_router, prefix=API_PREFIX)
    app.include_router(platform, prefix=API_PREFIX)

    # --- SPA static files (built frontend), if present --------------------------
    _mount_spa(app)
    return app


def _mount_spa(app: FastAPI) -> None:
    """Serves the built SPA (web/dist) with an index.html fallback for routes."""
    dist = Path(os.environ.get("TLA_WEB_DIST", "web/dist"))
    index = dist / "index.html"
    if not index.exists():
        return
    if (dist / "assets").exists():
        app.mount("/assets", StaticFiles(directory=str(dist / "assets")), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str):  # noqa: ANN202
        from fastapi.responses import FileResponse

        candidate = (dist / full_path.lstrip("/")).resolve()
        try:
            candidate.relative_to(dist.resolve())
        except ValueError:
            candidate = index  # path escape: serve the SPA shell, not the file
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index)
