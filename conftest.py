"""Root-level pytest fixtures shared across all SpotifyForge test modules.

Provides an in-memory SQLite database, session management with automatic
rollback, a mock Spotify client, and reusable sample data factories.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

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


# ---------------------------------------------------------------------------
# Mock Spotify client
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_spotify_client() -> AsyncMock:
    """Return an ``AsyncMock`` that mimics a ``tekore.Spotify`` client.

    Pre-configured return values cover the most commonly called methods so
    tests can run without network access.  Individual tests can override
    any method's ``return_value`` or ``side_effect`` as needed.

    Returns:
        An ``AsyncMock`` instance with sensible defaults for Spotify API calls.
    """
    client = AsyncMock(name="MockSpotifyClient")

    # -- Current user profile --
    client.current_user.return_value = AsyncMock(
        id="test_user_123",
        display_name="Test User",
        email="test@example.com",
        product="premium",
    )

    # -- Playlists --
    mock_playlist = AsyncMock(
        id="playlist_abc123",
        name="Test Playlist",
        description="A test playlist",
        public=True,
        collaborative=False,
        snapshot_id="snap_001",
        followers=AsyncMock(total=42),
        tracks=AsyncMock(total=10),
    )
    client.playlist.return_value = mock_playlist
    client.playlists.return_value = AsyncMock(items=[mock_playlist])

    # -- Tracks --
    mock_track = AsyncMock(
        id="track_xyz789",
        name="Test Track",
        uri="spotify:track:track_xyz789",
        duration_ms=210000,
        popularity=65,
        album=AsyncMock(
            id="album_def456",
            name="Test Album",
            release_date="2024-01-15",
        ),
        artists=[
            AsyncMock(id="artist_ghi012", name="Test Artist"),
        ],
    )
    client.track.return_value = mock_track
    client.tracks.return_value = [mock_track]

    # -- Audio features --
    mock_audio_features = AsyncMock(
        danceability=0.72,
        energy=0.85,
        valence=0.60,
        tempo=120.0,
        key=5,
        mode=1,
        loudness=-5.2,
        speechiness=0.04,
        acousticness=0.12,
        instrumentalness=0.001,
        liveness=0.09,
    )
    client.track_audio_features.return_value = mock_audio_features

    # -- Top tracks / artists --
    client.current_user_top_tracks.return_value = AsyncMock(items=[mock_track])
    client.current_user_top_artists.return_value = AsyncMock(
        items=[
            AsyncMock(
                id="artist_ghi012",
                name="Test Artist",
                genres=["indie-rock", "alternative"],
                popularity=72,
            ),
        ]
    )

    # -- Playlist mutation methods --
    client.playlist_create.return_value = mock_playlist
    client.playlist_add.return_value = AsyncMock(snapshot_id="snap_002")
    client.playlist_remove.return_value = AsyncMock(snapshot_id="snap_003")

    # -- Recommendations --
    client.recommendations.return_value = AsyncMock(tracks=[mock_track])

    return client


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
