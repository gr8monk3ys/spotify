"""Spotify client construction from stored user credentials.

The single place where encrypted tokens leave the database and become a
usable :class:`tekore.Spotify` client. Both the web layer and the
scheduler go through :func:`spotify_client_for_user`, so token
decryption, refresh, and persistence happen exactly one way.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import httpx
import tekore as tk
from sqlalchemy.ext.asyncio import AsyncSession
from tekore._sender.concrete import Sender
from tekore._sender.extending import ExtendingSender

from spotifyforge.auth.oauth import (
    AuthenticationError,
    SpotifyAuth,
    TokenExpiredError,
    make_sender,
)
from spotifyforge.models.models import User, as_utc, utc_now
from spotifyforge.security import decrypt_token, encrypt_token

logger = logging.getLogger(__name__)

# Refresh a little early so a token never expires mid-operation.
_REFRESH_MARGIN = timedelta(seconds=60)

# Retry 429/5xx responses this many times, honouring Retry-After.
_RETRIES = 2

# httpx defaults to 5s, which a bulk run trips on eventually: one slow
# response then aborts hundreds of playlists' worth of work.
_TIMEOUT = 30.0


class TimeoutRetryingSender(ExtendingSender):
    """Retry requests that never got an answer at all.

    ``tk.RetryingSender`` retries on 429 and 5xx — responses. A read
    timeout or dropped connection raises before there is a response, so
    it slips past and aborts whatever bulk operation was running. Across
    the few hundred requests a full catalogue run makes, that is close to
    inevitable, and it killed a 221-playlist reflow three times.
    """

    def __init__(self, sender: Sender | None = None, retries: int = 2) -> None:
        super().__init__(sender)
        self.retries = retries

    def send(self, request: object) -> object:
        if self.is_async:
            return self._async_send(request)
        raise NotImplementedError("TimeoutRetryingSender is async-only")

    async def _async_send(self, request: object) -> object:
        delay = 1.0
        for attempt in range(self.retries + 1):
            try:
                return await self.sender.send(request)
            except httpx.TransportError as exc:
                if attempt == self.retries:
                    logger.error("Request failed after %d attempts: %r", attempt + 1, exc)
                    raise
                logger.warning("Transport error (%r); retrying in %.0fs", exc, delay)
                await asyncio.sleep(delay)
                delay *= 2
        raise AssertionError("unreachable")


def build_spotify(access_token: str) -> tk.Spotify:
    """Wrap a plaintext access token in an async, retrying Spotify client."""
    inner = make_sender(True) or tk.AsyncSender(client=httpx.AsyncClient(timeout=_TIMEOUT))
    sender = tk.RetryingSender(retries=_RETRIES, sender=TimeoutRetryingSender(inner))
    return tk.Spotify(access_token, sender=sender)


def apply_user_tokens(
    user: User,
    access_token: str,
    refresh_token: str | None,
    expires_at: float | None,
) -> None:
    """Write a token set onto a ``User`` row (encrypted), without committing.

    The single definition of how tokens are persisted — the web callback,
    the CLI login, and the refresh path all go through here. A missing
    refresh token keeps the previously stored one (Spotify may omit it on
    refresh).
    """
    user.access_token_enc = encrypt_token(access_token)
    if refresh_token:
        user.refresh_token_enc = encrypt_token(refresh_token)
    user.token_expiry = datetime.fromtimestamp(expires_at, tz=UTC) if expires_at else None
    user.updated_at = utc_now()


def apply_user_profile(user: User, profile: dict[str, object]) -> None:
    """Write Spotify profile fields onto a ``User`` row, without committing."""
    user.display_name = profile.get("display_name") or user.display_name  # type: ignore[assignment]
    user.email = profile.get("email") or user.email  # type: ignore[assignment]
    user.is_premium = profile.get("product") == "premium"


def _token_expired(user: User) -> bool:
    expiry = as_utc(user.token_expiry)
    if expiry is None:
        return False
    return expiry <= datetime.now(UTC) + _REFRESH_MARGIN


async def refresh_user_token(user: User, db: AsyncSession) -> User:
    """Refresh the user's Spotify tokens and persist the new values.

    Raises
    ------
    TokenExpiredError
        If no refresh token is stored or the refresh request fails.
    AuthenticationError
        If the stored refresh token cannot be decrypted.
    """
    if not user.refresh_token_enc:
        raise TokenExpiredError(
            f"User {user.id} has an expired token and no refresh token; re-authentication needed."
        )

    try:
        refresh_token_plain = decrypt_token(user.refresh_token_enc)
    except Exception as exc:
        raise AuthenticationError(
            f"Stored refresh token for user {user.id} cannot be decrypted; "
            "the encryption key may have changed."
        ) from exc

    auth = SpotifyAuth(asynchronous=True)
    new_token = await auth.credentials.refresh_user_token(refresh_token_plain)

    apply_user_tokens(user, new_token.access_token, new_token.refresh_token, new_token.expires_at)

    db.add(user)
    await db.commit()
    await db.refresh(user)

    logger.info("Refreshed Spotify token for user %s", user.id)
    return user


async def spotify_client_for_user(user: User, db: AsyncSession) -> tk.Spotify:
    """Return an authenticated async Spotify client for *user*.

    Decrypts the stored access token, refreshing it first (and persisting
    the result) when it is expired or about to expire.

    Raises
    ------
    TokenExpiredError / AuthenticationError
        When no usable token can be produced; callers translate these
        into 401s or job failures as appropriate.
    """
    if _token_expired(user):
        user = await refresh_user_token(user, db)

    if not user.access_token_enc:
        raise TokenExpiredError(f"User {user.id} has no stored access token.")

    try:
        access_token = decrypt_token(user.access_token_enc)
    except Exception as exc:
        raise AuthenticationError(
            f"Stored access token for user {user.id} cannot be decrypted; "
            "the encryption key may have changed."
        ) from exc

    return build_spotify(access_token)
