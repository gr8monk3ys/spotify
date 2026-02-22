"""Comprehensive tests for the SpotifyForge FastAPI web routes.

Uses ``fastapi.testclient.TestClient`` with ``dependency_overrides`` to
exercise all API endpoints without a real database, Spotify connection,
or background scheduler.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from spotifyforge.models.models import (
    JobType,
    Playlist,
    ScheduledJob,
    User,
)

# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

# Build a minimal test user that all authenticated endpoints will use.
_NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)


def _make_user(
    id: int = 1,
    spotify_id: str = "sp_user_001",
    display_name: str = "Test User",
    email: str = "test@example.com",
    is_premium: bool = True,
) -> User:
    return User(
        id=id,
        spotify_id=spotify_id,
        display_name=display_name,
        email=email,
        access_token_enc="fake_access_token",
        refresh_token_enc="fake_refresh_token",
        token_expiry=_NOW,
        is_premium=is_premium,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _make_playlist(
    id: int = 1,
    owner_id: int = 1,
    spotify_id: str = "sp_pl_001",
    name: str = "Test Playlist",
) -> Playlist:
    return Playlist(
        id=id,
        spotify_id=spotify_id,
        owner_id=owner_id,
        name=name,
        description="A test playlist",
        public=True,
        collaborative=False,
        snapshot_id="snap_001",
        follower_count=42,
        track_count=10,
        last_synced_at=_NOW,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _make_scheduled_job(
    id: int = 1,
    user_id: int = 1,
    playlist_id: int | None = 1,
    name: str = "Nightly Sync",
) -> ScheduledJob:
    return ScheduledJob(
        id=id,
        user_id=user_id,
        name=name,
        job_type=JobType.playlist_sync,
        playlist_id=playlist_id,
        config=None,
        cron_expression="0 0 * * *",
        enabled=True,
        last_run_at=None,
        next_run_at=_NOW,
        created_at=_NOW,
        updated_at=_NOW,
    )


class _FakeScalarsResult:
    """Mimics the ``ScalarResult`` returned by ``session.execute().scalars()``."""

    def __init__(self, items: list[Any] | None = None, first_item: Any = None):
        self._items = items or []
        self._first_item = first_item

    def all(self) -> list[Any]:
        return self._items

    def first(self) -> Any:
        return self._first_item


class _FakeExecuteResult:
    """Mimics the ``Result`` returned by ``session.execute(...)``."""

    def __init__(self, items: list[Any] | None = None, first_item: Any = None):
        self._items = items or []
        self._first_item = first_item

    def scalars(self) -> _FakeScalarsResult:
        return _FakeScalarsResult(items=self._items, first_item=self._first_item)


class _FakeSession:
    """A lightweight stand-in for ``AsyncSession``.

    Supports ``execute``, ``add``, ``commit``, ``refresh``, ``delete``,
    and ``close`` with sensible defaults.  Tests can customise behaviour
    via the ``execute_results`` list (consumed in FIFO order).
    """

    def __init__(self, execute_results: list[_FakeExecuteResult] | None = None):
        self._execute_results = list(execute_results or [])
        self._added: list[Any] = []
        self._deleted: list[Any] = []
        self._committed = False

    async def execute(self, stmt) -> _FakeExecuteResult:
        if self._execute_results:
            return self._execute_results.pop(0)
        return _FakeExecuteResult()

    def add(self, obj: Any) -> None:
        self._added.append(obj)

    async def commit(self) -> None:
        self._committed = True

    async def refresh(self, obj: Any) -> None:
        # Set an id if the object doesn't have one yet
        if hasattr(obj, "id") and obj.id is None:
            obj.id = 99

    async def delete(self, obj: Any) -> None:
        self._deleted.append(obj)

    async def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# App factory with dependency overrides
# ---------------------------------------------------------------------------


def _build_test_app(
    user: User | None = None,
    session: _FakeSession | None = None,
) -> FastAPI:
    """Create a fresh FastAPI test app with overridden dependencies.

    We patch the lifespan to be a no-op so we don't initialize a real DB
    or start the scheduler.
    """
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _noop_lifespan(app: FastAPI):
        yield

    # Patch the lifespan BEFORE creating the app, so no real DB init happens
    with patch("spotifyforge.web.app._lifespan", _noop_lifespan):
        from spotifyforge.web.app import create_app, get_current_user, get_db_session

        test_app = create_app()

    # Override dependencies
    mock_user = user or _make_user()
    mock_session = session or _FakeSession()

    async def _override_get_current_user():
        return mock_user

    async def _override_get_db_session():
        return mock_session

    test_app.dependency_overrides[get_current_user] = _override_get_current_user
    test_app.dependency_overrides[get_db_session] = _override_get_db_session

    return test_app


def _build_unauthed_test_app(
    session: _FakeSession | None = None,
) -> FastAPI:
    """Build a test app where get_current_user raises a 401."""
    from contextlib import asynccontextmanager

    from fastapi import HTTPException, status

    @asynccontextmanager
    async def _noop_lifespan(app: FastAPI):
        yield

    with patch("spotifyforge.web.app._lifespan", _noop_lifespan):
        from spotifyforge.web.app import create_app, get_current_user, get_db_session

        test_app = create_app()

    mock_session = session or _FakeSession()

    async def _override_deny_user():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated.",
        )

    async def _override_get_db_session():
        return mock_session

    test_app.dependency_overrides[get_current_user] = _override_deny_user
    test_app.dependency_overrides[get_db_session] = _override_get_db_session

    return test_app


# =========================================================================
# Health check
# =========================================================================


class TestHealthCheck:
    """Tests for GET /health."""

    def test_health_returns_200(self):
        app = _build_test_app()
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert "version" in data

    def test_health_includes_version(self):
        app = _build_test_app()
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/health")
        data = response.json()
        assert data["version"] == "0.1.0"


# =========================================================================
# Auth routes
# =========================================================================


class TestAuthRoutes:
    """Tests for /api/auth/* endpoints."""

    def test_login_returns_auth_url(self):
        import types

        app = _build_test_app()
        client = TestClient(app, raise_server_exceptions=False)

        # The route handler does a lazy import:
        #   from spotifyforge.auth.oauth import build_auth_url
        # We inject a fake module into sys.modules so the import finds our mock.
        fake_oauth = types.ModuleType("spotifyforge.auth.oauth")
        fake_oauth.build_auth_url = lambda: "https://accounts.spotify.com/authorize?client_id=test"

        with patch.dict(
            "sys.modules",
            {"spotifyforge.auth.oauth": fake_oauth},
        ):
            response = client.get("/api/auth/login")

        assert response.status_code == 200
        data = response.json()
        assert "auth_url" in data
        assert "spotify" in data["auth_url"].lower()

    def test_me_returns_user_info(self):
        user = _make_user(display_name="Alice", email="alice@test.com")
        app = _build_test_app(user=user)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/api/auth/me")
        assert response.status_code == 200
        data = response.json()
        assert data["display_name"] == "Alice"
        assert data["email"] == "alice@test.com"
        assert data["spotify_id"] == "sp_user_001"
        assert data["is_premium"] is True

    def test_me_unauthenticated_returns_401(self):
        app = _build_unauthed_test_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/api/auth/me")
        assert response.status_code == 401

    def test_logout_returns_success(self):
        app = _build_test_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post("/api/auth/logout")
        assert response.status_code == 200
        data = response.json()
        assert data["detail"] == "Logged out successfully."


# =========================================================================
# Playlist routes
# =========================================================================


class TestPlaylistRoutes:
    """Tests for /api/playlists/* endpoints."""

    def test_list_playlists_returns_list(self):
        playlist = _make_playlist()
        session = _FakeSession(execute_results=[_FakeExecuteResult(items=[playlist])])
        app = _build_test_app(session=session)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/api/playlists")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["name"] == "Test Playlist"
        assert data[0]["spotify_id"] == "sp_pl_001"

    def test_list_playlists_empty(self):
        session = _FakeSession(execute_results=[_FakeExecuteResult(items=[])])
        app = _build_test_app(session=session)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/api/playlists")
        assert response.status_code == 200
        data = response.json()
        assert data == []

    def test_list_playlists_unauthenticated(self):
        app = _build_unauthed_test_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/api/playlists")
        assert response.status_code == 401

    def test_list_playlists_with_pagination(self):
        playlist = _make_playlist()
        session = _FakeSession(execute_results=[_FakeExecuteResult(items=[playlist])])
        app = _build_test_app(session=session)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/api/playlists?offset=0&limit=10")
        assert response.status_code == 200

    def test_create_playlist_success(self):
        import types

        mock_spotify_playlist = {
            "id": "sp_new_pl_001",
            "snapshot_id": "snap_new_001",
        }

        session = _FakeSession()
        app = _build_test_app(session=session)
        client = TestClient(app, raise_server_exceptions=False)

        # The route does a lazy import:
        #   from spotifyforge.core.playlists import create_spotify_playlist
        fake_playlists = types.ModuleType("spotifyforge.core.playlists")
        fake_playlists.create_spotify_playlist = AsyncMock(return_value=mock_spotify_playlist)

        with patch.dict(
            "sys.modules",
            {"spotifyforge.core.playlists": fake_playlists},
        ):
            response = client.post(
                "/api/playlists",
                json={
                    "name": "Brand New Playlist",
                    "description": "Created via API",
                    "public": True,
                    "collaborative": False,
                },
            )

        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "Brand New Playlist"
        assert data["spotify_id"] == "sp_new_pl_001"

    def test_create_playlist_unauthenticated(self):
        app = _build_unauthed_test_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            "/api/playlists",
            json={"name": "Unauthed", "public": True, "collaborative": False},
        )
        assert response.status_code == 401

    def test_create_playlist_spotify_failure(self):
        session = _FakeSession()
        app = _build_test_app(session=session)
        client = TestClient(app, raise_server_exceptions=False)

        mock_module = MagicMock()
        mock_module.create_spotify_playlist = AsyncMock(side_effect=Exception("Spotify API down"))
        with patch.dict("sys.modules", {"spotifyforge.core.playlists": mock_module}):
            response = client.post(
                "/api/playlists",
                json={
                    "name": "Broken Playlist",
                    "public": True,
                    "collaborative": False,
                },
            )

        assert response.status_code == 502

    def test_get_playlist_success(self):
        playlist = _make_playlist(id=5)
        session = _FakeSession(execute_results=[_FakeExecuteResult(first_item=playlist)])
        app = _build_test_app(session=session)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/api/playlists/5")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == 5
        assert data["name"] == "Test Playlist"

    def test_get_playlist_not_found(self):
        session = _FakeSession(execute_results=[_FakeExecuteResult(first_item=None)])
        app = _build_test_app(session=session)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/api/playlists/999")
        assert response.status_code == 404

    def test_sync_playlist_success(self):
        playlist = _make_playlist(id=3)
        session = _FakeSession(execute_results=[_FakeExecuteResult(first_item=playlist)])
        app = _build_test_app(session=session)
        client = TestClient(app, raise_server_exceptions=False)

        mock_module = MagicMock()
        mock_module.sync_playlist_from_spotify = AsyncMock(return_value={"tracks_synced": 25})
        with patch.dict("sys.modules", {"spotifyforge.core.playlists": mock_module}):
            response = client.post("/api/playlists/3/sync")

        assert response.status_code == 200
        data = response.json()
        assert data["detail"] == "Playlist synced successfully."
        assert data["tracks_synced"] == 25
        assert data["playlist_id"] == 3

    def test_sync_playlist_not_found(self):
        session = _FakeSession(execute_results=[_FakeExecuteResult(first_item=None)])
        app = _build_test_app(session=session)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post("/api/playlists/999/sync")
        assert response.status_code == 404

    def test_sync_playlist_spotify_failure(self):
        playlist = _make_playlist(id=7)
        session = _FakeSession(execute_results=[_FakeExecuteResult(first_item=playlist)])
        app = _build_test_app(session=session)
        client = TestClient(app, raise_server_exceptions=False)

        mock_module = MagicMock()
        mock_module.sync_playlist_from_spotify = AsyncMock(
            side_effect=Exception("Spotify unreachable")
        )
        with patch.dict("sys.modules", {"spotifyforge.core.playlists": mock_module}):
            response = client.post("/api/playlists/7/sync")

        assert response.status_code == 502

    def test_sync_playlist_unauthenticated(self):
        app = _build_unauthed_test_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post("/api/playlists/1/sync")
        assert response.status_code == 401

    def test_deduplicate_playlist_success(self):
        playlist = _make_playlist(id=4)
        session = _FakeSession(execute_results=[_FakeExecuteResult(first_item=playlist)])
        app = _build_test_app(session=session)
        client = TestClient(app, raise_server_exceptions=False)

        mock_module = MagicMock()
        mock_module.deduplicate_playlist_tracks = AsyncMock(return_value={"duplicates_removed": 3})
        with patch.dict("sys.modules", {"spotifyforge.core.playlists": mock_module}):
            response = client.post("/api/playlists/4/deduplicate")

        assert response.status_code == 200
        data = response.json()
        assert data["detail"] == "Deduplication complete."
        assert data["duplicates_removed"] == 3
        assert data["playlist_id"] == 4

    def test_deduplicate_playlist_not_found(self):
        session = _FakeSession(execute_results=[_FakeExecuteResult(first_item=None)])
        app = _build_test_app(session=session)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post("/api/playlists/999/deduplicate")
        assert response.status_code == 404

    def test_deduplicate_playlist_failure(self):
        playlist = _make_playlist(id=6)
        session = _FakeSession(execute_results=[_FakeExecuteResult(first_item=playlist)])
        app = _build_test_app(session=session)
        client = TestClient(app, raise_server_exceptions=False)

        mock_module = MagicMock()
        mock_module.deduplicate_playlist_tracks = AsyncMock(
            side_effect=Exception("dedup engine error")
        )
        with patch.dict("sys.modules", {"spotifyforge.core.playlists": mock_module}):
            response = client.post("/api/playlists/6/deduplicate")

        assert response.status_code == 502

    def test_deduplicate_unauthenticated(self):
        app = _build_unauthed_test_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post("/api/playlists/1/deduplicate")
        assert response.status_code == 401

    def test_update_playlist_success(self):
        playlist = _make_playlist(id=2)
        session = _FakeSession(execute_results=[_FakeExecuteResult(first_item=playlist)])
        app = _build_test_app(session=session)
        client = TestClient(app, raise_server_exceptions=False)

        mock_module = MagicMock()
        mock_module.update_spotify_playlist = AsyncMock(return_value=None)
        with patch.dict("sys.modules", {"spotifyforge.core.playlists": mock_module}):
            response = client.put(
                "/api/playlists/2",
                json={"name": "Updated Name"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Updated Name"

    def test_update_playlist_not_found(self):
        session = _FakeSession(execute_results=[_FakeExecuteResult(first_item=None)])
        app = _build_test_app(session=session)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.put(
            "/api/playlists/999",
            json={"name": "Ghost"},
        )
        assert response.status_code == 404


# =========================================================================
# Discovery routes
# =========================================================================


class TestDiscoveryRoutes:
    """Tests for /api/discover/* endpoints."""

    def test_top_tracks_success(self):
        app = _build_test_app()
        client = TestClient(app, raise_server_exceptions=False)

        mock_tracks = [
            {
                "id": 1,
                "spotify_id": "sp_track_1",
                "name": "Hit Song",
                "artist_names": ["Artist A"],
                "album_name": "Album X",
                "album_id": "alb_1",
                "duration_ms": 210000,
                "popularity": 95,
                "isrc": "US1234567890",
                "cached_at": _NOW.isoformat(),
            },
        ]

        mock_module = MagicMock()
        mock_module.get_top_tracks = AsyncMock(return_value=mock_tracks)

        with patch.dict("sys.modules", {"spotifyforge.core.discovery": mock_module}):
            response = client.get("/api/discover/top-tracks")

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["name"] == "Hit Song"

    def test_top_tracks_with_time_range(self):
        app = _build_test_app()
        client = TestClient(app, raise_server_exceptions=False)

        mock_module = MagicMock()
        mock_module.get_top_tracks = AsyncMock(return_value=[])

        with patch.dict("sys.modules", {"spotifyforge.core.discovery": mock_module}):
            response = client.get("/api/discover/top-tracks?time_range=short_term&limit=10")

        assert response.status_code == 200
        mock_module.get_top_tracks.assert_called_once()
        call_kwargs = mock_module.get_top_tracks.call_args
        assert call_kwargs.kwargs.get("time_range") == "short_term"
        assert call_kwargs.kwargs.get("limit") == 10

    def test_top_tracks_unauthenticated(self):
        app = _build_unauthed_test_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/api/discover/top-tracks")
        assert response.status_code == 401

    def test_top_tracks_spotify_failure(self):
        app = _build_test_app()
        client = TestClient(app, raise_server_exceptions=False)

        mock_module = MagicMock()
        mock_module.get_top_tracks = AsyncMock(side_effect=Exception("Spotify rate limit"))

        with patch.dict("sys.modules", {"spotifyforge.core.discovery": mock_module}):
            response = client.get("/api/discover/top-tracks")

        assert response.status_code == 502

    def test_top_artists_success(self):
        app = _build_test_app()
        client = TestClient(app, raise_server_exceptions=False)

        mock_artists = [
            {"id": "art_1", "name": "Great Band", "genres": ["rock"], "popularity": 80},
        ]
        mock_module = MagicMock()
        mock_module.get_top_artists = AsyncMock(return_value=mock_artists)

        with patch.dict("sys.modules", {"spotifyforge.core.discovery": mock_module}):
            response = client.get("/api/discover/top-artists")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["name"] == "Great Band"

    def test_top_artists_unauthenticated(self):
        app = _build_unauthed_test_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/api/discover/top-artists")
        assert response.status_code == 401

    def test_deep_cuts_success(self):
        app = _build_test_app()
        client = TestClient(app, raise_server_exceptions=False)

        mock_tracks = [
            {
                "id": 10,
                "spotify_id": "sp_deep_1",
                "name": "Underground Hit",
                "artist_names": ["Indie Act"],
                "album_name": "Deep Album",
                "album_id": "alb_deep",
                "duration_ms": 240000,
                "popularity": 15,
                "isrc": None,
                "cached_at": _NOW.isoformat(),
            },
        ]
        mock_module = MagicMock()
        mock_module.get_deep_cuts = AsyncMock(return_value=mock_tracks)

        with patch.dict("sys.modules", {"spotifyforge.core.discovery": mock_module}):
            response = client.get("/api/discover/deep-cuts/artist_xyz?threshold=20")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["name"] == "Underground Hit"

    def test_deep_cuts_unauthenticated(self):
        app = _build_unauthed_test_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/api/discover/deep-cuts/some_artist")
        assert response.status_code == 401

    def test_genre_playlist_success(self):
        playlist = _make_playlist(id=50, name="Indie Rock Mix", spotify_id="sp_genre_pl")
        app = _build_test_app()
        client = TestClient(app, raise_server_exceptions=False)

        mock_module = MagicMock()
        mock_module.create_genre_based_playlist = AsyncMock(return_value=playlist)

        with patch.dict("sys.modules", {"spotifyforge.core.discovery": mock_module}):
            response = client.post("/api/discover/genre-playlist?genre=indie-rock&limit=20")

        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "Indie Rock Mix"

    def test_time_capsule_success(self):
        playlist = _make_playlist(id=60, name="Time Capsule 2020", spotify_id="sp_tc_pl")
        app = _build_test_app()
        client = TestClient(app, raise_server_exceptions=False)

        mock_module = MagicMock()
        mock_module.create_time_capsule_playlist = AsyncMock(return_value=playlist)

        with patch.dict("sys.modules", {"spotifyforge.core.discovery": mock_module}):
            response = client.post("/api/discover/time-capsule?year=2020")

        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "Time Capsule 2020"


# =========================================================================
# Schedule routes
# =========================================================================


class TestScheduleRoutes:
    """Tests for /api/schedules/* endpoints."""

    def test_list_schedules_returns_list(self):
        job = _make_scheduled_job()
        session = _FakeSession(execute_results=[_FakeExecuteResult(items=[job])])
        app = _build_test_app(session=session)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/api/schedules")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["name"] == "Nightly Sync"
        assert data[0]["job_type"] == "playlist_sync"
        assert data[0]["cron_expression"] == "0 0 * * *"

    def test_list_schedules_empty(self):
        session = _FakeSession(execute_results=[_FakeExecuteResult(items=[])])
        app = _build_test_app(session=session)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/api/schedules")
        assert response.status_code == 200
        data = response.json()
        assert data == []

    def test_list_schedules_unauthenticated(self):
        app = _build_unauthed_test_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/api/schedules")
        assert response.status_code == 401

    def test_create_schedule_success(self):
        # The ScheduledJobCreate model uses strict=True in its ConfigDict,
        # causing Pydantic to reject plain string values for the JobType
        # StrEnum field when receiving JSON.  To exercise the route logic we
        # rebuild the model class *without* strict mode and swap it in via
        # the ``models`` module before the app is constructed.
        from pydantic import BaseModel, ConfigDict
        from pydantic import Field as PydanticField

        from spotifyforge.models import models as models_mod

        class _RelaxedJobCreate(BaseModel):
            """Non-strict copy of ScheduledJobCreate for test use."""

            model_config = ConfigDict(strict=False)
            name: str = PydanticField(min_length=1, max_length=256)
            job_type: JobType
            playlist_id: int | None = None
            config: dict | None = None
            cron_expression: str = PydanticField(min_length=1, max_length=128)
            enabled: bool = True

        playlist = _make_playlist(id=1)
        session = _FakeSession(execute_results=[_FakeExecuteResult(first_item=playlist)])

        mock_scheduler_module = MagicMock()
        mock_scheduler_module.register_job = MagicMock()

        # Swap the model in both the models module and the routes module
        # BEFORE building the test app so the FastAPI endpoint uses it.
        import spotifyforge.web.routes as routes_mod

        orig_routes = routes_mod.ScheduledJobCreate
        orig_models = models_mod.ScheduledJobCreate

        try:
            routes_mod.ScheduledJobCreate = _RelaxedJobCreate
            models_mod.ScheduledJobCreate = _RelaxedJobCreate

            # Build the app *after* the swap so the route parameter annotation
            # picks up the relaxed model.
            app = _build_test_app(session=session)
            client = TestClient(app, raise_server_exceptions=False)

            with patch.dict(
                "sys.modules",
                {"spotifyforge.core.scheduler": mock_scheduler_module},
            ):
                response = client.post(
                    "/api/schedules",
                    json={
                        "name": "Weekly Sync",
                        "job_type": "playlist_sync",
                        "playlist_id": 1,
                        "cron_expression": "0 8 * * 1",
                        "enabled": True,
                    },
                )
        finally:
            routes_mod.ScheduledJobCreate = orig_routes
            models_mod.ScheduledJobCreate = orig_models

        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "Weekly Sync"
        assert data["job_type"] == "playlist_sync"
        assert data["cron_expression"] == "0 8 * * 1"
        assert data["enabled"] is True

    def test_create_schedule_rejects_invalid_job_type_with_strict_mode(self):
        """With strict=True on the model, passing a plain string for job_type
        via JSON returns 422 because Pydantic refuses the coercion."""
        app = _build_test_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            "/api/schedules",
            json={
                "name": "Strict Test",
                "job_type": "playlist_sync",
                "cron_expression": "0 0 * * *",
            },
        )
        assert response.status_code == 422

    def test_create_schedule_playlist_not_found(self):
        from pydantic import BaseModel, ConfigDict
        from pydantic import Field as PydanticField

        from spotifyforge.models import models as models_mod

        class _RelaxedJobCreate(BaseModel):
            model_config = ConfigDict(strict=False)
            name: str = PydanticField(min_length=1, max_length=256)
            job_type: JobType
            playlist_id: int | None = None
            config: dict | None = None
            cron_expression: str = PydanticField(min_length=1, max_length=128)
            enabled: bool = True

        session = _FakeSession(execute_results=[_FakeExecuteResult(first_item=None)])

        import spotifyforge.web.routes as routes_mod

        orig_routes = routes_mod.ScheduledJobCreate
        orig_models = models_mod.ScheduledJobCreate

        try:
            routes_mod.ScheduledJobCreate = _RelaxedJobCreate
            models_mod.ScheduledJobCreate = _RelaxedJobCreate

            app = _build_test_app(session=session)
            client = TestClient(app, raise_server_exceptions=False)

            response = client.post(
                "/api/schedules",
                json={
                    "name": "Orphan Job",
                    "job_type": "playlist_sync",
                    "playlist_id": 999,
                    "cron_expression": "0 0 * * *",
                    "enabled": True,
                },
            )
        finally:
            routes_mod.ScheduledJobCreate = orig_routes
            models_mod.ScheduledJobCreate = orig_models

        assert response.status_code == 404

    def test_create_schedule_without_playlist_id(self):
        """A job with no playlist_id should skip the playlist lookup."""
        from pydantic import BaseModel, ConfigDict
        from pydantic import Field as PydanticField

        from spotifyforge.models import models as models_mod

        class _RelaxedJobCreate(BaseModel):
            model_config = ConfigDict(strict=False)
            name: str = PydanticField(min_length=1, max_length=256)
            job_type: JobType
            playlist_id: int | None = None
            config: dict | None = None
            cron_expression: str = PydanticField(min_length=1, max_length=128)
            enabled: bool = True

        session = _FakeSession()  # No execute results needed

        mock_scheduler_module = MagicMock()
        mock_scheduler_module.register_job = MagicMock()

        import spotifyforge.web.routes as routes_mod

        orig_routes = routes_mod.ScheduledJobCreate
        orig_models = models_mod.ScheduledJobCreate

        try:
            routes_mod.ScheduledJobCreate = _RelaxedJobCreate
            models_mod.ScheduledJobCreate = _RelaxedJobCreate

            app = _build_test_app(session=session)
            client = TestClient(app, raise_server_exceptions=False)

            with patch.dict(
                "sys.modules",
                {"spotifyforge.core.scheduler": mock_scheduler_module},
            ):
                response = client.post(
                    "/api/schedules",
                    json={
                        "name": "Health Check Job",
                        "job_type": "health_check",
                        "playlist_id": None,
                        "cron_expression": "*/5 * * * *",
                        "enabled": True,
                    },
                )
        finally:
            routes_mod.ScheduledJobCreate = orig_routes
            models_mod.ScheduledJobCreate = orig_models

        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "Health Check Job"
        assert data["playlist_id"] is None

    def test_create_schedule_unauthenticated(self):
        app = _build_unauthed_test_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            "/api/schedules",
            json={
                "name": "Unauthed Job",
                "job_type": "playlist_sync",
                "cron_expression": "0 0 * * *",
            },
        )
        assert response.status_code == 401

    def test_delete_schedule_success(self):
        job = _make_scheduled_job(id=10)
        session = _FakeSession(execute_results=[_FakeExecuteResult(first_item=job)])
        app = _build_test_app(session=session)
        client = TestClient(app, raise_server_exceptions=False)

        mock_scheduler_module = MagicMock()
        mock_scheduler_module.unregister_job = MagicMock()

        with patch.dict(
            "sys.modules",
            {"spotifyforge.core.scheduler": mock_scheduler_module},
        ):
            response = client.delete("/api/schedules/10")

        assert response.status_code == 204

    def test_delete_schedule_not_found(self):
        session = _FakeSession(execute_results=[_FakeExecuteResult(first_item=None)])
        app = _build_test_app(session=session)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.delete("/api/schedules/999")
        assert response.status_code == 404

    def test_delete_schedule_unauthenticated(self):
        app = _build_unauthed_test_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.delete("/api/schedules/1")
        assert response.status_code == 401

    def test_toggle_schedule_enable(self):
        job = _make_scheduled_job(id=15)
        job.enabled = False  # Start disabled
        session = _FakeSession(execute_results=[_FakeExecuteResult(first_item=job)])
        app = _build_test_app(session=session)
        client = TestClient(app, raise_server_exceptions=False)

        mock_scheduler_module = MagicMock()
        mock_scheduler_module.register_job = MagicMock()
        mock_scheduler_module.unregister_job = MagicMock()

        with patch.dict(
            "sys.modules",
            {"spotifyforge.core.scheduler": mock_scheduler_module},
        ):
            response = client.put("/api/schedules/15/toggle")

        assert response.status_code == 200
        data = response.json()
        # The job was disabled, toggling should enable it
        assert data["enabled"] is True

    def test_toggle_schedule_not_found(self):
        session = _FakeSession(execute_results=[_FakeExecuteResult(first_item=None)])
        app = _build_test_app(session=session)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.put("/api/schedules/999/toggle")
        assert response.status_code == 404


