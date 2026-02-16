"""FastAPI application factory and core dependencies for SpotifyForge.

This module creates and configures the main FastAPI application, including
CORS middleware, lifespan events for database and scheduler initialization,
the Spotify OAuth callback endpoint, a health check, and dependency
injection helpers used across all route modules.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import TYPE_CHECKING, AsyncGenerator

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from spotifyforge import __version__
from spotifyforge.config import settings
from spotifyforge.db.engine import _get_async_engine, init_db
from spotifyforge.models.models import User

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
        allow_methods=["*"],
        allow_headers=["*"],
    )

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

    @app.get(
        "/callback",
        response_class=RedirectResponse,
        summary="Spotify OAuth redirect handler",
        tags=["oauth"],
    )
    async def oauth_callback(
        code: str = Query(..., description="Authorization code returned by Spotify"),
        state: str | None = Query(default=None, description="Anti-CSRF state token"),
    ) -> RedirectResponse:
        """Handle the Spotify OAuth redirect.

        Exchanges the authorization *code* for access and refresh tokens,
        persists or updates the user record, and redirects the browser to
        the front-end dashboard.
        """
        from spotifyforge.auth.oauth import exchange_code, get_spotify_user

        try:
            token_info = await exchange_code(code, state=state)
        except Exception as exc:
            logger.error("OAuth token exchange failed: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to exchange authorization code with Spotify.",
            ) from exc

        try:
            spotify_user = await get_spotify_user(token_info["access_token"])
        except Exception as exc:
            logger.error("Failed to fetch Spotify user profile: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to retrieve Spotify user profile.",
            ) from exc

        # Upsert user in database
        async with get_async_session_raw() as session:
            result = await session.execute(
                select(User).where(User.spotify_id == spotify_user["id"])
            )
            user = result.scalars().first()

            if user is None:
                user = User(
                    spotify_id=spotify_user["id"],
                    display_name=spotify_user.get("display_name"),
                    email=spotify_user.get("email"),
                    access_token_enc=token_info["access_token"],
                    refresh_token_enc=token_info.get("refresh_token"),
                    token_expiry=token_info.get("expires_at"),
                    is_premium=spotify_user.get("product") == "premium",
                )
                session.add(user)
            else:
                user.access_token_enc = token_info["access_token"]
                user.refresh_token_enc = token_info.get("refresh_token", user.refresh_token_enc)
                user.token_expiry = token_info.get("expires_at")
                user.display_name = spotify_user.get("display_name", user.display_name)
                user.email = spotify_user.get("email", user.email)
                user.is_premium = spotify_user.get("product") == "premium"
                user.updated_at = datetime.now(timezone.utc)
                session.add(user)

            await session.commit()

        return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)

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

    return app


# ---------------------------------------------------------------------------
# Dependency injection helpers (canonical implementations live in deps.py
# to avoid circular imports with routes.py; re-exported here for convenience)
# ---------------------------------------------------------------------------
from spotifyforge.web.deps import get_current_user, get_db_session  # noqa: F401, E402


async def get_async_session_raw() -> AsyncGenerator[AsyncSession, None]:
    """Yield a raw async session outside of FastAPI dependency injection.

    This is used internally (e.g. in the ``/callback`` handler).  For
    route handlers, prefer :func:`get_db_session`.
    """
    engine = _get_async_engine()
    async with AsyncSession(engine) as session:
        yield session


# ---------------------------------------------------------------------------
# Convenience: module-level app for ``uvicorn spotifyforge.web.app:app``
# ---------------------------------------------------------------------------
app = create_app()
