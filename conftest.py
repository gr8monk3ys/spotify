"""Root-level pytest fixtures shared across all SpotifyForge test modules.

Provides an in-memory SQLite database, session management with automatic
rollback, a mock Spotify client, and reusable sample data factories.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import SessionTransaction
from sqlmodel import Session, SQLModel, create_engine

# ---------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_engine() -> Generator[Engine, None, None]:
    """Create a synchronous in-memory SQLite engine with all tables.

    A fresh database is created for each test that requests this fixture,
    ensuring complete isolation between tests.

    Yields:
        A ``sqlalchemy.engine.Engine`` backed by ``:memory:``.
    """
    # Import models so their metadata is registered with SQLModel.metadata
    import spotifyforge.models.models as _models  # noqa: F401

    engine = create_engine(
        "sqlite:///:memory:",
        echo=False,
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    yield engine
    SQLModel.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture()
def db_session(db_engine: Engine) -> Generator[Session, None, None]:
    """Yield a SQLModel ``Session`` that rolls back after each test.

    This fixture wraps the session in a transaction that is always rolled
    back when the test completes, so tests never persist changes to the
    in-memory database.  This guarantees each test starts with a clean
    slate even when sharing the same ``db_engine`` fixture.

    Yields:
        A ``sqlmodel.Session`` bound to an in-memory SQLite engine.
    """
    connection = db_engine.connect()
    transaction = connection.begin()

    session = Session(bind=connection)

    # Start a nested (SAVEPOINT) transaction so that ``session.commit()``
    # inside the code under test creates a savepoint instead of actually
    # committing.  When the outer transaction is rolled back, everything
    # is undone.
    nested = connection.begin_nested()

    @event.listens_for(session, "after_transaction_end")
    def restart_savepoint(session: Session, transaction_inner: SessionTransaction) -> None:  # noqa: N803
        nonlocal nested
        if transaction_inner.nested and not transaction_inner.parent.nested:
            nested = connection.begin_nested()

    yield session

    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture(autouse=True)
def _clear_crypto_caches():
    """Reset cached secret/Fernet derivations between tests.

    Production caches these for the process lifetime; tests monkeypatch
    settings, so stale cache entries would leak across tests.
    """
    from spotifyforge import security

    security._get_secret.cache_clear()
    security._get_fernet.cache_clear()
    yield
    security._get_secret.cache_clear()
    security._get_fernet.cache_clear()


# ---------------------------------------------------------------------------
# Fake Spotify backend + isolated app database
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    """Point the application at a fresh on-disk SQLite DB for this test.

    Patches the settings singleton (env vars are read only at import time)
    and resets the cached engines so both sync and async engines rebuild.
    """
    from spotifyforge.config import settings
    from spotifyforge.db import engine as engine_mod

    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path}/app.db")
    engine_mod.reset_engines()
    engine_mod.init_db()
    yield
    engine_mod.reset_engines()


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
def app_env(isolated_db, fake_spotify, monkeypatch):
    """Full app environment: fresh DB, fake Spotify, credentials, clean scheduler.

    SpotifyAuth builds a fresh ``Settings()`` (env vars cover it); the
    settings singleton covers every other code path. The scheduler
    singleton is reset before and stopped after each test.
    """
    monkeypatch.setenv("SPOTIFYFORGE_SPOTIFY_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("SPOTIFYFORGE_SPOTIFY_CLIENT_SECRET", "test-client-secret")
    from spotifyforge.config import settings
    from spotifyforge.core import scheduler as scheduler_mod

    monkeypatch.setattr(settings, "spotify_client_id", "test-client-id")
    monkeypatch.setattr(settings, "spotify_client_secret", "test-client-secret")
    monkeypatch.setattr(scheduler_mod, "_service", None)
    yield fake_spotify
    service = scheduler_mod._service
    if service is not None and service.is_running:
        service.stop(wait=False)
    monkeypatch.setattr(scheduler_mod, "_service", None)


# ---------------------------------------------------------------------------
# Sample data fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sample_track_data() -> dict[str, Any]:
    """Return a dictionary of sample track data matching the Track model schema.

    Useful for constructing ``Track`` model instances or simulating API
    responses in tests.

    Returns:
        A ``dict`` with keys corresponding to ``Track`` model fields.
    """
    return {
        "spotify_id": "6rqhFgbbKwnb9MLmUQDhG6",
        "name": "Bohemian Rhapsody",
        "artist_names": ["Queen"],
        "album_name": "A Night at the Opera",
        "album_id": "1GbtB4zTqAsyfZEsm1RZfx",
        "duration_ms": 354947,
        "popularity": 89,
        "isrc": "GBUM71029604",
        "cached_at": datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC),
    }


@pytest.fixture()
def sample_playlist_data() -> dict[str, Any]:
    """Return a dictionary of sample playlist data matching the Playlist model schema.

    Useful for constructing ``Playlist`` model instances or simulating API
    responses in tests.

    Returns:
        A ``dict`` with keys corresponding to ``Playlist`` model fields.
    """
    return {
        "spotify_id": "37i9dQZF1DXcBWIGoYBM5M",
        "owner_id": 1,
        "name": "Today's Top Hits",
        "description": "The biggest songs right now.",
        "public": True,
        "collaborative": False,
        "snapshot_id": "MTY4ODQ2MDAwMCwwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA",
        "follower_count": 35_000_000,
        "track_count": 50,
        "last_synced_at": datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC),
        "created_at": datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC),
        "updated_at": datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC),
    }
