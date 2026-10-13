"""The logged-in user's session, for anything that is not the login flow.

``auth login`` leaves two things behind: a token in the OS keyring and a
marker file naming whose it is. Everything that talks to Spotify
afterwards — every CLI command and every MCP tool — starts from those
two, so the loading and refreshing lives here once and raises plain
exceptions; each front end decides how to show them.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Iterator
from pathlib import Path

import tekore

from spotifyforge.auth import oauth
from spotifyforge.auth.oauth import AuthenticationError, SpotifyAuth
from spotifyforge.config import settings

# A run must start with at least this much token life left. Sized to
# the longest command (a full-catalogue covers or reflow pass), not to
# tekore's is_expiring (<60s) — a bulk run that starts with eight
# minutes remaining dies 401 halfway through with its work unsaved.
TOKEN_HEADROOM_SECONDS = 45 * 60

# A refresh is one request; a lock older than this was left by a process
# that died holding it.
_STALE_LOCK_SECONDS = 60.0
_LOCK_WAIT_SECONDS = 30.0


class NotLoggedInError(AuthenticationError):
    """No user has run ``spotifyforge auth login`` on this machine."""


def current_user_file() -> Path:
    """The marker ``auth login`` writes: the Spotify id of the logged-in user."""
    return settings.data_dir / "current_user"


def current_user_id() -> str:
    """Return the Spotify user id of the logged-in user."""
    path = current_user_file()
    if not path.exists():
        raise NotLoggedInError("Not logged in. Run `spotifyforge auth login` first.")
    return path.read_text(encoding="utf-8").strip()


def make_auth() -> SpotifyAuth:
    """Build a keyring-backed :class:`SpotifyAuth`."""
    # Looked up on the module at call time: tests swap the keyring store out.
    return SpotifyAuth(token_store=oauth.KeyringTokenStore())


@contextlib.contextmanager
def _refresh_lock() -> Iterator[None]:
    """Let one process at a time refresh the shared token.

    Several spotifyforge processes can be alive at once (a scheduled
    history sync, the weekly maintenance run, an MCP server), all reading
    the same keyring entry. Two that refresh at the same moment each
    save a token the other did not expect. A directory is the lock
    because ``mkdir`` is atomic on every platform this runs on.
    """
    lock = settings.data_dir / "token-refresh.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + _LOCK_WAIT_SECONDS
    while True:
        try:
            lock.mkdir()
            break
        except FileExistsError:
            with contextlib.suppress(FileNotFoundError):
                if time.time() - lock.stat().st_mtime > _STALE_LOCK_SECONDS:
                    lock.rmdir()
                    continue
            if time.monotonic() > deadline:
                raise AuthenticationError(
                    "Timed out waiting for another spotifyforge process to refresh the token."
                ) from None
            time.sleep(0.2)
    try:
        yield
    finally:
        with contextlib.suppress(FileNotFoundError):
            lock.rmdir()


def load_token(auth: SpotifyAuth, spotify_user_id: str) -> tekore.Token:
    """Load the stored token, refreshed unless it can outlast a bulk run."""
    store = auth.token_store
    assert store is not None
    token = store.load_token(spotify_user_id)
    if token.expires_in >= TOKEN_HEADROOM_SECONDS:
        return token
    with _refresh_lock():
        # Whoever held the lock before us may have refreshed already.
        token = store.load_token(spotify_user_id)
        if token.expires_in >= TOKEN_HEADROOM_SECONDS:
            return token
        if token.refresh_token:
            token = auth.credentials.refresh_user_token(token.refresh_token)
            store.save_token(spotify_user_id, token)
        elif token.is_expiring:
            raise AuthenticationError("Stored token expired with no refresh token; log in again.")
    return token


def open_client() -> tekore.Spotify:
    """Return an authenticated async Spotify client for the logged-in user."""
    from spotifyforge.core.clients import build_spotify

    token = load_token(make_auth(), current_user_id())
    return build_spotify(token.access_token)
