"""FastAPI application factory and core dependencies for SpotifyForge.

This module creates and configures the main FastAPI application, including
CORS middleware, lifespan events for database and scheduler initialization,
a health check, and dependency injection helpers used across all route
modules.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from spotifyforge import __version__
from spotifyforge.config import settings
from spotifyforge.db.engine import init_db

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger("spotifyforge.web")

# ---------------------------------------------------------------------------
# Configurable CORS origins
# ---------------------------------------------------------------------------
# In production, override via the SPOTIFYFORGE_CORS_ORIGINS env var
# (comma-separated list).  Defaults are suitable for local development.
_DEFAULT_ORIGINS = [
    "http://localhost:3000",
    "http://localhost:5173",
    "http://localhost:8000",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:8000",
]

import os as _os

CORS_ORIGINS: list[str] = [
    origin.strip()
    for origin in _os.environ.get("SPOTIFYFORGE_CORS_ORIGINS", "").split(",")
    if origin.strip()
] or _DEFAULT_ORIGINS

# ---------------------------------------------------------------------------
# Scheduler singleton (lazy import to avoid hard dep when scheduler disabled)
# ---------------------------------------------------------------------------
_scheduler = None


def _get_scheduler():
    """Return the APScheduler BackgroundScheduler singleton."""
    global _scheduler  # noqa: PLW0603
    if _scheduler is None:
        from apscheduler.schedulers.background import BackgroundScheduler

        _scheduler = BackgroundScheduler()
    return _scheduler


# ---------------------------------------------------------------------------
# Lifespan (startup / shutdown)
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan handler: initialize DB and start scheduler."""
    # --- Startup ---
    logger.info("Initializing database...")
    init_db()

    if settings.scheduler_enabled:
        logger.info("Starting background scheduler...")
        scheduler = _get_scheduler()
        if not scheduler.running:
            scheduler.start()

        # Reload saved jobs from the database
        try:
            from sqlmodel import Session, select
            from spotifyforge.db.engine import get_engine
            from spotifyforge.models.models import ScheduledJob
            from spotifyforge.core.scheduler import register_job

            with Session(get_engine()) as session:
                jobs = session.exec(
                    select(ScheduledJob).where(ScheduledJob.enabled == True)  # noqa: E712
                ).all()
                for job in jobs:
                    try:
                        register_job(job)
                    except Exception:
                        logger.warning("Failed to reload job %s (%s)", job.id, job.name, exc_info=True)
                logger.info("Reloaded %d scheduled job(s) from database.", len(jobs))
        except Exception:
            logger.warning("Failed to reload scheduled jobs from database.", exc_info=True)

    yield

    # --- Shutdown ---
    if settings.scheduler_enabled and _scheduler is not None and _scheduler.running:
        logger.info("Shutting down scheduler...")
        _scheduler.shutdown(wait=False)

    logger.info("SpotifyForge API stopped.")


