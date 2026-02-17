"""SQLModel database models and Pydantic schemas for SpotifyForge.

Defines all persistent entities (Users, Tracks, Artists, Albums, AudioFeatures,
Playlists, PlaylistTracks, ScheduledJobs, CurationRules) and the Pydantic
request/response schemas consumed by the CLI and FastAPI web layers.
"""

import enum
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict
from pydantic import Field as PydanticField
from sqlalchemy import Column, Index, Text, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy.types import JSON
from sqlmodel import Field, Relationship, SQLModel

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class AudioFeaturesSource(enum.StrEnum):
    """Origin of audio feature data."""

    spotify = "spotify"
    soundnet = "soundnet"
    cyanite = "cyanite"


class JobType(enum.StrEnum):
    """Kinds of scheduled jobs the platform can execute."""

    playlist_update = "playlist_update"
    playlist_sync = "playlist_sync"
    playlist_archive = "playlist_archive"
    discovery_refresh = "discovery_refresh"
    stats_snapshot = "stats_snapshot"
    health_check = "health_check"


class RuleType(enum.StrEnum):
    """Categories of automated curation rules."""

    filter = "filter"
    sort = "sort"
    deduplicate = "deduplicate"
    limit = "limit"
    add_tracks = "add_tracks"
    remove_tracks = "remove_tracks"
    replace_tracks = "replace_tracks"


# ---------------------------------------------------------------------------
# Database Models (table=True)
# ---------------------------------------------------------------------------


class User(SQLModel, table=True):
    """A SpotifyForge user, linked to a Spotify account via OAuth."""

    __tablename__ = "users"

    id: int | None = Field(default=None, primary_key=True)
    spotify_id: str = Field(index=True, unique=True, max_length=64)
    display_name: str | None = Field(default=None, max_length=256)
    email: str | None = Field(default=None, max_length=320)

    # OAuth tokens — stored encrypted (Fernet) in the database.
    access_token_enc: str | None = Field(default=None, sa_column=Column(Text, nullable=True, index=True))
    refresh_token_enc: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    token_expiry: datetime | None = Field(default=None)
    token_hash: str | None = Field(default=None, index=True)

    is_premium: bool = Field(default=False)

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    # Relationships
    playlists: list["Playlist"] = Relationship(back_populates="owner")
    scheduled_jobs: list["ScheduledJob"] = Relationship(back_populates="user")
    curation_rules: list["CurationRule"] = Relationship(back_populates="user")


class Track(SQLModel, table=True):
    """Cached Spotify track metadata."""

    __tablename__ = "tracks"

    id: int | None = Field(default=None, primary_key=True)
    spotify_id: str = Field(index=True, unique=True, max_length=64)
    name: str = Field(max_length=512)
    artist_names: list[str] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    album_name: str | None = Field(default=None, max_length=512)
    album_id: str | None = Field(default=None, max_length=64)
    duration_ms: int = Field(default=0)
    popularity: int | None = Field(default=None, ge=0, le=100)
    isrc: str | None = Field(default=None, max_length=16)
    cached_at: datetime = Field(default_factory=datetime.utcnow)

    # Relationships
    audio_features: list["AudioFeatures"] = Relationship(back_populates="track")
    playlist_tracks: list["PlaylistTrack"] = Relationship(back_populates="track")


class Artist(SQLModel, table=True):
    """Cached Spotify artist metadata."""

    __tablename__ = "artists"

    id: int | None = Field(default=None, primary_key=True)
    spotify_id: str = Field(index=True, unique=True, max_length=64)
    name: str = Field(max_length=512)
    genres: list[str] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    popularity: int | None = Field(default=None, ge=0, le=100)
    cached_at: datetime = Field(default_factory=datetime.utcnow)


class Album(SQLModel, table=True):
    """Cached Spotify album metadata."""

    __tablename__ = "albums"

    id: int | None = Field(default=None, primary_key=True)
    spotify_id: str = Field(index=True, unique=True, max_length=64)
    name: str = Field(max_length=512)
    artist_ids: list[str] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    release_date: str | None = Field(default=None, max_length=16)
    total_tracks: int | None = Field(default=None)
    cached_at: datetime = Field(default_factory=datetime.utcnow)


