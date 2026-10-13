"""Root-level pytest fixtures shared across all SpotifyForge test modules.

Provides the in-memory fake Spotify backend, a keyring stand-in, and an
isolated local data directory.
"""

from __future__ import annotations

from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Fake Spotify backend + isolated local state
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    """Point every test at a throwaway data directory.

    Core code reads its on-disk caches from ``settings.data_dir``. Without
    this, a test run on a machine where the tool is in real use would read
    (and could write) the developer's own ``~/.spotifyforge``.
    """
    from spotifyforge.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)


@pytest.fixture()
def fake_spotify():
    """An in-memory Spotify backend, installed as the tekore sender seam.

    Every internally constructed tekore client (OAuth exchange, token
    refresh, profile fetch, API clients built by ``core.clients``) routes
    through this fake for the duration of the test.
    """
    import httpx
    import tekore as tk

    from spotifyforge.auth import oauth
    from tests.fake_spotify import FakeSpotify

    fake = FakeSpotify()

    def factory(asynchronous: bool) -> tk.Sender:
        if asynchronous:
            return tk.AsyncSender(client=httpx.AsyncClient(transport=fake.transport()))
        return tk.SyncSender(client=httpx.Client(transport=fake.transport()))

    oauth.set_sender_factory(factory)
    yield fake
    oauth.set_sender_factory(None)


class MemoryTokenStore:
    """A TokenStore backed by a plain dict — keyring stand-in for tests."""

    def __init__(self) -> None:
        self.tokens: dict[str, Any] = {}

    def save_token(self, user_id: str, token: Any) -> None:
        self.tokens[user_id] = token

    def load_token(self, user_id: str) -> Any:
        from spotifyforge.auth.oauth import TokenNotFoundError

        try:
            return self.tokens[user_id]
        except KeyError:
            raise TokenNotFoundError(f"No token for '{user_id}'") from None

    def delete_token(self, user_id: str) -> None:
        from spotifyforge.auth.oauth import TokenNotFoundError

        try:
            del self.tokens[user_id]
        except KeyError:
            raise TokenNotFoundError(f"No token for '{user_id}'") from None


@pytest.fixture()
def memory_token_store() -> MemoryTokenStore:
    return MemoryTokenStore()


@pytest.fixture()
def app_env(fake_spotify, monkeypatch):
    """Full app environment: fake Spotify, credentials, throwaway data dir.

    SpotifyAuth builds a fresh ``Settings()`` (env vars cover it); the
    settings singleton covers every other code path. ``data_dir`` is
    already redirected for every test (``_isolated_data_dir``).
    """
    monkeypatch.setenv("SPOTIFYFORGE_SPOTIFY_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("SPOTIFYFORGE_SPOTIFY_CLIENT_SECRET", "test-client-secret")
    from spotifyforge.config import settings

    monkeypatch.setattr(settings, "spotify_client_id", "test-client-id")
    monkeypatch.setattr(settings, "spotify_client_secret", "test-client-secret")
    return fake_spotify
