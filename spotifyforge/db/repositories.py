"""Repository classes providing a clean data-access layer for SpotifyForge.

Every repository receives a :class:`sqlmodel.Session` at construction time so
that the caller controls transaction boundaries.  All queries use SQLModel's
``select()`` statement builder.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from sqlmodel import Session, select

from spotifyforge.models.models import (
    Artist,
    AudioFeatures,
    Playlist,
    PlaylistTrack,
    ScheduledJob,
    Track,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    """Return the current UTC timestamp (timezone-aware)."""
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# TrackRepository
# ---------------------------------------------------------------------------


class TrackRepository:
    """CRUD operations for :class:`Track` with cache-awareness."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # -- upsert -------------------------------------------------------------

    def upsert(self, track_data: dict) -> Track:
        """Insert a new track or update an existing one by ``spotify_id``.

        ``cached_at`` is automatically set to the current UTC time.
        """
        spotify_id: str = track_data["spotify_id"]
        existing = self.get_by_spotify_id(spotify_id)

        if existing is not None:
            for key, value in track_data.items():
                setattr(existing, key, value)
            existing.cached_at = _utcnow()
            self.session.add(existing)
            self.session.commit()
            self.session.refresh(existing)
            return existing

        track = Track(**track_data, cached_at=_utcnow())
        self.session.add(track)
        self.session.commit()
        self.session.refresh(track)
        return track

    def upsert_many(self, tracks: list[dict]) -> list[Track]:
        """Batch upsert a list of track dicts."""
        results: list[Track] = []
        for track_data in tracks:
            spotify_id: str = track_data["spotify_id"]
            existing = self.get_by_spotify_id(spotify_id)

            if existing is not None:
                for key, value in track_data.items():
                    setattr(existing, key, value)
                existing.cached_at = _utcnow()
                self.session.add(existing)
                results.append(existing)
            else:
                track = Track(**track_data, cached_at=_utcnow())
                self.session.add(track)
                results.append(track)

        self.session.commit()
        for track in results:
            self.session.refresh(track)
        return results

    # -- read ---------------------------------------------------------------

    def get_by_spotify_id(self, spotify_id: str) -> Track | None:
        """Return a single track by its Spotify ID, or ``None``."""
        statement = select(Track).where(Track.spotify_id == spotify_id)
        return self.session.exec(statement).first()

    def get_many_by_spotify_ids(self, ids: list[str]) -> list[Track]:
        """Return all tracks whose ``spotify_id`` is in *ids*."""
        if not ids:
            return []
        statement = select(Track).where(Track.spotify_id.in_(ids))  # type: ignore[union-attr]
        return list(self.session.exec(statement).all())

    def get_stale(self, ttl_seconds: int) -> list[Track]:
        """Return tracks whose ``cached_at`` is older than *ttl_seconds*."""
        cutoff = datetime.fromtimestamp(_utcnow().timestamp() - ttl_seconds, tz=UTC)
        statement = select(Track).where(Track.cached_at < cutoff)
        return list(self.session.exec(statement).all())

    def search(self, query: str, limit: int = 20) -> list[Track]:
        """Case-insensitive name search with a result *limit*."""
        pattern = f"%{query}%"
        statement = (
            select(Track)
            .where(Track.name.ilike(pattern))  # type: ignore[union-attr]
            .limit(limit)
        )
        return list(self.session.exec(statement).all())


# ---------------------------------------------------------------------------
# ArtistRepository
# ---------------------------------------------------------------------------