class AudioFeatures(SQLModel, table=True):
    """Audio analysis features for a track, potentially from multiple sources."""

    __tablename__ = "audio_features"
    __table_args__ = (
        UniqueConstraint("track_id", "source", name="uq_audio_features_track_source"),
    )

    id: int | None = Field(default=None, primary_key=True)
    track_id: int = Field(foreign_key="tracks.id", index=True)
    source: AudioFeaturesSource = Field(
        sa_column=Column(
            SAEnum(AudioFeaturesSource, name="audio_features_source", native_enum=False),
            nullable=False,
            default=AudioFeaturesSource.spotify,
        )
    )

    # Core audio features (all 0.0 – 1.0 except tempo, loudness, key, mode)
    danceability: float | None = Field(default=None, ge=0.0, le=1.0)
    energy: float | None = Field(default=None, ge=0.0, le=1.0)
    valence: float | None = Field(default=None, ge=0.0, le=1.0)
    tempo: float | None = Field(default=None, ge=0.0)
    key: int | None = Field(default=None, ge=-1, le=11)
    mode: int | None = Field(default=None, ge=0, le=1)
    loudness: float | None = Field(default=None)
    speechiness: float | None = Field(default=None, ge=0.0, le=1.0)
    acousticness: float | None = Field(default=None, ge=0.0, le=1.0)
    instrumentalness: float | None = Field(default=None, ge=0.0, le=1.0)
    liveness: float | None = Field(default=None, ge=0.0, le=1.0)

    cached_at: datetime = Field(default_factory=datetime.utcnow)

    # Relationship
    track: Track | None = Relationship(back_populates="audio_features")


class Playlist(SQLModel, table=True):
    """A Spotify playlist managed by SpotifyForge."""

    __tablename__ = "playlists"

    id: int | None = Field(default=None, primary_key=True)
    spotify_id: str = Field(index=True, max_length=64)
    owner_id: int = Field(foreign_key="users.id", index=True)
    name: str = Field(max_length=512)
    description: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    public: bool = Field(default=True)
    collaborative: bool = Field(default=False)
    snapshot_id: str | None = Field(default=None, max_length=128)
    follower_count: int = Field(default=0)
    track_count: int = Field(default=0)
    last_synced_at: datetime | None = Field(default=None)
    deleted_at: datetime | None = Field(default=None, index=True)

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    # Relationships
    owner: User | None = Relationship(back_populates="playlists")
    playlist_tracks: list["PlaylistTrack"] = Relationship(back_populates="playlist")
    scheduled_jobs: list["ScheduledJob"] = Relationship(back_populates="playlist")
    curation_rules: list["CurationRule"] = Relationship(back_populates="playlist")


class PlaylistTrack(SQLModel, table=True):
    """Association between a playlist and its tracks, preserving order."""

    __tablename__ = "playlist_tracks"
    __table_args__ = (
        UniqueConstraint("playlist_id", "track_id", name="uq_playlist_track"),
        Index("ix_playlist_tracks_playlist_position", "playlist_id", "position"),
    )

    id: int | None = Field(default=None, primary_key=True)
    playlist_id: int = Field(foreign_key="playlists.id", index=True)
    track_id: int = Field(foreign_key="tracks.id", index=True)
    position: int = Field(default=0)
    added_at: datetime | None = Field(default=None)
    added_by: str | None = Field(default=None, max_length=64)

    # Relationships
    playlist: Playlist | None = Relationship(back_populates="playlist_tracks")
    track: Track | None = Relationship(back_populates="playlist_tracks")


# Alias used by the repository layer
PlaylistTrackLink = PlaylistTrack


class ScheduledJob(SQLModel, table=True):
    """A recurring automation job configured by a user."""

    __tablename__ = "scheduled_jobs"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    name: str = Field(max_length=256)
    job_type: JobType = Field(
        sa_column=Column(
            SAEnum(JobType, name="job_type", native_enum=False),
            nullable=False,
        )
    )
    playlist_id: int | None = Field(default=None, foreign_key="playlists.id", index=True)
    config: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    cron_expression: str = Field(max_length=128)
    enabled: bool = Field(default=True, index=True)

    last_run_at: datetime | None = Field(default=None)
    next_run_at: datetime | None = Field(default=None)
    failure_count: int = Field(default=0)
    last_error: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    # Relationships
    user: User | None = Relationship(back_populates="scheduled_jobs")
    playlist: Playlist | None = Relationship(back_populates="scheduled_jobs")


