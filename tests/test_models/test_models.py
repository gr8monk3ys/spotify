"""Comprehensive tests for SpotifyForge SQLModel table models and Pydantic schemas.

Covers enum members, model instantiation, field defaults, schema validation,
partial-update semantics, from_attributes round-tripping, JSON field handling,
timezone helpers, and database-level constraints.
"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlmodel import Session, select

from spotifyforge.models.models import (
    # Table models
    Album,
    Artist,
    AudioFeatures,
    # Pydantic schemas
    AudioFeaturesResponse,
    # Enums
    AudioFeaturesSource,
    JobType,
    Playlist,
    PlaylistCreate,
    PlaylistResponse,
    PlaylistTrack,
    PlaylistTrackLink,
    PlaylistUpdate,
    ScheduledJob,
    ScheduledJobCreate,
    ScheduledJobResponse,
    Track,
    TrackResponse,
    User,
    as_utc,
    utc_now,
)

# ============================================================================
# Enum tests
# ============================================================================


class TestAudioFeaturesSourceEnum:
    """Only sources something actually writes belong in the enum."""

    def test_members(self) -> None:
        assert set(AudioFeaturesSource) == {AudioFeaturesSource.spotify}
        assert AudioFeaturesSource.spotify.value == "spotify"
        assert str(AudioFeaturesSource.spotify) == "spotify"

    def test_is_str_enum(self) -> None:
        for member in AudioFeaturesSource:
            assert isinstance(member, str)


class TestJobTypeEnum:
    """JobType is the single job vocabulary: five members, each with a handler."""

    EXPECTED = {
        "sync",
        "archive",
        "deduplicate",
        "genre_refresh",
        "time_capsule",
    }

    def test_member_count(self) -> None:
        assert len(JobType) == 5

    def test_member_values(self) -> None:
        assert {m.value for m in JobType} == self.EXPECTED

    @pytest.mark.parametrize(
        "name",
        [
            "sync",
            "archive",
            "deduplicate",
            "genre_refresh",
            "time_capsule",
        ],
    )
    def test_lookup_by_value(self, name) -> None:
        assert JobType(name) is not None

    @pytest.mark.parametrize(
        "old_name",
        [
            "playlist_update",
            "playlist_sync",
            "playlist_archive",
            "discovery_refresh",
            "stats_snapshot",
            "health_check",
        ],
    )
    def test_old_members_are_gone(self, old_name) -> None:
        """The pre-rewrite job vocabulary must not be storable."""
        with pytest.raises(ValueError):
            JobType(old_name)

    def test_every_member_has_a_scheduler_handler(self) -> None:
        """Each JobType member dispatches to a real handler in core.scheduler."""
        from spotifyforge.core.scheduler import SchedulerService

        handler_names = {
            JobType.sync: "_handle_sync",
            JobType.archive: "_handle_archive",
            JobType.deduplicate: "_handle_deduplicate",
            JobType.genre_refresh: "_handle_genre_refresh",
            JobType.time_capsule: "_handle_time_capsule",
        }
        assert set(handler_names) == set(JobType)
        for name in handler_names.values():
            assert callable(getattr(SchedulerService, name))


class TestTimestampHelpers:
    """utc_now()/as_utc() back every model timestamp."""

    def test_utc_now_is_timezone_aware(self) -> None:
        now = utc_now()
        assert now.tzinfo is UTC

    def test_as_utc_none_passthrough(self) -> None:
        assert as_utc(None) is None

    def test_as_utc_attaches_utc_to_naive(self) -> None:
        naive = datetime(2025, 6, 1, 12, 0, 0)
        result = as_utc(naive)
        assert result == datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)

    def test_as_utc_preserves_existing_tzinfo(self) -> None:
        aware = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)
        assert as_utc(aware) is aware

    def test_model_default_timestamps_are_aware(self) -> None:
        user = User(spotify_id="tz_user")
        assert user.created_at.tzinfo is UTC
        assert user.updated_at.tzinfo is UTC
        track = Track(spotify_id="tz_track", name="T")
        assert track.cached_at.tzinfo is UTC


class TestUserModel:
    """User model instantiation and defaults."""

    def test_create_minimal(self) -> None:
        user = User(spotify_id="user123")
        assert user.spotify_id == "user123"
        assert user.id is None
        assert user.display_name is None
        assert user.email is None

    def test_defaults(self) -> None:
        user = User(spotify_id="u1")
        assert user.is_premium is False
        assert isinstance(user.created_at, datetime)
        assert isinstance(user.updated_at, datetime)

    def test_all_fields(self) -> None:
        now = datetime(2025, 1, 1)
        user = User(
            spotify_id="u2",
            display_name="DJ Shadow",
            email="shadow@example.com",
            access_token_enc="enc_tok",
            refresh_token_enc="enc_ref",
            token_expiry=now,
            is_premium=True,
            created_at=now,
            updated_at=now,
        )
        assert user.display_name == "DJ Shadow"
        assert user.email == "shadow@example.com"
        assert user.is_premium is True
        assert user.token_expiry == now


class TestTrackModel:
    """Track model instantiation and defaults."""

    def test_create_minimal(self) -> None:
        track = Track(spotify_id="track1", name="Song A")
        assert track.spotify_id == "track1"
        assert track.name == "Song A"
        assert track.id is None

    def test_defaults(self) -> None:
        track = Track(spotify_id="t1", name="X")
        assert track.duration_ms == 0
        assert track.popularity is None
        assert track.artist_names is None
        assert track.album_name is None
        assert track.album_id is None
        assert track.isrc is None
        assert isinstance(track.cached_at, datetime)

    def test_json_field_artist_names(self) -> None:
        track = Track(
            spotify_id="t2",
            name="Y",
            artist_names=["Artist A", "Artist B"],
        )
        assert track.artist_names == ["Artist A", "Artist B"]

    def test_all_fields(self) -> None:
        track = Track(
            spotify_id="t3",
            name="Z",
            artist_names=["Solo"],
            album_name="The Album",
            album_id="alb1",
            duration_ms=240000,
            popularity=85,
            isrc="USRC12345678",
        )
        assert track.duration_ms == 240000
        assert track.popularity == 85
        assert track.isrc == "USRC12345678"


class TestArtistModel:
    """Artist model instantiation and defaults."""

    def test_create_minimal(self) -> None:
        artist = Artist(spotify_id="art1", name="Radiohead")
        assert artist.spotify_id == "art1"
        assert artist.name == "Radiohead"
        assert artist.id is None

    def test_defaults(self) -> None:
        artist = Artist(spotify_id="a1", name="A")
        assert artist.genres is None
        assert artist.popularity is None
        assert isinstance(artist.cached_at, datetime)

    def test_json_field_genres(self) -> None:
        artist = Artist(
            spotify_id="a2",
            name="B",
            genres=["rock", "alternative"],
        )
        assert artist.genres == ["rock", "alternative"]


class TestAlbumModel:
    """Album model instantiation and defaults."""

    def test_create_minimal(self) -> None:
        album = Album(spotify_id="alb1", name="OK Computer")
        assert album.spotify_id == "alb1"
        assert album.name == "OK Computer"

    def test_defaults(self) -> None:
        album = Album(spotify_id="al1", name="Al")
        assert album.artist_ids is None
        assert album.release_date is None
        assert album.total_tracks is None
        assert isinstance(album.cached_at, datetime)

    def test_json_field_artist_ids(self) -> None:
        album = Album(
            spotify_id="al2",
            name="Al2",
            artist_ids=["art1", "art2"],
        )
        assert album.artist_ids == ["art1", "art2"]


class TestAudioFeaturesModel:
    """AudioFeatures model instantiation and defaults."""

    def test_create_minimal(self) -> None:
        af = AudioFeatures(
            track_id=1,
            source=AudioFeaturesSource.spotify,
        )
        assert af.track_id == 1
        assert af.source == AudioFeaturesSource.spotify

    def test_defaults(self) -> None:
        af = AudioFeatures(track_id=1, source=AudioFeaturesSource.spotify)
        assert af.danceability is None
        assert af.energy is None
        assert af.valence is None
        assert af.tempo is None
        assert af.key is None
        assert af.mode is None
        assert af.loudness is None
        assert af.speechiness is None
        assert af.acousticness is None
        assert af.instrumentalness is None
        assert af.liveness is None
        assert isinstance(af.cached_at, datetime)

    def test_all_fields(self) -> None:
        af = AudioFeatures(
            track_id=1,
            source=AudioFeaturesSource.spotify,
            danceability=0.8,
            energy=0.9,
            valence=0.75,
            tempo=120.0,
            key=5,
            mode=1,
            loudness=-5.0,
            speechiness=0.05,
            acousticness=0.1,
            instrumentalness=0.0,
            liveness=0.15,
        )
        assert af.danceability == 0.8
        assert af.energy == 0.9
        assert af.tempo == 120.0
        assert af.key == 5
        assert af.loudness == -5.0


class TestPlaylistModel:
    """Playlist model instantiation and defaults."""

    def test_create_minimal(self) -> None:
        pl = Playlist(spotify_id="pl1", owner_id=1, name="My Mix")
        assert pl.spotify_id == "pl1"
        assert pl.owner_id == 1
        assert pl.name == "My Mix"

    def test_defaults(self) -> None:
        pl = Playlist(spotify_id="pl1", owner_id=1, name="X")
        assert pl.public is True
        assert pl.collaborative is False
        assert pl.snapshot_id is None
        assert pl.track_count == 0
        assert pl.last_synced_at is None
        assert isinstance(pl.created_at, datetime)
        assert isinstance(pl.updated_at, datetime)


class TestPlaylistTrackModel:
    """PlaylistTrack / PlaylistTrackLink model instantiation and defaults."""

    def test_create_minimal(self) -> None:
        pt = PlaylistTrack(playlist_id=1, track_id=1)
        assert pt.playlist_id == 1
        assert pt.track_id == 1

    def test_defaults(self) -> None:
        pt = PlaylistTrack(playlist_id=1, track_id=1)
        assert pt.position == 0
        assert pt.added_at is None
        assert pt.added_by is None

    def test_alias_is_same_class(self) -> None:
        assert PlaylistTrackLink is PlaylistTrack

    def test_with_position(self) -> None:
        pt = PlaylistTrack(playlist_id=1, track_id=5, position=3)
        assert pt.position == 3


class TestPlaylistTrackDuplicatesAllowed:
    """Spotify playlists can contain the same track at multiple positions.

    The old UniqueConstraint(playlist_id, track_id) is gone: sync must be
    able to mirror duplicates, so inserting the same (playlist, track) pair
    twice at different positions has to commit cleanly.
    """

    def test_same_track_twice_commits(self, db_session: Session) -> None:
        user = User(spotify_id="dup_user")
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)

        track = Track(spotify_id="dup_track", name="Repeated Song")
        playlist = Playlist(spotify_id="dup_pl", owner_id=user.id, name="Dupes")
        db_session.add(track)
        db_session.add(playlist)
        db_session.commit()
        db_session.refresh(track)
        db_session.refresh(playlist)

        db_session.add(PlaylistTrack(playlist_id=playlist.id, track_id=track.id, position=0))
        db_session.add(PlaylistTrack(playlist_id=playlist.id, track_id=track.id, position=2))
        db_session.commit()  # must not raise IntegrityError

        rows = db_session.exec(
            select(PlaylistTrack).where(PlaylistTrack.playlist_id == playlist.id)
        ).all()
        assert len(rows) == 2
        assert {r.position for r in rows} == {0, 2}
        assert all(r.track_id == track.id for r in rows)


class TestScheduledJobModel:
    """ScheduledJob model instantiation and defaults."""

    def test_create_minimal(self) -> None:
        job = ScheduledJob(
            user_id=1,
            name="Nightly sync",
            job_type=JobType.sync,
            cron_expression="0 0 * * *",
        )
        assert job.user_id == 1
        assert job.name == "Nightly sync"
        assert job.job_type == JobType.sync

    def test_defaults(self) -> None:
        job = ScheduledJob(
            user_id=1,
            name="J",
            job_type=JobType.deduplicate,
            cron_expression="*/5 * * * *",
        )
        assert job.enabled is True
        assert job.playlist_id is None
        assert job.config is None
        assert job.last_run_at is None
        assert job.next_run_at is None
        assert isinstance(job.created_at, datetime)
        assert isinstance(job.updated_at, datetime)

    def test_json_field_config(self) -> None:
        job = ScheduledJob(
            user_id=1,
            name="Configured",
            job_type=JobType.genre_refresh,
            cron_expression="0 12 * * *",
            config={"max_tracks": 50, "genre": "electronic"},
        )
        assert job.config == {"max_tracks": 50, "genre": "electronic"}


class TestPlaylistCreateSchema:
    """PlaylistCreate request schema."""

    def test_valid_minimal(self) -> None:
        pc = PlaylistCreate(name="My Playlist")
        assert pc.name == "My Playlist"
        assert pc.description is None
        assert pc.public is True
        assert pc.collaborative is False

    def test_valid_full(self) -> None:
        pc = PlaylistCreate(
            name="Chill Vibes",
            description="Relaxing tracks for study sessions",
            public=False,
            collaborative=True,
        )
        assert pc.name == "Chill Vibes"
        assert pc.description == "Relaxing tracks for study sessions"
        assert pc.public is False
        assert pc.collaborative is True

    def test_rejects_empty_name(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            PlaylistCreate(name="")
        errors = exc_info.value.errors()
        assert any(e["type"] == "string_too_short" for e in errors)

    def test_rejects_missing_name(self) -> None:
        with pytest.raises(ValidationError):
            PlaylistCreate()  # type: ignore[call-arg]

    def test_rejects_name_too_long(self) -> None:
        with pytest.raises(ValidationError):
            PlaylistCreate(name="x" * 513)

    def test_rejects_description_too_long(self) -> None:
        with pytest.raises(ValidationError):
            PlaylistCreate(name="ok", description="x" * 2001)

    def test_strict_mode_rejects_int_for_name(self) -> None:
        """strict=True should reject non-string for name."""
        with pytest.raises(ValidationError):
            PlaylistCreate(name=123)  # type: ignore[arg-type]


class TestPlaylistUpdateSchema:
    """PlaylistUpdate allows partial updates -- all fields optional."""

    def test_empty_update(self) -> None:
        pu = PlaylistUpdate()
        assert pu.name is None
        assert pu.description is None
        assert pu.public is None
        assert pu.collaborative is None

    def test_partial_name_only(self) -> None:
        pu = PlaylistUpdate(name="New Name")
        assert pu.name == "New Name"
        assert pu.public is None

    def test_partial_public_only(self) -> None:
        pu = PlaylistUpdate(public=False)
        assert pu.public is False
        assert pu.name is None

    def test_all_fields(self) -> None:
        pu = PlaylistUpdate(
            name="Updated",
            description="New desc",
            public=True,
            collaborative=True,
        )
        assert pu.name == "Updated"
        assert pu.description == "New desc"
        assert pu.public is True
        assert pu.collaborative is True

    def test_rejects_empty_name_when_provided(self) -> None:
        with pytest.raises(ValidationError):
            PlaylistUpdate(name="")

    def test_model_dump_excludes_unset(self) -> None:
        pu = PlaylistUpdate(name="Only Name")
        dumped = pu.model_dump(exclude_unset=True)
        assert "name" in dumped
        assert "public" not in dumped
        assert "description" not in dumped


class TestScheduledJobCreateSchema:
    """ScheduledJobCreate request schema."""

    def test_valid(self) -> None:
        sj = ScheduledJobCreate(
            name="Daily sync",
            job_type=JobType.sync,
            cron_expression="0 0 * * *",
        )
        assert sj.name == "Daily sync"
        assert sj.job_type == JobType.sync
        assert sj.cron_expression == "0 0 * * *"
        assert sj.enabled is True
        assert sj.playlist_id is None
        assert sj.config is None

    def test_rejects_empty_name(self) -> None:
        with pytest.raises(ValidationError):
            ScheduledJobCreate(
                name="",
                job_type=JobType.deduplicate,
                cron_expression="* * * * *",
            )

    def test_rejects_empty_cron(self) -> None:
        with pytest.raises(ValidationError):
            ScheduledJobCreate(
                name="Job",
                job_type=JobType.deduplicate,
                cron_expression="",
            )

    def test_with_config(self) -> None:
        sj = ScheduledJobCreate(
            name="Discovery",
            job_type=JobType.genre_refresh,
            cron_expression="0 6 * * *",
            config={"seeds": ["pop", "rock"]},
        )
        assert sj.config == {"seeds": ["pop", "rock"]}

    def test_accepts_string_job_type(self) -> None:
        """JSON carries the enum as its string value; strict=False on the field."""
        sj = ScheduledJobCreate(
            name="String type",
            job_type="time_capsule",
            cron_expression="0 0 1 * *",
        )
        assert sj.job_type is JobType.time_capsule

    def test_rejects_unknown_job_type(self) -> None:
        with pytest.raises(ValidationError):
            ScheduledJobCreate(
                name="Bad type",
                job_type="stats_snapshot",
                cron_expression="0 0 * * *",
            )


class TestPlaylistResponseSchema:
    """PlaylistResponse should be constructable from a Playlist ORM object."""

    def test_from_orm_object(self) -> None:
        now = datetime(2025, 6, 1, 12, 0, 0)
        pl = Playlist(
            id=1,
            spotify_id="sp_pl_1",
            owner_id=42,
            name="Test Playlist",
            description="A test",
            public=True,
            collaborative=False,
            snapshot_id="snap1",
            track_count=25,
            last_synced_at=now,
            created_at=now,
            updated_at=now,
        )
        resp = PlaylistResponse.model_validate(pl)
        assert resp.id == 1
        assert resp.spotify_id == "sp_pl_1"
        assert resp.owner_id == 42
        assert resp.name == "Test Playlist"
        assert resp.description == "A test"
        assert resp.public is True
        assert resp.collaborative is False
        assert resp.snapshot_id == "snap1"
        assert resp.track_count == 25
        assert resp.last_synced_at == now
        assert resp.created_at == now

    def test_nullable_fields(self) -> None:
        now = datetime.now(UTC)
        pl = Playlist(
            id=2,
            spotify_id="sp_pl_2",
            owner_id=1,
            name="Minimal",
            public=False,
            collaborative=False,
            track_count=0,
            created_at=now,
            updated_at=now,
        )
        resp = PlaylistResponse.model_validate(pl)
        assert resp.description is None
        assert resp.snapshot_id is None
        assert resp.last_synced_at is None


class TestTrackResponseSchema:
    """TrackResponse should be constructable from a Track ORM object."""

    def test_from_orm_object(self) -> None:
        now = datetime(2025, 3, 15, 8, 30, 0)
        track = Track(
            id=10,
            spotify_id="sp_t_10",
            name="Bohemian Rhapsody",
            artist_names=["Queen"],
            album_name="A Night at the Opera",
            album_id="alb_opera",
            duration_ms=354000,
            popularity=95,
            isrc="GBUM71029604",
            cached_at=now,
        )
        resp = TrackResponse.model_validate(track)
        assert resp.id == 10
        assert resp.spotify_id == "sp_t_10"
        assert resp.name == "Bohemian Rhapsody"
        assert resp.artist_names == ["Queen"]
        assert resp.album_name == "A Night at the Opera"
        assert resp.duration_ms == 354000
        assert resp.popularity == 95
        assert resp.isrc == "GBUM71029604"
        assert resp.cached_at == now

    def test_json_field_multiple_artists(self) -> None:
        now = datetime.now(UTC)
        track = Track(
            id=11,
            spotify_id="sp_t_11",
            name="Collab Track",
            artist_names=["Artist A", "Artist B", "Artist C"],
            duration_ms=200000,
            cached_at=now,
        )
        resp = TrackResponse.model_validate(track)
        assert resp.artist_names == ["Artist A", "Artist B", "Artist C"]

    def test_nullable_fields(self) -> None:
        now = datetime.now(UTC)
        track = Track(
            id=12,
            spotify_id="sp_t_12",
            name="Minimal",
            duration_ms=0,
            cached_at=now,
        )
        resp = TrackResponse.model_validate(track)
        assert resp.artist_names is None
        assert resp.album_name is None
        assert resp.album_id is None
        assert resp.popularity is None
        assert resp.isrc is None


class TestAudioFeaturesResponseSchema:
    """AudioFeaturesResponse from_attributes round-trip."""

    def test_from_orm_object(self) -> None:
        now = datetime(2025, 5, 1)
        af = AudioFeatures(
            id=1,
            track_id=10,
            source=AudioFeaturesSource.spotify,
            danceability=0.72,
            energy=0.65,
            valence=0.55,
            tempo=128.5,
            key=7,
            mode=1,
            loudness=-6.2,
            speechiness=0.04,
            acousticness=0.12,
            instrumentalness=0.0,
            liveness=0.08,
            cached_at=now,
        )
        resp = AudioFeaturesResponse.model_validate(af)
        assert resp.id == 1
        assert resp.track_id == 10
        assert resp.source == AudioFeaturesSource.spotify
        assert resp.danceability == 0.72
        assert resp.energy == 0.65
        assert resp.tempo == 128.5
        assert resp.key == 7
        assert resp.cached_at == now

    def test_nullable_feature_fields(self) -> None:
        now = datetime.now(UTC)
        af = AudioFeatures(
            id=2,
            track_id=20,
            source=AudioFeaturesSource.spotify,
            cached_at=now,
        )
        resp = AudioFeaturesResponse.model_validate(af)
        assert resp.danceability is None
        assert resp.energy is None
        assert resp.valence is None
        assert resp.tempo is None


class TestScheduledJobResponseSchema:
    """ScheduledJobResponse from_attributes round-trip."""

    def test_from_orm_object(self) -> None:
        now = datetime(2025, 7, 1)
        job = ScheduledJob(
            id=5,
            user_id=1,
            name="Nightly update",
            job_type=JobType.archive,
            playlist_id=10,
            config={"max": 100},
            cron_expression="0 0 * * *",
            enabled=True,
            last_run_at=now,
            next_run_at=now,
            created_at=now,
            updated_at=now,
        )
        resp = ScheduledJobResponse.model_validate(job)
        assert resp.id == 5
        assert resp.user_id == 1
        assert resp.name == "Nightly update"
        assert resp.job_type == JobType.archive
        assert resp.config == {"max": 100}
        assert resp.cron_expression == "0 0 * * *"
        assert resp.enabled is True

    def test_nullable_fields(self) -> None:
        now = datetime.now(UTC)
        job = ScheduledJob(
            id=6,
            user_id=2,
            name="Minimal",
            job_type=JobType.deduplicate,
            cron_expression="*/5 * * * *",
            created_at=now,
            updated_at=now,
        )
        resp = ScheduledJobResponse.model_validate(job)
        assert resp.playlist_id is None
        assert resp.config is None
        assert resp.last_run_at is None
        assert resp.next_run_at is None


class TestAudioFeaturesFieldBounds:
    """AudioFeatures fields should respect their ge/le constraints on the model."""

    @pytest.mark.parametrize(
        "field",
        [
            "danceability",
            "energy",
            "valence",
            "speechiness",
            "acousticness",
            "instrumentalness",
            "liveness",
        ],
    )
    def test_zero_to_one_fields_accept_boundaries(self, field) -> None:
        """Fields bounded [0.0, 1.0] should accept both endpoints."""
        af_low = AudioFeatures(track_id=1, source=AudioFeaturesSource.spotify, **{field: 0.0})
        assert getattr(af_low, field) == 0.0

        af_high = AudioFeatures(track_id=1, source=AudioFeaturesSource.spotify, **{field: 1.0})
        assert getattr(af_high, field) == 1.0

    def test_key_boundaries(self) -> None:
        af = AudioFeatures(track_id=1, source=AudioFeaturesSource.spotify, key=-1)
        assert af.key == -1
        af2 = AudioFeatures(track_id=1, source=AudioFeaturesSource.spotify, key=11)
        assert af2.key == 11

    def test_mode_boundaries(self) -> None:
        af0 = AudioFeatures(track_id=1, source=AudioFeaturesSource.spotify, mode=0)
        assert af0.mode == 0
        af1 = AudioFeatures(track_id=1, source=AudioFeaturesSource.spotify, mode=1)
        assert af1.mode == 1

    def test_tempo_non_negative(self) -> None:
        af = AudioFeatures(track_id=1, source=AudioFeaturesSource.spotify, tempo=0.0)
        assert af.tempo == 0.0
        af2 = AudioFeatures(track_id=1, source=AudioFeaturesSource.spotify, tempo=300.0)
        assert af2.tempo == 300.0


class TestPlaylistCreateStrictMode:
    """PlaylistCreate uses ConfigDict(strict=True)."""

    def test_rejects_int_for_bool_public(self) -> None:
        """strict mode should reject 1 for a bool field."""
        with pytest.raises(ValidationError):
            PlaylistCreate(name="test", public=1)  # type: ignore[arg-type]

    def test_rejects_int_for_bool_collaborative(self) -> None:
        with pytest.raises(ValidationError):
            PlaylistCreate(name="test", collaborative=1)  # type: ignore[arg-type]


class TestPlaylistUpdateStrictMode:
    """PlaylistUpdate uses ConfigDict(strict=True)."""

    def test_rejects_int_for_bool_public(self) -> None:
        with pytest.raises(ValidationError):
            PlaylistUpdate(public=1)  # type: ignore[arg-type]


class TestJsonFieldSerialization:
    """JSON fields (genres, artist_names, config, conditions, actions) can hold
    various JSON-compatible structures."""

    def test_track_artist_names_empty_list(self) -> None:
        track = Track(spotify_id="t_empty", name="Empty Artists", artist_names=[])
        assert track.artist_names == []

    def test_artist_genres_empty_list(self) -> None:
        artist = Artist(spotify_id="a_empty", name="No Genre", genres=[])
        assert artist.genres == []

    def test_album_artist_ids_single(self) -> None:
        album = Album(spotify_id="al_single", name="Solo", artist_ids=["one"])
        assert album.artist_ids == ["one"]

    def test_scheduled_job_nested_config(self) -> None:
        job = ScheduledJob(
            user_id=1,
            name="Nested",
            job_type=JobType.time_capsule,
            cron_expression="0 0 * * 0",
            config={
                "metrics": ["plays", "saves"],
                "filters": {"genre": "jazz", "min_popularity": 50},
            },
        )
        assert job.config["metrics"] == ["plays", "saves"]
        assert job.config["filters"]["genre"] == "jazz"
