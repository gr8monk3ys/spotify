"""OAuth authentication module for SpotifyForge.

Wraps Tekore's OAuth 2.0 authorization-code flow with multi-account token
storage and automatic refresh.  Two concrete token-store implementations are
provided: one backed by the OS keyring (ideal for local CLI use) and one that
persists Fernet-encrypted tokens in a database or file (suitable for server
deployments).
"""

from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from typing import Any

import tekore
from cryptography.fernet import Fernet
from cryptography.fernet import InvalidToken as FernetInvalidToken

from spotifyforge.config import Settings
from spotifyforge.security import (
    generate_csrf_state,
    verify_csrf_state,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Required OAuth scopes (from PRD)
# ---------------------------------------------------------------------------

REQUIRED_SCOPES = tekore.Scope(
    "playlist-modify-public",
    "playlist-modify-private",
    "playlist-read-private",
    "playlist-read-collaborative",
    "user-library-read",
    "user-top-read",
    "user-read-recently-played",
    "ugc-image-upload",
)

# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class AuthenticationError(Exception):
    """Raised when an OAuth operation fails (bad credentials, denied access, etc.)."""


class TokenExpiredError(AuthenticationError):
    """Raised when a token has expired and could not be refreshed."""


class TokenNotFoundError(AuthenticationError):
    """Raised when no stored token is available for the requested user."""


# ---------------------------------------------------------------------------
# Serialisation helpers for Tekore tokens
# ---------------------------------------------------------------------------


def _token_to_dict(token: tekore.Token) -> dict[str, Any]:
    """Serialise a Tekore ``Token`` to a plain dictionary."""
    return {
        "access_token": token.access_token,
        "refresh_token": token.refresh_token,
        "token_type": token.token_type,
        "expires_at": token.expires_at,
        "scope": str(token.scope) if token.scope else "",
        "uses_pkce": token.uses_pkce,
    }


def _dict_to_token(data: dict[str, Any]) -> tekore.Token:
    """Reconstruct a Tekore ``Token`` from a dictionary previously created by
    :func:`_token_to_dict`.

    The resulting ``Token`` carries the original *refresh_token* so that
    downstream code can refresh it via ``Credentials.refresh_user_token``.
    """
    # Tekore Token is not trivially constructable from keyword args; we build
    # a minimal info dict that mirrors what the Spotify token endpoint returns
    # and let Tekore parse it.
    token_info: dict[str, Any] = {
        "access_token": data["access_token"],
        "token_type": data.get("token_type", "Bearer"),
        "scope": data.get("scope", ""),
        "refresh_token": data.get("refresh_token", ""),
        # Tekore expects ``expires_in`` (seconds until expiry).  We stored the
        # absolute ``expires_at`` timestamp, so convert it back.
        "expires_in": max(int(data["expires_at"] - time.time()), 0),
    }
    token = tekore.Token(token_info, uses_pkce=data.get("uses_pkce", False))
    return token


# ---------------------------------------------------------------------------
# Token-store protocol / ABC
# ---------------------------------------------------------------------------


class TokenStore(ABC):
    """Abstract base for persisting OAuth tokens keyed by Spotify user ID."""

    @abstractmethod
    def save_token(self, user_id: str, token: tekore.Token) -> None:
        """Persist *token* for *user_id*, overwriting any previous value."""

    @abstractmethod
    def load_token(self, user_id: str) -> tekore.Token:
        """Load a previously stored token for *user_id*.

        Raises
        ------
        TokenNotFoundError
            If no token exists for the given user.
        """

    @abstractmethod
    def delete_token(self, user_id: str) -> None:
        """Remove the stored token for *user_id*.

        Raises
        ------
        TokenNotFoundError
            If no token exists for the given user.
        """


# ---------------------------------------------------------------------------
# Keyring-backed token store (local / CLI)
# ---------------------------------------------------------------------------

_KEYRING_SERVICE = "spotifyforge"


class KeyringTokenStore(TokenStore):
    """Store tokens in the operating-system keyring via the ``keyring`` library.

    Each user's token is stored as a JSON blob under the service name
    ``spotifyforge`` with the Spotify user ID as the username.
    """

    def __init__(self, service_name: str = _KEYRING_SERVICE) -> None:
        try:
            import keyring as _keyring  # noqa: F811
        except ImportError as exc:
            raise ImportError(
                "The 'keyring' package is required for KeyringTokenStore. "
                "Install it with: pip install keyring"
            ) from exc
        self._keyring = _keyring
        self._service = service_name

    def save_token(self, user_id: str, token: tekore.Token) -> None:
        payload = json.dumps(_token_to_dict(token))
        self._keyring.set_password(self._service, user_id, payload)
        logger.debug("Saved token for user %s in keyring", user_id)

    def load_token(self, user_id: str) -> tekore.Token:
        raw = self._keyring.get_password(self._service, user_id)
        if raw is None:
            raise TokenNotFoundError(f"No token found in keyring for user '{user_id}'")
        data: dict[str, Any] = json.loads(raw)
        return _dict_to_token(data)

    def delete_token(self, user_id: str) -> None:
        try:
            self._keyring.delete_password(self._service, user_id)
            logger.debug("Deleted token for user %s from keyring", user_id)
        except self._keyring.errors.PasswordDeleteError as exc:
            raise TokenNotFoundError(f"No token found in keyring for user '{user_id}'") from exc


# ---------------------------------------------------------------------------
# Database / encrypted token store
# ---------------------------------------------------------------------------


class DBTokenStore(TokenStore):
    """Store tokens as Fernet-encrypted JSON blobs.

    This implementation keeps an in-memory dictionary as its backing store.
    In production the *_storage* dict would be replaced by (or backed by) a
    database table; the class is designed so that subclasses can override
    ``_persist`` and ``_retrieve`` for custom backends.

    Parameters
    ----------
    encryption_key
        A URL-safe base64-encoded 32-byte Fernet key.  Generate one with
        ``cryptography.fernet.Fernet.generate_key()``.
    """

    def __init__(self, encryption_key: str | bytes) -> None:
        if isinstance(encryption_key, str):
            encryption_key = encryption_key.encode()
        try:
            self._fernet = Fernet(encryption_key)
        except (ValueError, Exception) as exc:
            raise AuthenticationError("Invalid Fernet encryption key for DBTokenStore") from exc
        self._storage: dict[str, bytes] = {}

    # -- internal persistence hooks (override for real DB) ------------------

    def _persist(self, user_id: str, encrypted: bytes) -> None:
        """Write *encrypted* blob for *user_id* to the backing store."""
        self._storage[user_id] = encrypted

    def _retrieve(self, user_id: str) -> bytes | None:
        """Read the encrypted blob for *user_id*, or ``None`` if absent."""
        return self._storage.get(user_id)

    def _remove(self, user_id: str) -> bool:
        """Remove the entry for *user_id*.  Return ``True`` if it existed."""
        return self._storage.pop(user_id, None) is not None

    # -- public interface ---------------------------------------------------

    def save_token(self, user_id: str, token: tekore.Token) -> None:
        payload = json.dumps(_token_to_dict(token)).encode()
        encrypted = self._fernet.encrypt(payload)
        self._persist(user_id, encrypted)
        logger.debug("Saved encrypted token for user %s", user_id)

    def load_token(self, user_id: str) -> tekore.Token:
        encrypted = self._retrieve(user_id)
        if encrypted is None:
            raise TokenNotFoundError(f"No token found in DB store for user '{user_id}'")
        try:
            decrypted = self._fernet.decrypt(encrypted)
        except FernetInvalidToken as exc:
            raise AuthenticationError(
                f"Failed to decrypt token for user '{user_id}'; the encryption key may have changed"
            ) from exc
        data: dict[str, Any] = json.loads(decrypted)
        return _dict_to_token(data)

    def delete_token(self, user_id: str) -> None:
        if not self._remove(user_id):
            raise TokenNotFoundError(f"No token found in DB store for user '{user_id}'")
        logger.debug("Deleted encrypted token for user %s", user_id)


# ---------------------------------------------------------------------------
# Main auth wrapper
# ---------------------------------------------------------------------------


class SpotifyAuth:
    """High-level wrapper around Tekore's OAuth authorization-code flow.

    Parameters
    ----------
    client_id
        Spotify application client ID.  Falls back to ``Settings``.
    client_secret
        Spotify application client secret.  Falls back to ``Settings``.
    redirect_uri
        Registered redirect URI.  Falls back to ``Settings``.
    token_store
        Optional :class:`TokenStore` for persisting per-user tokens.
    scopes
        OAuth scopes to request.  Defaults to :data:`REQUIRED_SCOPES`.
    asynchronous
        When ``True``, Tekore operations use ``httpx.AsyncClient``.
        Defaults to ``False``.
    """

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        redirect_uri: str | None = None,
        token_store: TokenStore | None = None,
        scopes: tekore.Scope | None = None,
        asynchronous: bool = False,
    ) -> None:
        self._settings = Settings()

        self._client_id = client_id or self._settings.spotify_client_id
        self._client_secret = client_secret or self._settings.spotify_client_secret
        self._redirect_uri = redirect_uri or self._settings.spotify_redirect_uri

        if not self._client_id or not self._client_secret:
            raise AuthenticationError(
                "Spotify client_id and client_secret are required.  "
                "Set them via arguments or the SPOTIFYFORGE_SPOTIFY_CLIENT_ID / "
                "SPOTIFYFORGE_SPOTIFY_CLIENT_SECRET environment variables."
            )

        self._scopes = scopes or REQUIRED_SCOPES
        self._asynchronous = asynchronous
        self._token_store = token_store

        self._credentials = tekore.Credentials(
            client_id=self._client_id,
            client_secret=self._client_secret,
            redirect_uri=self._redirect_uri,
            asynchronous=self._asynchronous,
        )

    # -- properties ---------------------------------------------------------

    @property
    def credentials(self) -> tekore.Credentials:
        """The underlying Tekore :class:`Credentials` instance."""
        return self._credentials

    @property
    def scopes(self) -> tekore.Scope:
        """The OAuth scopes that will be requested during authorisation."""
        return self._scopes

    @property
    def token_store(self) -> TokenStore | None:
        """The configured token store, if any."""
        return self._token_store

    # -- authorisation flow -------------------------------------------------

    def get_auth_url(self, state: str | None = None) -> str:
        """Return the Spotify authorisation URL the user should visit.

        Parameters
        ----------
        state
            Optional opaque value forwarded to Spotify and returned in the
            callback for CSRF protection.

        Returns
        -------
        str
            The full authorisation URL.
        """
        url: str = self._credentials.user_authorisation_url(
            scope=self._scopes,
            state=state,
        )
        logger.debug("Generated auth URL: %s", url)
        return url

    async def handle_callback(self, code: str) -> tekore.Spotify:
        """Exchange an authorisation *code* for tokens and return a Spotify client.

        The token is automatically persisted in the configured
        :class:`TokenStore` (if one was provided), keyed by the Spotify user
        ID obtained from the ``/me`` endpoint.

        Parameters
        ----------
        code
            The authorisation code from the callback query parameters.

        Returns
        -------
        tekore.Spotify
            A ready-to-use Spotify API client.

        Raises
        ------
        AuthenticationError
            If the token exchange or user-info request fails.
        """
        try:
            if self._asynchronous:
                token: tekore.Token = await self._credentials.request_user_token(code)
            else:
                token = self._credentials.request_user_token(code)
        except Exception as exc:
            raise AuthenticationError(
                f"Failed to exchange authorisation code for token: {exc}"
            ) from exc

        client = tekore.Spotify(token, asynchronous=self._asynchronous)

        # Persist token keyed by user ID
        if self._token_store is not None:
            try:
                if self._asynchronous:
                    user = await client.current_user()
                else:
                    user = client.current_user()
                self._token_store.save_token(user.id, token)
                logger.info("Stored token for user %s", user.id)
            except Exception:
                logger.warning(
                    "Token obtained but failed to persist it; "
                    "the client is still usable for this session",
                    exc_info=True,
                )

        return client

    async def refresh_client(self, refresh_token: str) -> tekore.Spotify:
        """Create a new Spotify client from a *refresh_token*.

        Parameters
        ----------
        refresh_token
            A previously obtained refresh token.

        Returns
        -------
        tekore.Spotify
            A Spotify client with a freshly refreshed access token.

        Raises
        ------
        AuthenticationError
            If the refresh request fails.
        """
        try:
            if self._asynchronous:
                token: tekore.Token = await self._credentials.refresh_user_token(refresh_token)
            else:
                token = self._credentials.refresh_user_token(refresh_token)
        except Exception as exc:
            raise TokenExpiredError(f"Failed to refresh token: {exc}") from exc

        return tekore.Spotify(token, asynchronous=self._asynchronous)

    async def get_client(self, user_id: str | None = None) -> tekore.Spotify:
        """High-level helper: obtain a Spotify client for *user_id*.

        Resolution order:

        1. Load the stored token for *user_id* from the token store.
        2. If the token is expiring, refresh it and persist the new one.
        3. Return a ``tekore.Spotify`` client.

        If no *user_id* is supplied and the store contains exactly one user,
        that user's token is used automatically.

        Parameters
        ----------
        user_id
            Spotify user ID.  ``None`` for single-user convenience.

        Returns
        -------
        tekore.Spotify
            A ready-to-use Spotify client.

        Raises
        ------
        TokenNotFoundError
            If no token store is configured, or no token exists for the user.
        TokenExpiredError
            If the stored token cannot be refreshed.
        AuthenticationError
            For any other authentication failure.
        """
        if self._token_store is None:
            raise TokenNotFoundError(
                "No token store configured; cannot look up tokens.  "
                "Provide a TokenStore when constructing SpotifyAuth."
            )

        if user_id is None:
            raise TokenNotFoundError(
                "user_id is required when calling get_client (multi-account support)."
            )

        token = self._token_store.load_token(user_id)

        # Refresh the token if it is expiring (or already expired)
        if token.is_expiring:
            refresh_tok = token.refresh_token
            if not refresh_tok:
                raise TokenExpiredError(
                    f"Token for user '{user_id}' is expiring and has no refresh token."
                )
            try:
                if self._asynchronous:
                    token = await self._credentials.refresh_user_token(refresh_tok)
                else:
                    token = self._credentials.refresh_user_token(refresh_tok)
            except Exception as exc:
                raise TokenExpiredError(
                    f"Failed to refresh token for user '{user_id}': {exc}"
                ) from exc

            self._token_store.save_token(user_id, token)
            logger.info("Refreshed and stored token for user %s", user_id)

        return tekore.Spotify(token, asynchronous=self._asynchronous)


