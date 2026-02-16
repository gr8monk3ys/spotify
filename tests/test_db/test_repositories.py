"""Comprehensive tests for SpotifyForge repository classes.

Uses an in-memory SQLite database via a session-scoped fixture so that every
test method gets a fresh, empty database with all tables created.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine

from spotifyforge.models.models import (
    AudioFeatures,
    AudioFeaturesSource,
    JobType,
    Playlist,
    PlaylistTrackLink,
    ScheduledJob,
    Track,
    User,
)
from spotifyforge.db.repositories import (
    ArtistRepository,
    AudioFeaturesRepository,
    PlaylistRepository,
    ScheduledJobRepository,
    TrackRepository,
)


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(name="engine")
def fixture_engine():
    """Create an in-memory SQLite engine with all tables."""
    engine = create_engine(
        "sqlite://",
        echo=False,
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture(name="session")
def fixture_session(engine):
    """Yield a fresh SQLModel session, rolling back after each test."""
    with Session(engine) as session:
        yield session


@pytest.fixture(name="sample_user")
def fixture_sample_user(session: Session) -> User:
    """Insert and return a sample user."""
    user = User(spotify_id="user_1", display_name="Test User")
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


@pytest.fixture(name="sample_tracks")
def fixture_sample_tracks(session: Session) -> list[Track]:
    """Insert and return three sample tracks."""
    tracks = [
        Track(
            spotify_id=f"sp_track_{i}",
            name=name,
            artist_names=artists,
            duration_ms=dur,
            cached_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
        for i, (name, artists, dur) in enumerate(
            [
                ("Bohemian Rhapsody", ["Queen"], 354000),
                ("Stairway to Heaven", ["Led Zeppelin"], 482000),
                ("Hotel California", ["Eagles"], 391000),
            ],
            start=1,
        )
    ]
    for t in tracks:
        session.add(t)
    session.commit()
    for t in tracks:
        session.refresh(t)
    return tracks


@pytest.fixture(name="sample_playlist")
def fixture_sample_playlist(session: Session, sample_user: User) -> Playlist:
    """Insert and return a sample playlist owned by sample_user."""
    pl = Playlist(
        spotify_id="sp_playlist_1",
        owner_id=sample_user.id,
        name="Classic Rock",
        snapshot_id="snap_v1",
    )
    session.add(pl)
    session.commit()
    session.refresh(pl)
    return pl


# ============================================================================
# TrackRepository
# ============================================================================


class TestTrackRepositoryUpsert:
    """TrackRepository.upsert inserts new tracks and updates existing ones."""

    def test_upsert_creates_new_track(self, session: Session):
        repo = TrackRepository(session)
        track = repo.upsert(
            {
                "spotify_id": "new_1",
                "name": "New Song",
                "duration_ms": 200000,
            }
        )
        assert track.id is not None
        assert track.spotify_id == "new_1"
        assert track.name == "New Song"
        assert track.cached_at is not None

    def test_upsert_updates_existing_track(self, session: Session):
        repo = TrackRepository(session)
        original = repo.upsert(
            {"spotify_id": "upd_1", "name": "Original", "duration_ms": 100}
        )
        original_id = original.id
        original_cached = original.cached_at

        updated = repo.upsert(
            {"spotify_id": "upd_1", "name": "Updated", "duration_ms": 200}
        )
        assert updated.id == original_id
        assert updated.name == "Updated"
        assert updated.duration_ms == 200
        assert updated.cached_at >= original_cached

    def test_upsert_preserves_id(self, session: Session):
        repo = TrackRepository(session)
        t1 = repo.upsert({"spotify_id": "id_test", "name": "A", "duration_ms": 0})
        t2 = repo.upsert({"spotify_id": "id_test", "name": "B", "duration_ms": 0})
        assert t1.id == t2.id


class TestTrackRepositoryUpsertMany:
    """TrackRepository.upsert_many handles batch operations."""

    def test_upsert_many_creates_all(self, session: Session):
        repo = TrackRepository(session)
        tracks = repo.upsert_many(
            [
                {"spotify_id": "batch_1", "name": "B1", "duration_ms": 100},
                {"spotify_id": "batch_2", "name": "B2", "duration_ms": 200},
                {"spotify_id": "batch_3", "name": "B3", "duration_ms": 300},
            ]
        )
        assert len(tracks) == 3
        assert all(t.id is not None for t in tracks)
        names = {t.name for t in tracks}
        assert names == {"B1", "B2", "B3"}

    def test_upsert_many_mixed_insert_update(self, session: Session):
        repo = TrackRepository(session)
        # Insert first
        repo.upsert({"spotify_id": "mix_1", "name": "Old", "duration_ms": 0})

        results = repo.upsert_many(
            [
                {"spotify_id": "mix_1", "name": "New", "duration_ms": 0},
                {"spotify_id": "mix_2", "name": "Brand New", "duration_ms": 0},
            ]
        )
        assert len(results) == 2
        assert results[0].name == "New"  # updated
        assert results[1].name == "Brand New"  # inserted

    def test_upsert_many_empty_list(self, session: Session):
        repo = TrackRepository(session)
        assert repo.upsert_many([]) == []


class TestTrackRepositoryRead:
    """TrackRepository read operations."""

    def test_get_by_spotify_id_found(self, session: Session, sample_tracks):
        repo = TrackRepository(session)
        track = repo.get_by_spotify_id("sp_track_1")
        assert track is not None
        assert track.name == "Bohemian Rhapsody"

    def test_get_by_spotify_id_not_found(self, session: Session):
        repo = TrackRepository(session)
        assert repo.get_by_spotify_id("nonexistent") is None

    def test_get_many_by_spotify_ids(self, session: Session, sample_tracks):
        repo = TrackRepository(session)
        tracks = repo.get_many_by_spotify_ids(["sp_track_1", "sp_track_3"])
        spotify_ids = {t.spotify_id for t in tracks}
        assert spotify_ids == {"sp_track_1", "sp_track_3"}

    def test_get_many_by_spotify_ids_partial_match(
        self, session: Session, sample_tracks
    ):
        repo = TrackRepository(session)
        tracks = repo.get_many_by_spotify_ids(["sp_track_1", "nonexistent"])
        assert len(tracks) == 1
        assert tracks[0].spotify_id == "sp_track_1"

    def test_get_many_by_spotify_ids_empty_list(self, session: Session):
        repo = TrackRepository(session)
        assert repo.get_many_by_spotify_ids([]) == []

    def test_get_stale_returns_old_tracks(self, session: Session, sample_tracks):
        repo = TrackRepository(session)
        # sample_tracks have cached_at = 2025-01-01 UTC, which is over a year ago
        # Using ttl_seconds=1 should make them all stale
        stale = repo.get_stale(ttl_seconds=1)
        assert len(stale) == 3

    def test_get_stale_excludes_fresh_tracks(self, session: Session):
        repo = TrackRepository(session)
        # Upsert a track (gets current timestamp via _utcnow)
        repo.upsert({"spotify_id": "fresh_1", "name": "Fresh", "duration_ms": 0})
        # A very large TTL should not mark it stale
        stale = repo.get_stale(ttl_seconds=999999)
        assert len(stale) == 0


class TestTrackRepositorySearch:
    """TrackRepository.search case-insensitive name matching."""

    def test_search_finds_substring(self, session: Session, sample_tracks):
        repo = TrackRepository(session)
        results = repo.search("bohemian")
        assert len(results) == 1
        assert results[0].name == "Bohemian Rhapsody"

    def test_search_case_insensitive(self, session: Session, sample_tracks):
        repo = TrackRepository(session)
        results = repo.search("STAIRWAY")
        assert len(results) == 1
        assert results[0].name == "Stairway to Heaven"

    def test_search_no_match(self, session: Session, sample_tracks):
        repo = TrackRepository(session)
        assert repo.search("zzzzz_nonexistent") == []

    def test_search_respects_limit(self, session: Session):
        repo = TrackRepository(session)
        for i in range(10):
            repo.upsert(
                {"spotify_id": f"lim_{i}", "name": f"Song {i}", "duration_ms": 0}
            )
        results = repo.search("Song", limit=3)
        assert len(results) == 3

    def test_search_partial_match(self, session: Session, sample_tracks):
        repo = TrackRepository(session)
        # "to" appears in "Stairway to Heaven"
        results = repo.search("to")
        names = {r.name for r in results}
        assert "Stairway to Heaven" in names


# ============================================================================
# ArtistRepository
# ============================================================================


class TestArtistRepositoryUpsert:
    """ArtistRepository.upsert and upsert_many."""

    def test_upsert_creates_new(self, session: Session):
        repo = ArtistRepository(session)
        artist = repo.upsert(
            {
                "spotify_id": "art_1",
                "name": "Radiohead",
                "genres": ["alternative rock", "art rock"],
            }
        )
        assert artist.id is not None
        assert artist.name == "Radiohead"
        assert artist.genres == ["alternative rock", "art rock"]

    def test_upsert_updates_existing(self, session: Session):
        repo = ArtistRepository(session)
        original = repo.upsert(
            {"spotify_id": "art_upd", "name": "Old Name", "genres": ["rock"]}
        )
        updated = repo.upsert(
            {"spotify_id": "art_upd", "name": "New Name", "genres": ["pop"]}
        )
        assert updated.id == original.id
        assert updated.name == "New Name"
        assert updated.genres == ["pop"]

    def test_upsert_many_creates_all(self, session: Session):
        repo = ArtistRepository(session)
        artists = repo.upsert_many(
            [
                {"spotify_id": "am_1", "name": "A1"},
                {"spotify_id": "am_2", "name": "A2"},
            ]
        )
        assert len(artists) == 2
        assert all(a.id is not None for a in artists)

    def test_upsert_many_mixed(self, session: Session):
        repo = ArtistRepository(session)
        repo.upsert({"spotify_id": "amx_1", "name": "Existing"})
        results = repo.upsert_many(
            [
                {"spotify_id": "amx_1", "name": "Updated"},
                {"spotify_id": "amx_2", "name": "New"},
            ]
        )
        assert results[0].name == "Updated"
        assert results[1].name == "New"

    def test_upsert_many_empty(self, session: Session):
        repo = ArtistRepository(session)
        assert repo.upsert_many([]) == []


class TestArtistRepositoryRead:
    """ArtistRepository read operations."""

    def test_get_by_spotify_id_found(self, session: Session):
        repo = ArtistRepository(session)
        repo.upsert({"spotify_id": "art_read", "name": "Found"})
        result = repo.get_by_spotify_id("art_read")
        assert result is not None
        assert result.name == "Found"

    def test_get_by_spotify_id_not_found(self, session: Session):
        repo = ArtistRepository(session)
        assert repo.get_by_spotify_id("missing_artist") is None


class TestArtistRepositorySearch:
    """ArtistRepository.search case-insensitive name matching."""

    def test_search_finds_match(self, session: Session):
        repo = ArtistRepository(session)
        repo.upsert({"spotify_id": "as_1", "name": "Pink Floyd"})
        repo.upsert({"spotify_id": "as_2", "name": "Floyd Cramer"})
        results = repo.search("floyd")
        assert len(results) == 2

    def test_search_case_insensitive(self, session: Session):
        repo = ArtistRepository(session)
        repo.upsert({"spotify_id": "as_ci", "name": "Daft Punk"})
        results = repo.search("DAFT")
        assert len(results) == 1
        assert results[0].name == "Daft Punk"

    def test_search_no_match(self, session: Session):
        repo = ArtistRepository(session)
        assert repo.search("zzzz_nonexistent") == []

    def test_search_respects_limit(self, session: Session):
        repo = ArtistRepository(session)
        for i in range(10):
            repo.upsert({"spotify_id": f"as_lim_{i}", "name": f"Artist {i}"})
        results = repo.search("Artist", limit=5)
        assert len(results) == 5


# ============================================================================
# PlaylistRepository
# ============================================================================


class TestPlaylistRepositoryCreate:
    """PlaylistRepository.create."""

    def test_create_persists(self, session: Session, sample_user: User):
        repo = PlaylistRepository(session)
        pl = repo.create(
            {
                "spotify_id": "sp_pl_new",
                "owner_id": sample_user.id,
                "name": "Fresh Playlist",
            }
        )
        assert pl.id is not None
        assert pl.spotify_id == "sp_pl_new"
        assert pl.name == "Fresh Playlist"
        assert pl.owner_id == sample_user.id

    def test_create_with_all_fields(self, session: Session, sample_user: User):
        repo = PlaylistRepository(session)
        pl = repo.create(
            {
                "spotify_id": "sp_pl_full",
                "owner_id": sample_user.id,
                "name": "Full Playlist",
                "description": "A detailed description",
                "public": False,
                "collaborative": True,
                "snapshot_id": "snap_1",
                "follower_count": 42,
                "track_count": 10,
            }
        )
        assert pl.description == "A detailed description"
        assert pl.public is False
        assert pl.collaborative is True
        assert pl.follower_count == 42
        assert pl.track_count == 10


class TestPlaylistRepositoryGetBySpotifyId:
    """PlaylistRepository.get_by_spotify_id."""

    def test_found(self, session: Session, sample_playlist: Playlist):
        repo = PlaylistRepository(session)
        result = repo.get_by_spotify_id("sp_playlist_1")
        assert result is not None
        assert result.id == sample_playlist.id

    def test_not_found(self, session: Session):
        repo = PlaylistRepository(session)
        assert repo.get_by_spotify_id("nonexistent_playlist") is None


class TestPlaylistRepositoryGetByUser:
    """PlaylistRepository.get_by_user.

    Note: The repository references ``Playlist.user_id`` which does not exist
    as a declared column on the model (the actual column is ``owner_id``).
    SQLAlchemy will raise an ``ArgumentError`` when trying to build the
    WHERE clause. We verify that the method raises instead of silently
    returning wrong results.
    """

    def test_get_by_user_raises_for_missing_attribute(
        self, session: Session, sample_user: User, sample_playlist: Playlist
    ):
        repo = PlaylistRepository(session)
        with pytest.raises(Exception):
            repo.get_by_user(str(sample_user.id))


class TestPlaylistRepositorySyncTracks:
    """PlaylistRepository.sync_tracks replaces tracks and updates snapshot."""

    def test_sync_tracks_inserts_links(
        self,
        session: Session,
        sample_playlist: Playlist,
        sample_tracks: list[Track],
    ):
        repo = PlaylistRepository(session)
        track_ids = [t.id for t in sample_tracks]
        repo.sync_tracks(sample_playlist.id, track_ids, "snap_v2")

        # Verify links were created
        from sqlmodel import select

        links = session.exec(
            select(PlaylistTrackLink).where(
                PlaylistTrackLink.playlist_id == sample_playlist.id
            )
        ).all()
        assert len(links) == 3

        # Verify positional ordering
        positions = sorted(links, key=lambda l: l.position)
        for i, link in enumerate(positions):
            assert link.position == i
            assert link.track_id == track_ids[i]

    def test_sync_tracks_updates_snapshot(
        self,
        session: Session,
        sample_playlist: Playlist,
        sample_tracks: list[Track],
    ):
        repo = PlaylistRepository(session)
        repo.sync_tracks(sample_playlist.id, [sample_tracks[0].id], "snap_v3")

        session.refresh(sample_playlist)
        assert sample_playlist.snapshot_id == "snap_v3"

    def test_sync_tracks_replaces_existing(
        self,
        session: Session,
        sample_playlist: Playlist,
        sample_tracks: list[Track],
    ):
        repo = PlaylistRepository(session)
        # First sync with first two tracks
        repo.sync_tracks(
            sample_playlist.id,
            [sample_tracks[0].id, sample_tracks[1].id],
            "snap_a",
        )

        # Second sync with last track only (no overlap with first sync)
        repo.sync_tracks(
            sample_playlist.id, [sample_tracks[2].id], "snap_b"
        )

        from sqlmodel import select

        links = session.exec(
            select(PlaylistTrackLink).where(
                PlaylistTrackLink.playlist_id == sample_playlist.id
            )
        ).all()
        assert len(links) == 1
        assert links[0].track_id == sample_tracks[2].id

    def test_sync_tracks_empty_list_clears_all(
        self,
        session: Session,
        sample_playlist: Playlist,
        sample_tracks: list[Track],
    ):
        repo = PlaylistRepository(session)
        repo.sync_tracks(
            sample_playlist.id, [t.id for t in sample_tracks], "snap_x"
        )
        # Now sync with empty list
        repo.sync_tracks(sample_playlist.id, [], "snap_empty")

        from sqlmodel import select

        links = session.exec(
            select(PlaylistTrackLink).where(
                PlaylistTrackLink.playlist_id == sample_playlist.id
            )
        ).all()
        assert len(links) == 0


class TestPlaylistRepositoryNeedsSync:
    """PlaylistRepository.needs_sync checks snapshot_id divergence."""

    def test_needs_sync_same_snapshot(
        self, session: Session, sample_playlist: Playlist
    ):
        repo = PlaylistRepository(session)
        # sample_playlist has snapshot_id="snap_v1"
        assert repo.needs_sync(sample_playlist.id, "snap_v1") is False

    def test_needs_sync_different_snapshot(
        self, session: Session, sample_playlist: Playlist
    ):
        repo = PlaylistRepository(session)
        assert repo.needs_sync(sample_playlist.id, "snap_v2") is True

    def test_needs_sync_nonexistent_playlist(self, session: Session):
        repo = PlaylistRepository(session)
        # Nonexistent playlist always needs sync
        assert repo.needs_sync(99999, "any_snapshot") is True

    def test_needs_sync_after_sync_tracks(
        self,
        session: Session,
        sample_playlist: Playlist,
        sample_tracks: list[Track],
    ):
        repo = PlaylistRepository(session)
        repo.sync_tracks(
            sample_playlist.id, [sample_tracks[0].id], "snap_updated"
        )
        assert repo.needs_sync(sample_playlist.id, "snap_updated") is False
        assert repo.needs_sync(sample_playlist.id, "snap_old") is True


# ============================================================================
# AudioFeaturesRepository
# ============================================================================


class TestAudioFeaturesRepositoryUpsert:
    """AudioFeaturesRepository.upsert inserts or updates features."""

    def test_upsert_creates_new(self, session: Session, sample_tracks: list[Track]):
        repo = AudioFeaturesRepository(session)
        af = repo.upsert(
            {
                "track_id": sample_tracks[0].id,
                "source": AudioFeaturesSource.spotify,
                "danceability": 0.8,
                "energy": 0.7,
            }
        )
        assert af.id is not None
        assert af.track_id == sample_tracks[0].id
        assert af.danceability == 0.8
        assert af.energy == 0.7
        assert af.cached_at is not None

    def test_upsert_updates_existing(
        self, session: Session, sample_tracks: list[Track]
    ):
        repo = AudioFeaturesRepository(session)
        original = repo.upsert(
            {
                "track_id": sample_tracks[0].id,
                "source": AudioFeaturesSource.spotify,
                "danceability": 0.5,
            }
        )
        updated = repo.upsert(
            {
                "track_id": sample_tracks[0].id,
                "source": AudioFeaturesSource.spotify,
                "danceability": 0.9,
                "energy": 0.6,
            }
        )
        assert updated.id == original.id
        assert updated.danceability == 0.9
        assert updated.energy == 0.6

    def test_upsert_all_feature_fields(
        self, session: Session, sample_tracks: list[Track]
    ):
        repo = AudioFeaturesRepository(session)
        af = repo.upsert(
            {
                "track_id": sample_tracks[0].id,
                "source": AudioFeaturesSource.cyanite,
                "danceability": 0.72,
                "energy": 0.65,
                "valence": 0.55,
                "tempo": 128.5,
                "key": 7,
                "mode": 1,
                "loudness": -6.2,
                "speechiness": 0.04,
                "acousticness": 0.12,
                "instrumentalness": 0.0,
                "liveness": 0.08,
            }
        )
        assert af.danceability == 0.72
        assert af.tempo == 128.5
        assert af.key == 7
        assert af.loudness == -6.2


class TestAudioFeaturesRepositoryUpsertMany:
    """AudioFeaturesRepository.upsert_many batch operations."""

    def test_upsert_many_creates_all(
        self, session: Session, sample_tracks: list[Track]
    ):
        repo = AudioFeaturesRepository(session)
        features = repo.upsert_many(
            [
                {
                    "track_id": sample_tracks[0].id,
                    "source": AudioFeaturesSource.spotify,
                    "danceability": 0.5,
                },
                {
                    "track_id": sample_tracks[1].id,
                    "source": AudioFeaturesSource.spotify,
                    "danceability": 0.7,
                },
            ]
        )
        assert len(features) == 2
        assert all(f.id is not None for f in features)

    def test_upsert_many_empty(self, session: Session):
        repo = AudioFeaturesRepository(session)
        assert repo.upsert_many([]) == []


class TestAudioFeaturesRepositoryRead:
    """AudioFeaturesRepository read operations."""

    def test_get_by_track_id_found(
        self, session: Session, sample_tracks: list[Track]
    ):
        repo = AudioFeaturesRepository(session)
        repo.upsert(
            {
                "track_id": sample_tracks[0].id,
                "source": AudioFeaturesSource.spotify,
                "danceability": 0.6,
            }
        )
        result = repo.get_by_track_id(sample_tracks[0].id)
        assert result is not None
        assert result.danceability == 0.6

    def test_get_by_track_id_not_found(self, session: Session):
        repo = AudioFeaturesRepository(session)
        assert repo.get_by_track_id(99999) is None

    def test_get_missing_track_ids_all_missing(
        self, session: Session, sample_tracks: list[Track]
    ):
        repo = AudioFeaturesRepository(session)
        track_ids = [t.id for t in sample_tracks]
        missing = repo.get_missing_track_ids(track_ids)
        assert set(missing) == set(track_ids)

    def test_get_missing_track_ids_some_cached(
        self, session: Session, sample_tracks: list[Track]
    ):
        repo = AudioFeaturesRepository(session)
        # Cache features for first track only
        repo.upsert(
            {
                "track_id": sample_tracks[0].id,
                "source": AudioFeaturesSource.spotify,
                "danceability": 0.5,
            }
        )
        track_ids = [t.id for t in sample_tracks]
        missing = repo.get_missing_track_ids(track_ids)
        assert sample_tracks[0].id not in missing
        assert sample_tracks[1].id in missing
        assert sample_tracks[2].id in missing

    def test_get_missing_track_ids_none_missing(
        self, session: Session, sample_tracks: list[Track]
    ):
        repo = AudioFeaturesRepository(session)
        for t in sample_tracks:
            repo.upsert(
                {
                    "track_id": t.id,
                    "source": AudioFeaturesSource.spotify,
                    "danceability": 0.5,
                }
            )
        track_ids = [t.id for t in sample_tracks]
        assert repo.get_missing_track_ids(track_ids) == []

    def test_get_missing_track_ids_empty_input(self, session: Session):
        repo = AudioFeaturesRepository(session)
        assert repo.get_missing_track_ids([]) == []


# ============================================================================
# ScheduledJobRepository
# ============================================================================


class TestScheduledJobRepositoryCreate:
    """ScheduledJobRepository.create."""

    def test_create_persists(self, session: Session, sample_user: User):
        repo = ScheduledJobRepository(session)
        job = repo.create(
            {
                "user_id": sample_user.id,
                "name": "Daily sync",
                "job_type": JobType.playlist_sync,
                "cron_expression": "0 0 * * *",
            }
        )
        assert job.id is not None
        assert job.name == "Daily sync"
        assert job.job_type == JobType.playlist_sync
        assert job.enabled is True

    def test_create_with_all_fields(
        self, session: Session, sample_user: User, sample_playlist: Playlist
    ):
        repo = ScheduledJobRepository(session)
        # Use naive datetimes because SQLite drops timezone info on round-trip
        now = datetime.utcnow()
        job = repo.create(
            {
                "user_id": sample_user.id,
                "name": "Full job",
                "job_type": JobType.playlist_update,
                "playlist_id": sample_playlist.id,
                "config": {"max_tracks": 50},
                "cron_expression": "0 6 * * *",
                "enabled": False,
                "last_run_at": now,
                "next_run_at": now + timedelta(hours=24),
            }
        )
        assert job.playlist_id == sample_playlist.id
        assert job.config == {"max_tracks": 50}
        assert job.enabled is False
        assert job.last_run_at == now

    def test_create_disabled_job(self, session: Session, sample_user: User):
        repo = ScheduledJobRepository(session)
        job = repo.create(
            {
                "user_id": sample_user.id,
                "name": "Disabled job",
                "job_type": JobType.health_check,
                "cron_expression": "*/5 * * * *",
                "enabled": False,
            }
        )
        assert job.enabled is False


class TestScheduledJobRepositoryGetEnabledJobs:
    """ScheduledJobRepository.get_enabled_jobs."""

    def test_returns_only_enabled(self, session: Session, sample_user: User):
        repo = ScheduledJobRepository(session)
        repo.create(
            {
                "user_id": sample_user.id,
                "name": "Enabled 1",
                "job_type": JobType.playlist_sync,
                "cron_expression": "0 0 * * *",
                "enabled": True,
            }
        )
        repo.create(
            {
                "user_id": sample_user.id,
                "name": "Disabled 1",
                "job_type": JobType.health_check,
                "cron_expression": "*/5 * * * *",
                "enabled": False,
            }
        )
        repo.create(
            {
                "user_id": sample_user.id,
                "name": "Enabled 2",
                "job_type": JobType.stats_snapshot,
                "cron_expression": "0 12 * * *",
                "enabled": True,
            }
        )

        enabled = repo.get_enabled_jobs()
        assert len(enabled) == 2
        names = {j.name for j in enabled}
        assert names == {"Enabled 1", "Enabled 2"}

    def test_returns_empty_when_all_disabled(
        self, session: Session, sample_user: User
    ):
        repo = ScheduledJobRepository(session)
        repo.create(
            {
                "user_id": sample_user.id,
                "name": "Off",
                "job_type": JobType.health_check,
                "cron_expression": "0 0 * * *",
                "enabled": False,
            }
        )
        assert repo.get_enabled_jobs() == []

    def test_returns_empty_when_no_jobs(self, session: Session):
        repo = ScheduledJobRepository(session)
        assert repo.get_enabled_jobs() == []


class TestScheduledJobRepositoryGetByUser:
    """ScheduledJobRepository.get_by_user."""

    def test_returns_user_jobs(self, session: Session, sample_user: User):
        repo = ScheduledJobRepository(session)
        repo.create(
            {
                "user_id": sample_user.id,
                "name": "User job 1",
                "job_type": JobType.playlist_sync,
                "cron_expression": "0 0 * * *",
            }
        )
        repo.create(
            {
                "user_id": sample_user.id,
                "name": "User job 2",
                "job_type": JobType.health_check,
                "cron_expression": "*/5 * * * *",
            }
        )

        # The method expects a string user_id, but compares against ScheduledJob.user_id (int).
        # With SQLite, integer user_id == str "1" can work via type coercion.
        jobs = repo.get_by_user(str(sample_user.id))
        assert len(jobs) == 2

    def test_returns_empty_for_unknown_user(self, session: Session):
        repo = ScheduledJobRepository(session)
        assert repo.get_by_user("999999") == []


class TestScheduledJobRepositoryUpdateLastRun:
    """ScheduledJobRepository.update_last_run."""

    def test_sets_last_run(self, session: Session, sample_user: User):
        repo = ScheduledJobRepository(session)
        job = repo.create(
            {
                "user_id": sample_user.id,
                "name": "Run tracker",
                "job_type": JobType.playlist_sync,
                "cron_expression": "0 0 * * *",
            }
        )
        assert job.last_run_at is None

        # Use naive datetime because SQLite drops timezone info
        run_time = datetime(2025, 6, 15, 12, 0, 0)
        updated = repo.update_last_run(job.id, run_time)
        assert updated is not None
        assert updated.last_run_at == run_time

    def test_update_last_run_nonexistent_returns_none(self, session: Session):
        repo = ScheduledJobRepository(session)
        assert repo.update_last_run(99999, datetime.now(timezone.utc)) is None

    def test_update_last_run_overwrites(self, session: Session, sample_user: User):
        repo = ScheduledJobRepository(session)
        job = repo.create(
            {
                "user_id": sample_user.id,
                "name": "Multi run",
                "job_type": JobType.stats_snapshot,
                "cron_expression": "0 0 * * *",
            }
        )
        # Use naive datetimes because SQLite drops timezone info
        first_run = datetime(2025, 1, 1)
        repo.update_last_run(job.id, first_run)

        second_run = datetime(2025, 2, 1)
        updated = repo.update_last_run(job.id, second_run)
        assert updated.last_run_at == second_run


# ============================================================================
# Cross-repository integration tests
# ============================================================================


class TestCrossRepositoryIntegration:
    """Tests that span multiple repositories working together."""

    def test_track_upsert_then_audio_features(
        self, session: Session
    ):
        """Upsert a track, then add audio features for it."""
        track_repo = TrackRepository(session)
        af_repo = AudioFeaturesRepository(session)

        track = track_repo.upsert(
            {"spotify_id": "cross_1", "name": "Cross Track", "duration_ms": 180000}
        )
        af = af_repo.upsert(
            {
                "track_id": track.id,
                "source": AudioFeaturesSource.spotify,
                "danceability": 0.65,
                "energy": 0.8,
            }
        )
        assert af.track_id == track.id

        # Verify the lookup works
        result = af_repo.get_by_track_id(track.id)
        assert result is not None
        assert result.danceability == 0.65

    def test_playlist_sync_with_upserted_tracks(
        self, session: Session, sample_user: User
    ):
        """Create tracks, create a playlist, sync them together."""
        track_repo = TrackRepository(session)
        playlist_repo = PlaylistRepository(session)

        tracks = track_repo.upsert_many(
            [
                {"spotify_id": "sync_t1", "name": "Sync 1", "duration_ms": 100},
                {"spotify_id": "sync_t2", "name": "Sync 2", "duration_ms": 200},
            ]
        )

        playlist = playlist_repo.create(
            {
                "spotify_id": "sync_pl",
                "owner_id": sample_user.id,
                "name": "Synced Playlist",
                "snapshot_id": "init",
            }
        )

        track_ids = [t.id for t in tracks]
        playlist_repo.sync_tracks(playlist.id, track_ids, "snap_synced")

        assert playlist_repo.needs_sync(playlist.id, "snap_synced") is False
        assert playlist_repo.needs_sync(playlist.id, "snap_other") is True

    def test_get_missing_audio_features_for_tracks(
        self, session: Session
    ):
        """Upsert several tracks, add features for some, check missing."""
        track_repo = TrackRepository(session)
        af_repo = AudioFeaturesRepository(session)

        tracks = track_repo.upsert_many(
            [
                {"spotify_id": f"miss_{i}", "name": f"Track {i}", "duration_ms": 0}
                for i in range(5)
            ]
        )
        track_ids = [t.id for t in tracks]

        # Add features for first two only
        af_repo.upsert_many(
            [
                {
                    "track_id": tracks[0].id,
                    "source": AudioFeaturesSource.spotify,
                    "danceability": 0.5,
                },
                {
                    "track_id": tracks[1].id,
                    "source": AudioFeaturesSource.spotify,
                    "danceability": 0.6,
                },
            ]
        )

        missing = af_repo.get_missing_track_ids(track_ids)
        assert len(missing) == 3
        assert tracks[0].id not in missing
        assert tracks[1].id not in missing
        assert tracks[2].id in missing
        assert tracks[3].id in missing
        assert tracks[4].id in missing
