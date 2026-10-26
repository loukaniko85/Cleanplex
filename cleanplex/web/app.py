"""FastAPI application factory."""

from __future__ import annotations

import base64
import os
import secrets
import traceback
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..logger import get_logger
from .routes.settings import router as settings_router
from .routes.sessions import router as sessions_router
from .routes.users import router as users_router
from .routes.segments import router as segments_router
from .routes.scanner_routes import router as scanner_router
from .routes.thumbnails import router as thumbnails_router
from .routes.sync_routes import router as sync_router
from .routes.mcp_routes import router as mcp_router
from .routes.analytics_routes import router as analytics_router
from .routes.marker_routes import router as marker_router
from .routes.jumpscare_routes import router as jumpscare_router
from .routes.import_routes import router as import_router
from .routes.videoskip_routes import router as videoskip_router
from .routes.webhook_routes import router as webhook_router

STATIC_DIR = Path(__file__).parent / "static"
logger = get_logger(__name__)


def create_app() -> FastAPI:
    app = FastAPI(
        title="Cleanplex",
        description="Plex content filter service",
        version="1.1.0",
    )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.error(
            "Unhandled error %s %s — %s\n%s",
            request.method,
            request.url.path,
            exc,
            traceback.format_exc(),
        )
        # Never project exception text to clients: it can contain file paths,
        # driver messages and upstream response bodies. The log keeps the detail.
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error — see the Cleanplex log for details."},
        )

    # The built UI is same-origin, so cross-origin access is opt-in. A wildcard
    # here let any website read API responses from a LAN deployment.
    cors_origins = [
        origin.strip()
        for origin in os.environ.get("CLEANPLEX_CORS_ORIGINS", "").split(",")
        if origin.strip()
    ]
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # Optional HTTP Basic auth. Empty env vars keep the original LAN-only
    # behaviour; setting both is enough to protect the UI and API behind a
    # browser prompt without a reverse proxy.
    auth_user = os.environ.get("CLEANPLEX_AUTH_USER", "").strip()
    auth_pass = os.environ.get("CLEANPLEX_AUTH_PASS", "")
    if auth_user and auth_pass:
        @app.middleware("http")
        async def _basic_auth(request: Request, call_next):
            path = request.url.path
            if (
                path == "/api/health"
                or path == "/favicon.svg"
                or path.startswith("/assets/")
                # Plex cannot send Basic credentials; the webhook has its own
                # optional shared token instead (CLEANPLEX_WEBHOOK_TOKEN).
                or path == "/api/webhooks/plex"
            ):
                return await call_next(request)
            header = request.headers.get("authorization", "")
            if header.lower().startswith("basic "):
                try:
                    decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
                    user, _, password = decoded.partition(":")
                except (ValueError, UnicodeDecodeError):
                    user = password = ""
                if secrets.compare_digest(user, auth_user) and secrets.compare_digest(
                    password, auth_pass
                ):
                    return await call_next(request)
            return Response(
                status_code=401,
                content="Authentication required",
                headers={"WWW-Authenticate": 'Basic realm="cleanplex"'},
            )

    @app.get("/api/health", include_in_schema=False)
    async def health():
        """Unauthenticated liveness probe for Docker and reverse proxies."""
        return {"status": "ok", "version": __version__}

    app.include_router(settings_router)
    app.include_router(sessions_router)
    app.include_router(users_router)
    app.include_router(segments_router)
    app.include_router(scanner_router)
    app.include_router(thumbnails_router)
    app.include_router(sync_router)
    app.include_router(mcp_router)
    app.include_router(analytics_router)
    app.include_router(marker_router)
    app.include_router(jumpscare_router)
    app.include_router(import_router)
    app.include_router(videoskip_router)
    app.include_router(webhook_router)

    # Serve built React frontend (if present). The assets directory is absent in a
    # source checkout until `npm run build` runs, and mounting a missing directory
    # would crash the app (and every test) rather than simply serving the API.
    assets_dir = STATIC_DIR / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

        @app.get("/", include_in_schema=False)
        @app.get("/{full_path:path}", include_in_schema=False)
        async def serve_spa(full_path: str = ""):
            index = STATIC_DIR / "index.html"
            if index.exists():
                return FileResponse(str(index))
            return {"message": "Cleanplex API running. Frontend not built yet."}

    return app