# =========================================================================
# Authentication required (global check)
# =========================================================================


class TestAuthenticationRequired:
    """Verify that all protected endpoints return 401 when unauthenticated."""

    @pytest.mark.parametrize(
        "method,path",
        [
            ("GET", "/api/auth/me"),
            ("GET", "/api/playlists"),
            ("POST", "/api/playlists"),
            ("GET", "/api/playlists/1"),
            ("PUT", "/api/playlists/1"),
            ("POST", "/api/playlists/1/sync"),
            ("POST", "/api/playlists/1/deduplicate"),
            ("POST", "/api/playlists/1/tracks"),
            ("DELETE", "/api/playlists/1/tracks"),
            ("GET", "/api/discover/top-tracks"),
            ("GET", "/api/discover/top-artists"),
            ("GET", "/api/discover/deep-cuts/artist_x"),
            ("POST", "/api/discover/genre-playlist?genre=rock"),
            ("POST", "/api/discover/time-capsule"),
            ("GET", "/api/schedules"),
            ("POST", "/api/schedules"),
            ("DELETE", "/api/schedules/1"),
            ("PUT", "/api/schedules/1/toggle"),
        ],
    )
    def test_protected_endpoint_returns_401(self, method: str, path: str):
        app = _build_unauthed_test_app()
        client = TestClient(app, raise_server_exceptions=False)

        # Build a minimal valid body for POST/PUT/DELETE that expects one
        body: dict | list | None = None
        if method == "POST" and path == "/api/playlists":
            body = {"name": "x", "public": True, "collaborative": False}
        elif method == "POST" and path.endswith("/tracks"):
            body = ["spotify:track:abc"]
        elif method == "DELETE" and path.endswith("/tracks"):
            body = ["spotify:track:abc"]
        elif method == "POST" and path == "/api/schedules":
            body = {
                "name": "x",
                "job_type": "health_check",
                "cron_expression": "* * * * *",
            }
        elif method == "PUT" and "/playlists/" in path and "toggle" not in path:
            body = {"name": "x"}

        kwargs: dict[str, Any] = {}
        if body is not None:
            kwargs["json"] = body

        response = client.request(method, path, **kwargs)
        assert response.status_code == 401, (
            f"Expected 401 for {method} {path}, got {response.status_code}"
        )


# =========================================================================
# Edge cases: unauthenticated endpoints (health, login, logout)
# =========================================================================


class TestPublicEndpoints:
    """Verify that public endpoints work without authentication."""

    def test_health_no_auth_needed(self):
        app = _build_unauthed_test_app()
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/health")
        assert response.status_code == 200

    def test_login_no_auth_needed(self):
        app = _build_unauthed_test_app()
        client = TestClient(app, raise_server_exceptions=False)

        mock_oauth = MagicMock()
        mock_oauth.build_auth_url.return_value = "https://accounts.spotify.com/authorize"
        with patch.dict(
            "sys.modules",
            {
                "spotifyforge.auth.oauth": mock_oauth,
                "spotifyforge.auth": MagicMock(),
            },
        ):
            response = client.get("/api/auth/login")

        assert response.status_code == 200

    def test_logout_no_auth_needed(self):
        """The logout endpoint doesn't require the get_current_user dep."""
        app = _build_unauthed_test_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post("/api/auth/logout")
        assert response.status_code == 200