class CurationRule(SQLModel, table=True):
    """A declarative rule that drives automated playlist curation."""

    __tablename__ = "curation_rules"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    playlist_id: int | None = Field(default=None, foreign_key="playlists.id", index=True)
    name: str = Field(max_length=256)
    rule_type: RuleType = Field(
        sa_column=Column(
            SAEnum(RuleType, name="rule_type", native_enum=False),
            nullable=False,
        )
    )
    conditions: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )
    actions: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    enabled: bool = Field(default=True)
    priority: int = Field(default=0)

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    # Relationships
    user: User | None = Relationship(back_populates="curation_rules")
    playlist: Playlist | None = Relationship(back_populates="curation_rules")


# ---------------------------------------------------------------------------
# Pydantic Schemas (API request / response)
# ---------------------------------------------------------------------------
# These are plain Pydantic models (not SQLModel) so they stay decoupled from
# the database layer and can evolve independently.


class PlaylistCreate(BaseModel):
    """Schema for creating a new playlist via the API."""

    model_config = ConfigDict(strict=True)

    name: str = PydanticField(min_length=1, max_length=512)
    description: str | None = PydanticField(default=None, max_length=2000)
    public: bool = True
    collaborative: bool = False


class PlaylistUpdate(BaseModel):
    """Schema for updating an existing playlist. All fields optional."""

    model_config = ConfigDict(strict=True)

    name: str | None = PydanticField(default=None, min_length=1, max_length=512)
    description: str | None = PydanticField(default=None, max_length=2000)
    public: bool | None = None
    collaborative: bool | None = None


class PlaylistResponse(BaseModel):
    """Schema returned when reading playlist data."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    spotify_id: str
    owner_id: int
    name: str
    description: str | None = None
    public: bool
    collaborative: bool
    snapshot_id: str | None = None
    follower_count: int
    track_count: int
    last_synced_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class TrackResponse(BaseModel):
    """Schema returned when reading track data."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    spotify_id: str
    name: str
    artist_names: list[str] | None = None
    album_name: str | None = None
    album_id: str | None = None
    duration_ms: int
    popularity: int | None = None
    isrc: str | None = None
    cached_at: datetime


class AudioFeaturesResponse(BaseModel):
    """Schema returned when reading audio features."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    track_id: int
    source: AudioFeaturesSource
    danceability: float | None = None
    energy: float | None = None
    valence: float | None = None
    tempo: float | None = None
    key: int | None = None
    mode: int | None = None
    loudness: float | None = None
    speechiness: float | None = None
    acousticness: float | None = None
    instrumentalness: float | None = None
    liveness: float | None = None
    cached_at: datetime


class ScheduledJobCreate(BaseModel):
    """Schema for creating a new scheduled job."""

    model_config = ConfigDict(strict=True)

    name: str = PydanticField(min_length=1, max_length=256)
    job_type: JobType
    playlist_id: int | None = None
    config: dict[str, Any] | None = None
    cron_expression: str = PydanticField(min_length=1, max_length=128)
    enabled: bool = True


class ScheduledJobResponse(BaseModel):
    """Schema returned when reading scheduled job data."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    name: str
    job_type: JobType
    playlist_id: int | None = None
    config: dict[str, Any] | None = None
    cron_expression: str
    enabled: bool
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class CurationRuleCreate(BaseModel):
    """Schema for creating a new curation rule."""

    model_config = ConfigDict(strict=True)

    name: str = PydanticField(min_length=1, max_length=256)
    rule_type: RuleType
    playlist_id: int | None = None
    conditions: dict[str, Any] | None = None
    actions: dict[str, Any] | None = None
    enabled: bool = True
    priority: int = 0


class CurationRuleResponse(BaseModel):
    """Schema returned when reading curation rule data."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    playlist_id: int | None = None
    name: str
    rule_type: RuleType
    conditions: dict[str, Any] | None = None
    actions: dict[str, Any] | None = None
    enabled: bool
    priority: int
    created_at: datetime
    updated_at: datetime