class ArtistRepository:
    """CRUD operations for :class:`Artist` with cache-awareness."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # -- upsert -------------------------------------------------------------

    def upsert(self, artist_data: dict) -> Artist:
        """Insert a new artist or update an existing one by ``spotify_id``."""
        spotify_id: str = artist_data["spotify_id"]
        existing = self.get_by_spotify_id(spotify_id)

        if existing is not None:
            for key, value in artist_data.items():
                setattr(existing, key, value)
            existing.cached_at = _utcnow()
            self.session.add(existing)
            self.session.commit()
            self.session.refresh(existing)
            return existing

        artist = Artist(**artist_data, cached_at=_utcnow())
        self.session.add(artist)
        self.session.commit()
        self.session.refresh(artist)
        return artist

    def upsert_many(self, artists: list[dict]) -> list[Artist]:
        """Batch upsert a list of artist dicts."""
        results: list[Artist] = []
        for artist_data in artists:
            spotify_id: str = artist_data["spotify_id"]
            existing = self.get_by_spotify_id(spotify_id)

            if existing is not None:
                for key, value in artist_data.items():
                    setattr(existing, key, value)
                existing.cached_at = _utcnow()
                self.session.add(existing)
                results.append(existing)
            else:
                artist = Artist(**artist_data, cached_at=_utcnow())
                self.session.add(artist)
                results.append(artist)

        self.session.commit()
        for artist in results:
            self.session.refresh(artist)
        return results

    # -- read ---------------------------------------------------------------

    def get_by_spotify_id(self, spotify_id: str) -> Artist | None:
        """Return a single artist by its Spotify ID, or ``None``."""
        statement = select(Artist).where(Artist.spotify_id == spotify_id)
        return self.session.exec(statement).first()

    def get_many_by_spotify_ids(self, ids: list[str]) -> list[Artist]:
        """Return all artists whose ``spotify_id`` is in *ids*."""
        if not ids:
            return []
        statement = select(Artist).where(Artist.spotify_id.in_(ids))  # type: ignore[union-attr]
        return list(self.session.exec(statement).all())

    def get_stale(self, ttl_seconds: int) -> list[Artist]:
        """Return artists whose ``cached_at`` is older than *ttl_seconds*."""
        cutoff = datetime.fromtimestamp(_utcnow().timestamp() - ttl_seconds, tz=UTC)
        statement = select(Artist).where(Artist.cached_at < cutoff)
        return list(self.session.exec(statement).all())

    def search(self, query: str, limit: int = 20) -> list[Artist]:
        """Case-insensitive name search with a result *limit*."""
        pattern = f"%{query}%"
        statement = (
            select(Artist)
            .where(Artist.name.ilike(pattern))  # type: ignore[union-attr]
            .limit(limit)
        )
        return list(self.session.exec(statement).all())


# ---------------------------------------------------------------------------
# PlaylistRepository
# ---------------------------------------------------------------------------


class PlaylistRepository:
    """CRUD and synchronisation operations for :class:`Playlist`."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # -- create / read / update / delete ------------------------------------

    def create(self, playlist_data: dict) -> Playlist:
        """Persist a new playlist."""
        playlist = Playlist(**playlist_data)
        self.session.add(playlist)
        self.session.commit()
        self.session.refresh(playlist)
        return playlist

    def get_by_id(self, playlist_id: int) -> Playlist | None:
        """Return a playlist by primary key."""
        return self.session.get(Playlist, playlist_id)

    def get_by_spotify_id(self, spotify_id: str) -> Playlist | None:
        """Return a playlist by its Spotify ID."""
        statement = select(Playlist).where(Playlist.spotify_id == spotify_id)
        return self.session.exec(statement).first()

    def get_by_user(self, user_id: str) -> list[Playlist]:
        """Return all playlists belonging to *user_id*."""
        statement = select(Playlist).where(Playlist.user_id == user_id)
        return list(self.session.exec(statement).all())

    def update(self, playlist: Playlist, data: dict) -> Playlist:
        """Apply *data* to an existing *playlist* and persist."""
        for key, value in data.items():
            setattr(playlist, key, value)
        self.session.add(playlist)
        self.session.commit()
        self.session.refresh(playlist)
        return playlist

    def delete(self, playlist: Playlist) -> None:
        """Remove a playlist from the database."""
        self.session.delete(playlist)
        self.session.commit()

    # -- sync ---------------------------------------------------------------

    def sync_tracks(
        self,
        playlist_id: int,
        track_ids: Sequence[int],
        snapshot_id: str,
    ) -> None:
        """Replace all tracks in a playlist and update its snapshot.

        Existing :class:`PlaylistTrack` rows for *playlist_id* are deleted
        and replaced with new rows preserving the order given by *track_ids*.
        """
        # Remove existing links
        existing_links = self.session.exec(
            select(PlaylistTrack).where(PlaylistTrack.playlist_id == playlist_id)
        ).all()
        for link in existing_links:
            self.session.delete(link)

        # Insert new links with positional ordering
        for position, track_id in enumerate(track_ids):
            link = PlaylistTrack(
                playlist_id=playlist_id,
                track_id=track_id,
                position=position,
            )
            self.session.add(link)

        # Update the playlist snapshot
        playlist = self.get_by_id(playlist_id)
        if playlist is not None:
            playlist.snapshot_id = snapshot_id
            self.session.add(playlist)

        self.session.commit()

    def needs_sync(self, playlist_id: int, snapshot_id: str) -> bool:
        """Return ``True`` if the stored snapshot differs from *snapshot_id*.

        A playlist that does not exist in the database is considered as
        needing synchronisation.
        """
        playlist = self.get_by_id(playlist_id)
        if playlist is None:
            return True
        return playlist.snapshot_id != snapshot_id