# ---------------------------------------------------------------------------
# Application instance
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    """Build and return the configured FastAPI application.

    This is the canonical entry point for ``uvicorn`` and test clients::

        uvicorn spotifyforge.web.app:create_app --factory
    """
    # Configure logging from application settings
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    app = FastAPI(
        title="SpotifyForge API",
        version=__version__,
        description=(
            "REST API for SpotifyForge -- the all-in-one platform "
            "for serious Spotify playlist curators."
        ),
        lifespan=_lifespan,
    )

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", "Accept"],
    )

    # ------------------------------------------------------------------
    # Security headers middleware
    # ------------------------------------------------------------------
    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'"
        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        return response

    # ------------------------------------------------------------------
    # Global exception handler
    # ------------------------------------------------------------------
    @app.exception_handler(Exception)
    async def _global_exception_handler(request: Request, exc: Exception):
        logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error. Please try again later."},
        )

    # ------------------------------------------------------------------
    # Simple in-memory rate limiter
    # NOTE: This in-memory implementation is only suitable for single-worker
    # deployments.  For multi-worker production deployments, replace with a
    # Redis-backed solution such as slowapi (https://github.com/laurentS/slowapi).
    # ------------------------------------------------------------------
    import time as _time
    from collections import defaultdict

    _rate_limit_store: dict[str, list[float]] = defaultdict(list)
    _RATE_LIMIT = 60  # requests per window
    _RATE_WINDOW = 60  # seconds
    _CLEANUP_EVERY = 100  # run stale-IP cleanup every N requests
    _request_counter = {"count": 0}  # mutable container for nonlocal access

    def _prune_stale_ips(now: float) -> None:
        """Remove IPs that have not been seen within 2x the rate window.

        This prevents unbounded memory growth when many unique client IPs
        hit the server over time.
        """
        stale_cutoff = now - (_RATE_WINDOW * 2)
        stale_keys = [
            ip for ip, timestamps in _rate_limit_store.items()
            if not timestamps or timestamps[-1] < stale_cutoff
        ]
        for ip in stale_keys:
            del _rate_limit_store[ip]

    @app.middleware("http")
    async def rate_limit_middleware(request: Request, call_next):
        # Skip rate limiting for health checks
        if request.url.path == "/health":
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"
        now = _time.time()
        window_start = now - _RATE_WINDOW

        # Periodically prune IPs that haven't been seen in 2x the window
        _request_counter["count"] += 1
        if _request_counter["count"] >= _CLEANUP_EVERY:
            _request_counter["count"] = 0
            _prune_stale_ips(now)

        # Clean old entries for the current IP and add the new one
        _rate_limit_store[client_ip] = [
            t for t in _rate_limit_store[client_ip] if t > window_start
        ]

        if len(_rate_limit_store[client_ip]) >= _RATE_LIMIT:
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests. Please slow down."},
                headers={"Retry-After": str(_RATE_WINDOW)},
            )

        _rate_limit_store[client_ip].append(now)
        return await call_next(request)

    # ------------------------------------------------------------------
    # Request logging middleware
    # ------------------------------------------------------------------
    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        import time
        start = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.info(
            "%s %s %d %.1fms",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
        )
        return response

    # Include routers from the routes module
    from spotifyforge.web.routes import (
        auth_router,
        discovery_router,
        playlist_router,
        schedule_router,
    )

    app.include_router(auth_router)
    app.include_router(playlist_router)
    app.include_router(discovery_router)
    app.include_router(schedule_router)

    # ------------------------------------------------------------------
    # Top-level endpoints (outside any router)
    # ------------------------------------------------------------------

    @app.get("/health", summary="Health check", tags=["ops"])
    async def health_check() -> dict[str, str]:
        """Return a simple health-check response.

        Useful for load-balancer probes, container orchestrators, and
        uptime monitors.
        """
        return {
            "status": "healthy",
            "version": __version__,
        }

    @app.get("/dashboard", response_class=HTMLResponse, summary="Dashboard", tags=["oauth"])
    async def dashboard():
        """Simple landing page shown after successful OAuth login."""
        return HTMLResponse(content="""<!DOCTYPE html>
<html>
<head><title>SpotifyForge</title>
<style>
body { font-family: system-ui, sans-serif; max-width: 600px; margin: 80px auto; padding: 0 20px; color: #1a1a1a; }
h1 { color: #1DB954; }
a { color: #1DB954; }
.card { background: #f5f5f5; border-radius: 8px; padding: 20px; margin: 20px 0; }
</style></head>
<body>
<h1>SpotifyForge</h1>
<div class="card">
<p><strong>Authentication successful.</strong> You're now logged in.</p>
<p>You can use the SpotifyForge API to manage your playlists, discover music, and set up automated workflows.</p>
</div>
<h2>Next steps</h2>
<ul>
<li><a href="/docs">API Documentation</a> — Interactive Swagger UI</li>
<li><a href="/api/auth/me">Your Profile</a> — View your account info</li>
<li><a href="/api/playlists">Your Playlists</a> — Browse your playlists</li>
</ul>
<p style="margin-top: 40px; color: #666; font-size: 0.9em;">
Or use the CLI: <code>spotifyforge playlist list</code>
</p>
</body></html>""")

    return app


# ---------------------------------------------------------------------------
# Dependency injection helpers (canonical implementations live in deps.py
# to avoid circular imports with routes.py; re-exported here for convenience)
# ---------------------------------------------------------------------------
from spotifyforge.web.deps import get_current_user, get_db_session  # noqa: F401, E402


# ---------------------------------------------------------------------------
# Convenience: module-level app for ``uvicorn spotifyforge.web.app:app``
# ---------------------------------------------------------------------------
app = create_app()