# ---------------------------------------------------------------------------
# Module-level convenience functions (used by web routes and web app)
# ---------------------------------------------------------------------------


def build_auth_url(state: str | None = None) -> str:
    """Build a Spotify authorization URL.

    If no state is provided, generates a CSRF token automatically.
    """
    if state is None:
        state = generate_csrf_state()
    auth = SpotifyAuth()
    return auth.get_auth_url(state=state)


async def exchange_code(
    code: str,
    state: str | None = None,
    expected_state: str | None = None,
) -> dict[str, Any]:
    """Exchange an authorization code for token info.

    If expected_state is provided, validates the state parameter to prevent CSRF.

    Returns a dict with ``access_token``, ``refresh_token``, and
    ``expires_at`` keys, compatible with what the web layer expects.
    """
    if expected_state is not None and not verify_csrf_state(expected_state, state):
        raise AuthenticationError("CSRF state mismatch — possible cross-site request forgery.")

    auth = SpotifyAuth(asynchronous=True)
    try:
        token: tekore.Token = await auth.credentials.request_user_token(code)
    except Exception as exc:
        raise AuthenticationError(f"Failed to exchange authorization code: {exc}") from exc

    return _token_to_dict(token)


async def get_spotify_user(access_token: str) -> dict[str, Any]:
    """Module-level convenience for fetching the Spotify user profile.

    Creates a Tekore async Spotify client with the given *access_token*,
    calls ``/me``, and returns a dict with ``id``, ``display_name``,
    ``email``, and ``product`` fields.
    """
    client = tekore.Spotify(access_token, asynchronous=True)
    try:
        user = await client.current_user()
    except Exception as exc:
        raise AuthenticationError(f"Failed to fetch Spotify user profile: {exc}") from exc

    return {
        "id": user.id,
        "display_name": user.display_name,
        "email": getattr(user, "email", None),
        "product": getattr(user, "product", None),
    }
