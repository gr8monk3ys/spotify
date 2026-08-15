"""FastAPI dependency injection helpers for SpotifyForge.

Extracted into their own module so that both ``app.py`` and ``routes.py``
can import them without circular-import issues.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator

import tekore as tk
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from spotifyforge.auth.oauth import AuthenticationError
from spotifyforge.core.clients import spotify_client_for_user
from spotifyforge.db.engine import get_async_session
from spotifyforge.models.models import User
from spotifyforge.security import verify_session

logger = logging.getLogger("spotifyforge.web.deps")

SESSION_COOKIE = "spotifyforge_session"


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields an ``AsyncSession``."""
    async with get_async_session() as session:
        yield session


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db_session),
) -> User:
    """Resolve the authenticated user from the signed session cookie.

    The cookie is HMAC-signed (see :mod:`spotifyforge.security`); a
    forged, tampered, or expired cookie fails verification and yields
    401. Raises 401 if the referenced user no longer exists.
    """
    user_id = verify_session(request.cookies.get(SESSION_COOKIE))
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated. Please log in via /api/auth/login.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalars().first()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User session is invalid or expired. Please re-authenticate.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


async def get_spotify(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> AsyncGenerator[tk.Spotify, None]:
    """Yield an authenticated Spotify client for the current user.

    Decrypts the stored access token (refreshing and persisting it first
    when expired), and closes the client's HTTP pool when the request
    finishes. Authentication problems map to 401 so the front-end re-runs
    the OAuth flow; anything else propagates to the route.
    """
    try:
        client = await spotify_client_for_user(current_user, db)
    except AuthenticationError as exc:
        logger.info("Spotify auth failed for user %s: %s", current_user.id, exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Spotify authorization expired. Please re-authenticate.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    try:
        yield client
    finally:
        await client.close()