# ---------------------------------------------------------------------------
# AudioFeaturesRepository
# ---------------------------------------------------------------------------


class AudioFeaturesRepository:
    """CRUD operations for :class:`AudioFeatures`."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # -- upsert -------------------------------------------------------------

    def upsert(self, features: dict) -> AudioFeatures:
        """Insert or update audio features for a track."""
        track_id: int = features["track_id"]
        existing = self.get_by_track_id(track_id)

        if existing is not None:
            for key, value in features.items():
                setattr(existing, key, value)
            existing.cached_at = _utcnow()
            self.session.add(existing)
            self.session.commit()
            self.session.refresh(existing)
            return existing

        af = AudioFeatures(**features, cached_at=_utcnow())
        self.session.add(af)
        self.session.commit()
        self.session.refresh(af)
        return af

    def upsert_many(self, features_list: list[dict]) -> list[AudioFeatures]:
        """Batch upsert a list of audio-feature dicts."""
        results: list[AudioFeatures] = []
        for features in features_list:
            track_id: int = features["track_id"]
            existing = self.get_by_track_id(track_id)

            if existing is not None:
                for key, value in features.items():
                    setattr(existing, key, value)
                existing.cached_at = _utcnow()
                self.session.add(existing)
                results.append(existing)
            else:
                af = AudioFeatures(**features, cached_at=_utcnow())
                self.session.add(af)
                results.append(af)

        self.session.commit()
        for af in results:
            self.session.refresh(af)
        return results

    # -- read ---------------------------------------------------------------

    def get_by_track_id(self, track_id: int) -> AudioFeatures | None:
        """Return audio features for a given track, or ``None``."""
        statement = select(AudioFeatures).where(AudioFeatures.track_id == track_id)
        return self.session.exec(statement).first()

    def get_missing_track_ids(self, track_ids: list[int]) -> list[int]:
        """Return those *track_ids* that have no cached audio features."""
        if not track_ids:
            return []
        statement = select(AudioFeatures.track_id).where(
            AudioFeatures.track_id.in_(track_ids)  # type: ignore[union-attr]
        )
        cached_ids = set(self.session.exec(statement).all())
        return [tid for tid in track_ids if tid not in cached_ids]


# ---------------------------------------------------------------------------
# ScheduledJobRepository
# ---------------------------------------------------------------------------


class ScheduledJobRepository:
    """CRUD operations for :class:`ScheduledJob`."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # -- create / read / update / delete ------------------------------------

    def create(self, job_data: dict) -> ScheduledJob:
        """Persist a new scheduled job."""
        job = ScheduledJob(**job_data)
        self.session.add(job)
        self.session.commit()
        self.session.refresh(job)
        return job

    def get_by_id(self, job_id: int) -> ScheduledJob | None:
        """Return a job by primary key."""
        return self.session.get(ScheduledJob, job_id)

    def get_enabled_jobs(self) -> list[ScheduledJob]:
        """Return all jobs that are currently enabled."""
        statement = select(ScheduledJob).where(ScheduledJob.enabled == True)  # noqa: E712
        return list(self.session.exec(statement).all())

    def get_by_user(self, user_id: str) -> list[ScheduledJob]:
        """Return all jobs belonging to *user_id*."""
        statement = select(ScheduledJob).where(ScheduledJob.user_id == user_id)
        return list(self.session.exec(statement).all())

    def update(self, job: ScheduledJob, data: dict) -> ScheduledJob:
        """Apply *data* to an existing *job* and persist."""
        for key, value in data.items():
            setattr(job, key, value)
        self.session.add(job)
        self.session.commit()
        self.session.refresh(job)
        return job

    def update_last_run(self, job_id: int, timestamp: datetime) -> ScheduledJob | None:
        """Set the ``last_run_at`` field for the given job.

        Returns the updated job, or ``None`` if the job does not exist.
        """
        job = self.get_by_id(job_id)
        if job is None:
            return None
        job.last_run_at = timestamp
        self.session.add(job)
        self.session.commit()
        self.session.refresh(job)
        return job

    def delete(self, job: ScheduledJob) -> None:
        """Remove a scheduled job from the database."""
        self.session.delete(job)
        self.session.commit()
