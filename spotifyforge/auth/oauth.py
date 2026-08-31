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
from collections.abc import Callable
from typing import Any

import tekore

from spotifyforge.config import Settings
from spotifyforge.security import (
    generate_csrf_state,
    verify_csrf_state,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Test seam: when set (tests only), every internally constructed tekore
# Credentials/Spotify instance uses a sender from this factory, so the whole
# OAuth + API stack can be pointed at an in-process fake. Production leaves
# it None and tekore uses its default senders.
# ---------------------------------------------------------------------------

SenderFactory = Callable[[bool], "tekore.Sender"]
_sender_factory: SenderFactory | None = None


def set_sender_factory(factory: SenderFactory | None) -> None:
    """Install (or clear) the sender factory. Intended for tests."""
    global _sender_factory  # noqa: PLW0603
    _sender_factory = factory


def make_sender(asynchronous: bool) -> tekore.Sender | None:
    """Return a sender from the installed factory, or ``None`` for defaults."""
    return _sender_factory(asynchronous) if _sender_factory is not None else None


def make_client(token: tekore.Token | str, asynchronous: bool) -> tekore.Spotify:
    """Build a ``tekore.Spotify`` client, honouring the test seam.

    Every internal Spotify construction goes through here so a forgotten
    ``sender=`` can never silently escape the fake in tests.
    """
    sender = make_sender(asynchronous)
    if sender is not None:
        return tekore.Spotify(token, sender=sender)
    return tekore.Spotify(token, asynchronous=asynchronous)


# ---------------------------------------------------------------------------
# Required OAuth scopes (from PRD)
# ---------------------------------------------------------------------------

# ``user-follow-modify`` is here for core/following.py, which follows the
# artists behind the user's own liked songs. It is one scope for both
# artists and users, so it no longer makes following *listeners* a 403 —
# the policy against that has to hold in code now rather than in the
# grant. core/curators.py stays read-only on purpose and says why; a
# bulk follow of strangers does not belong behind this scope just
# because the scope would now permit it.
REQUIRED_SCOPES = tekore.Scope(
    "playlist-modify-public",
    "playlist-modify-private",
    "playlist-read-private",
    "playlist-read-collaborative",
    "user-library-read",
    "user-library-modify",  # core/library.py saves Discogs-owned records
    "user-top-read",
    "user-read-recently-played",
    "user-follow-read",
    "user-follow-modify",
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

        sender = make_sender(self._asynchronous)
        self._credentials = tekore.Credentials(
            client_id=self._client_id,
            client_secret=self._client_secret,
            redirect_uri=self._redirect_uri,
            sender=sender,
            asynchronous=None if sender is not None else self._asynchronous,
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

    # -- CLI login flow -----------------------------------------------------

    def begin_login(self) -> tuple[str, str]:
        """Start an interactive login: return ``(auth_url, state)``.

        The caller sends the user to *auth_url* and must pass *state*
        back to :meth:`complete_login` for CSRF verification.
        """
        state = generate_csrf_state()
        return self.get_auth_url(state=state), state

    def complete_login(self, redirect_url: str, expected_state: str) -> dict[str, Any]:
        """Finish an interactive login from the pasted redirect URL.

        Parses the authorization code and state from *redirect_url*,
        verifies the state against *expected_state*, exchanges the code
        for tokens (synchronously — this is the CLI path), persists the
        token in the configured store, and returns the user's profile.

        Returns a dict with ``user_id``, ``display_name``, ``email``,
        and ``product``.
        """
        from urllib.parse import parse_qs, urlparse

        query = parse_qs(urlparse(redirect_url).query)
        returned_state = query.get("state", [None])[0]
        if not verify_csrf_state(expected_state, returned_state):
            raise AuthenticationError(
                "OAuth state mismatch — the redirect URL does not belong to this login attempt."
            )
        code = query.get("code", [None])[0]
        if not code:
            error = query.get("error", ["missing authorization code"])[0]
            raise AuthenticationError(f"Spotify authorization failed: {error}")

        try:
            token: tekore.Token = self._credentials.request_user_token(code)
        except Exception as exc:
            raise AuthenticationError(f"Failed to exchange authorization code: {exc}") from exc

        client = make_client(token, asynchronous=False)
        try:
            user = client.current_user()
        except Exception as exc:
            raise AuthenticationError(f"Failed to fetch Spotify user profile: {exc}") from exc

        if self._token_store is not None:
            self._token_store.save_token(user.id, token)
            logger.info("Stored token for user %s", user.id)

        return {
            "user_id": user.id,
            "display_name": user.display_name,
            "email": getattr(user, "email", None),
            "product": getattr(user, "product", None),
        }

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
        return url


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


async def exchange_code(code: str) -> dict[str, Any]:
    """Exchange an authorization code for token info.

    CSRF state validation is the caller's responsibility (the web callback
    checks the state cookie before calling this).

    Returns a dict with ``access_token``, ``refresh_token``, and
    ``expires_at`` keys, compatible with what the web layer expects.
    """
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
    client = make_client(access_token, asynchronous=True)
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
