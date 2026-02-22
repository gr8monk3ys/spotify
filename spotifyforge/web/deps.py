"""FastAPI dependency injection helpers for SpotifyForge.

Extracted into their own module so that both ``app.py`` and ``routes.py``
can import them without circular-import issues.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from spotifyforge.auth.oauth import AuthenticationError, SpotifyAuth
from spotifyforge.db.engine import _get_async_engine
from spotifyforge.models.models import User
from spotifyforge.security import decrypt_token, encrypt_token, hash_token

logger = logging.getLogger("spotifyforge.web.deps")


async def _refresh_user_token(user: User, db: AsyncSession) -> User:
    """Attempt to refresh the user's expired Spotify access token.

    Decrypts the stored refresh token, calls the Spotify token-refresh
    endpoint via :class:`SpotifyAuth`, and persists the new encrypted
    tokens and expiry back to the database.

    Parameters
    ----------
    user
        The :class:`User` whose token needs refreshing.
    db
        An active async database session for persisting updated tokens.

    Returns
    -------
    User
        The same user instance, updated with new token fields.

    Raises
    ------
    HTTPException (401)
        If the refresh token is missing, cannot be decrypted, or the
        Spotify refresh request fails.
    """
    if not user.refresh_token_enc:
        logger.warning("User %s has no refresh token; cannot auto-refresh.", user.id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired and no refresh token available. Please re-authenticate.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        refresh_token_plain = decrypt_token(user.refresh_token_enc)
    except Exception:
        logger.exception("Failed to decrypt refresh token for user %s", user.id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired and refresh token is invalid. Please re-authenticate.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        auth = SpotifyAuth(asynchronous=True)
        new_token = await auth.credentials.refresh_user_token(refresh_token_plain)
    except (AuthenticationError, Exception):
        logger.exception("Failed to refresh Spotify token for user %s", user.id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired and refresh failed. Please re-authenticate.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Persist the refreshed tokens
    user.access_token_enc = encrypt_token(new_token.access_token)
    user.token_hash = hash_token(new_token.access_token)
    if new_token.refresh_token:
        user.refresh_token_enc = encrypt_token(new_token.refresh_token)
    user.token_expiry = datetime.fromtimestamp(new_token.expires_at, tz=UTC)
    user.updated_at = datetime.now(tz=UTC)

    db.add(user)
    await db.commit()
    await db.refresh(user)

    logger.info("Successfully refreshed token for user %s", user.id)
    return user


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields an ``AsyncSession``.

    The session is automatically closed when the request finishes::

        @router.get("/items")
        async def list_items(db: AsyncSession = Depends(get_db_session)):
            ...
    """
    engine = _get_async_engine()
    async with AsyncSession(engine) as session:
        yield session


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db_session),
) -> User:
    """FastAPI dependency that validates the caller's authentication.

    Looks for a ``user_id`` stored in the session cookie (or a Bearer
    token, depending on the front-end strategy).  Raises *401* if the
    caller is not authenticated or the user no longer exists.
    """
    user_id: int | None = None

    # Strategy 1: session cookie (set during OAuth callback)
    session_user_id = request.cookies.get("spotifyforge_user_id")
    if session_user_id is not None:
        try:
            user_id = int(session_user_id)
        except (TypeError, ValueError):
            pass

    # Strategy 2: Bearer token in Authorization header
    if user_id is None:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
            result = await db.execute(select(User).where(User.token_hash == hash_token(token)))
            user = result.scalars().first()
            if user is not None:
                if user.token_expiry is not None and user.token_expiry.replace(
                    tzinfo=UTC
                ) <= datetime.now(tz=UTC):
                    user = await _refresh_user_token(user, db)
                return user

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

    if user.token_expiry is not None and user.token_expiry.replace(tzinfo=UTC) <= datetime.now(
        tz=UTC
    ):
        user = await _refresh_user_token(user, db)

    return user
