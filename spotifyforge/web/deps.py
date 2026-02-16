"""FastAPI dependency injection helpers for SpotifyForge.

Extracted into their own module so that both ``app.py`` and ``routes.py``
can import them without circular-import issues.
"""

from __future__ import annotations

import logging
from typing import AsyncGenerator

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from spotifyforge.db.engine import _get_async_engine
from spotifyforge.models.models import User

logger = logging.getLogger("spotifyforge.web.deps")


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
            result = await db.execute(
                select(User).where(User.access_token_enc == token)
            )
            user = result.scalars().first()
            if user is not None:
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

    return user
