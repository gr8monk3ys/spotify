"""Central playlist management service for SpotifyForge.

Provides high-level operations for creating, syncing, and manipulating
Spotify playlists while keeping the local database in sync.  All public
methods use Tekore's async API and handle chunking, pagination, and error
recovery internally so callers never have to worry about Spotify rate
limits or page sizes.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any

import tekore as tk
from sqlmodel import select

from spotifyforge.db.engine import get_async_session
from spotifyforge.models.models import Playlist, PlaylistTrack, Track

if TYPE_CHECKING:
    from tekore import Spotify

logger = logging.getLogger(__name__)

# Spotify imposes a maximum of 100 track URIs per add/remove request.
_CHUNK_SIZE = 100


class PlaylistManager:
    """High-level playlist operations backed by Tekore and a local SQLite DB.

    Parameters
    ----------
    spotify:
        An authenticated :class:`tekore.Spotify` client instance.  The caller
        is responsible for token management; this class simply uses whatever
        client it receives.
    """

    def __init__(self, spotify: Spotify) -> None:
        self._sp = spotify

    # ------------------------------------------------------------------
    # Read / List
    # ------------------------------------------------------------------

    async def get_user_playlists(self) -> list[dict[str, Any]]:
        """Fetch the current user's playlists from Spotify.

        Returns a list of dicts with keys: ``id``, ``name``,
        ``track_count``, ``public``, ``followers``.
        """
        try:
            user = await self._sp.current_user()
            paging = await self._sp.playlists(user.id, limit=50)
        except tk.HTTPError as exc:
            logger.error("Failed to fetch user playlists: %s", exc)
            raise

        playlists: list[dict[str, Any]] = []

        def _collect(page):
            if page and page.items:
                for pl in page.items:
                    playlists.append(
                        {
                            "id": pl.id,
                            "name": pl.name,
                            "track_count": pl.tracks.total if pl.tracks else 0,
                            "public": pl.public,
                            "followers": pl.followers.total if pl.followers else 0,
                        }
                    )

        _collect(paging)
        while paging.next is not None:
            try:
                paging = await self._sp.next(paging)
            except tk.HTTPError as exc:
                logger.error("Failed during playlist pagination: %s", exc)
                raise
            if paging is None:
                break
            _collect(paging)

        return playlists

    async def get_playlist_details(self, playlist_id: str) -> dict[str, Any]:
        """Fetch a single playlist with its tracks from Spotify.

        Returns a dict with ``meta`` (playlist metadata) and ``tracks``
        (list of track dicts).
        """
        try:
            sp_playlist = await self._sp.playlist(playlist_id)
        except tk.HTTPError as exc:
            logger.error("Failed to fetch playlist %s: %s", playlist_id, exc)
            raise

        meta: dict[str, Any] = {
            "name": sp_playlist.name,
            "description": sp_playlist.description or "",
            "owner": sp_playlist.owner.display_name if sp_playlist.owner else "N/A",
            "track_count": sp_playlist.tracks.total if sp_playlist.tracks else 0,
            "followers": sp_playlist.followers.total if sp_playlist.followers else 0,
            "public": sp_playlist.public,
        }

        all_items = await self.get_playlist_tracks(playlist_id)
        tracks: list[dict[str, Any]] = []
        for item in all_items:
            track = item.track
            if track is None or track.id is None:
                continue
            artist_names = ", ".join(a.name for a in track.artists) if track.artists else "Unknown"
            tracks.append(
                {
                    "name": track.name,
                    "artist": artist_names,
                    "album": track.album.name if track.album else "Unknown",
                    "duration_ms": track.duration_ms,
                    "uri": track.uri,
                }
            )

        return {"meta": meta, "tracks": tracks}

    # ------------------------------------------------------------------
    # Sync
    # ------------------------------------------------------------------

    async def sync_playlist(self, playlist_id: str) -> Playlist:
        """Fetch a playlist from Spotify and upsert it into the local DB.

        All tracks are resolved and stored; the local ``PlaylistTrack``
        association rows are rebuilt so they mirror Spotify's ordering.

        Returns the local :class:`Playlist` row after sync.
        """
        try:
            sp_playlist = await self._sp.playlist(playlist_id)
        except tk.HTTPError as exc:
            logger.error("Failed to fetch playlist %s from Spotify: %s", playlist_id, exc)
            raise

        # Gather every track via auto-pagination.
        all_tracks = await self.get_playlist_tracks(playlist_id)

        async with get_async_session() as session:
            # Upsert the playlist row.
            result = await session.execute(
                select(Playlist).where(Playlist.spotify_id == playlist_id)
            )
            db_playlist: Playlist | None = result.scalars().first()

            if db_playlist is None:
                db_playlist = Playlist(
                    spotify_id=sp_playlist.id,
                    name=sp_playlist.name,
                    description=sp_playlist.description or "",
                    public=sp_playlist.public if sp_playlist.public is not None else True,
                    collaborative=sp_playlist.collaborative,
                    snapshot_id=sp_playlist.snapshot_id,
                    track_count=sp_playlist.tracks.total,
                    last_synced_at=datetime.utcnow(),
                    # owner_id will be set by the caller if needed
                    owner_id=0,
                )
                session.add(db_playlist)
            else:
                db_playlist.name = sp_playlist.name
                db_playlist.description = sp_playlist.description or ""
                db_playlist.public = sp_playlist.public if sp_playlist.public is not None else True
                db_playlist.collaborative = sp_playlist.collaborative
                db_playlist.snapshot_id = sp_playlist.snapshot_id
                db_playlist.track_count = sp_playlist.tracks.total
                db_playlist.last_synced_at = datetime.utcnow()
                db_playlist.updated_at = datetime.utcnow()
                session.add(db_playlist)

            await session.flush()

            # Ensure we have the playlist's PK.
            await session.refresh(db_playlist)
            playlist_pk: int = db_playlist.id  # type: ignore[assignment]

            # Remove old association rows for this playlist.
            old_assocs = await session.execute(
                select(PlaylistTrack).where(PlaylistTrack.playlist_id == playlist_pk)
            )
            for old_row in old_assocs.scalars().all():
                await session.delete(old_row)
            await session.flush()

            # Upsert tracks and rebuild association rows.
            for position, item in enumerate(all_tracks):
                track_obj = item.track
                if track_obj is None or track_obj.id is None:
                    # Local or unavailable tracks are skipped.
                    continue

                track_result = await session.execute(
                    select(Track).where(Track.spotify_id == track_obj.id)
                )
                db_track: Track | None = track_result.scalars().first()

                artist_names = [a.name for a in track_obj.artists] if track_obj.artists else []

                if db_track is None:
                    db_track = Track(
                        spotify_id=track_obj.id,
                        name=track_obj.name,
                        artist_names=artist_names,
                        album_name=track_obj.album.name if track_obj.album else None,
                        album_id=track_obj.album.id if track_obj.album else None,
                        duration_ms=track_obj.duration_ms,
                        popularity=track_obj.popularity,
                        isrc=_extract_isrc(track_obj),
                    )
                    session.add(db_track)
                    await session.flush()
                    await session.refresh(db_track)
                else:
                    db_track.name = track_obj.name
                    db_track.artist_names = artist_names
                    db_track.album_name = track_obj.album.name if track_obj.album else None
                    db_track.duration_ms = track_obj.duration_ms
                    db_track.popularity = track_obj.popularity
                    db_track.cached_at = datetime.utcnow()
                    session.add(db_track)
                    await session.flush()

                assoc = PlaylistTrack(
                    playlist_id=playlist_pk,
                    track_id=db_track.id,  # type: ignore[arg-type]
                    position=position,
                    added_at=_parse_added_at(item),
                    added_by=_extract_added_by(item),
                )
                session.add(assoc)

            await session.commit()
            await session.refresh(db_playlist)

        logger.info(
            "Synced playlist %s (%s) — %d tracks",
            db_playlist.name,
            playlist_id,
            db_playlist.track_count,
        )
        return db_playlist

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    async def create_playlist(
        self,
        name: str,
        description: str = "",
        public: bool = True,
    ) -> Playlist:
        """Create a new playlist on Spotify and persist it locally.

        The current user's Spotify ID is resolved automatically from the
        authenticated client.

        Returns the newly created local :class:`Playlist` row.
        """
        try:
            user = await self._sp.current_user()
            sp_playlist = await self._sp.playlist_create(
                user.id,
                name,
                public=public,
                description=description,
            )
        except tk.HTTPError as exc:
            logger.error("Failed to create playlist on Spotify: %s", exc)
            raise

        async with get_async_session() as session:
            db_playlist = Playlist(
                spotify_id=sp_playlist.id,
                name=sp_playlist.name,
                description=description,
                public=public,
                collaborative=False,
                snapshot_id=sp_playlist.snapshot_id,
                track_count=0,
                last_synced_at=datetime.utcnow(),
                owner_id=0,  # Caller should update with the real FK
            )
            session.add(db_playlist)
            await session.commit()
            await session.refresh(db_playlist)

        logger.info("Created playlist '%s' (%s)", name, sp_playlist.id)
        return db_playlist

    # ------------------------------------------------------------------
    # Add / Remove / Reorder tracks
    # ------------------------------------------------------------------

    async def add_tracks(
        self,
        playlist_id: str,
        track_uris: Sequence[str],
        position: int | None = None,
    ) -> str:
        """Add tracks to a Spotify playlist, chunking at 100 per request.

        Parameters
        ----------
        playlist_id:
            The Spotify playlist ID.
        track_uris:
            An ordered sequence of Spotify track URIs to add.
        position:
            Optional 0-based index at which to insert the tracks.  When
            *None*, tracks are appended to the end of the playlist.

        Returns
        -------
        The latest snapshot ID after all chunks have been added.
        """
        snapshot_id = ""
        for offset in range(0, len(track_uris), _CHUNK_SIZE):
            chunk = list(track_uris[offset : offset + _CHUNK_SIZE])
            insert_pos = (position + offset) if position is not None else None
            try:
                snapshot_id = await self._sp.playlist_add(playlist_id, chunk, position=insert_pos)
            except tk.HTTPError as exc:
                logger.error(
                    "Failed to add tracks (chunk at offset %d) to %s: %s",
                    offset,
                    playlist_id,
                    exc,
                )
                raise

        logger.info("Added %d tracks to playlist %s", len(track_uris), playlist_id)
        return snapshot_id

    async def remove_tracks(
        self,
        playlist_id: str,
        track_uris: Sequence[str],
    ) -> str:
        """Remove tracks from a Spotify playlist, chunking at 100 per request.

        Returns the latest snapshot ID after all chunks have been removed.
        """
        snapshot_id = ""
        for offset in range(0, len(track_uris), _CHUNK_SIZE):
            chunk = list(track_uris[offset : offset + _CHUNK_SIZE])
            try:
                snapshot_id = await self._sp.playlist_remove(playlist_id, chunk)
            except tk.HTTPError as exc:
                logger.error(
                    "Failed to remove tracks (chunk at offset %d) from %s: %s",
                    offset,
                    playlist_id,
                    exc,
                )
                raise

        logger.info("Removed %d tracks from playlist %s", len(track_uris), playlist_id)
        return snapshot_id

    async def reorder_tracks(
        self,
        playlist_id: str,
        range_start: int,
        insert_before: int,
        range_length: int = 1,
    ) -> str:
        """Reorder a block of tracks within a Spotify playlist.

        Parameters
        ----------
        playlist_id:
            The Spotify playlist ID.
        range_start:
            The 0-based position of the first track to move.
        insert_before:
            The 0-based position *before* which the block will be inserted.
        range_length:
            Number of consecutive tracks in the block (default 1).

        Returns
        -------
        The new snapshot ID.
        """
        try:
            snapshot_id = await self._sp.playlist_reorder(
                playlist_id,
                range_start=range_start,
                insert_before=insert_before,
                range_length=range_length,
            )
        except tk.HTTPError as exc:
            logger.error("Failed to reorder tracks in %s: %s", playlist_id, exc)
            raise

        logger.info(
            "Reordered %d track(s) in playlist %s: %d -> %d",
            range_length,
            playlist_id,
            range_start,
            insert_before,
        )
        return snapshot_id

    # ------------------------------------------------------------------
    # Deduplicate
    # ------------------------------------------------------------------

    async def deduplicate(self, playlist_id: str) -> int:
        """Remove duplicate tracks from a playlist.

        A track is considered a duplicate if its URI appears more than once.
        Only the *first* occurrence of each track is kept.

        Returns the number of duplicate occurrences removed.
        """
        all_items = await self.get_playlist_tracks(playlist_id)

        seen: set[str] = set()
        duplicate_uris: list[str] = []

        for item in all_items:
            track = item.track
            if track is None or track.uri is None:
                continue
            if track.uri in seen:
                duplicate_uris.append(track.uri)
            else:
                seen.add(track.uri)

        if not duplicate_uris:
            logger.info("No duplicates found in playlist %s", playlist_id)
            return 0

        await self.remove_tracks(playlist_id, duplicate_uris)
        logger.info(
            "Removed %d duplicate track(s) from playlist %s",
            len(duplicate_uris),
            playlist_id,
        )
        return len(duplicate_uris)

    # ------------------------------------------------------------------
    # Pagination helper
    # ------------------------------------------------------------------

    async def get_playlist_tracks(self, playlist_id: str) -> list[Any]:
        """Return every track item in a playlist with automatic pagination.

        Tekore returns tracks in pages of up to 100.  This method fetches
        all pages and returns a flat list of ``PlaylistTrack`` items (Tekore
        model objects, *not* the local DB model).
        """
        try:
            page = await self._sp.playlist_items(playlist_id, limit=100)
        except tk.HTTPError as exc:
            logger.error("Failed to fetch tracks for playlist %s: %s", playlist_id, exc)
            raise

        items: list[Any] = list(page.items) if page.items else []

        while page.next is not None:
            try:
                page = await self._sp.next(page)
            except tk.HTTPError as exc:
                logger.error("Failed during pagination for playlist %s: %s", playlist_id, exc)
                raise
            if page is None:
                break
            items.extend(page.items if page.items else [])

        return items

    # ------------------------------------------------------------------
    # Snapshot check
    # ------------------------------------------------------------------

    async def snapshot_check(self, playlist_id: str, known_snapshot_id: str) -> bool:
        """Check whether a playlist has changed since a known snapshot.

        Returns *True* if the remote snapshot ID differs from
        *known_snapshot_id*, indicating the playlist has been modified.
        """
        try:
            sp_playlist = await self._sp.playlist(playlist_id, fields="snapshot_id")
        except tk.HTTPError as exc:
            logger.error("Failed to check snapshot for %s: %s", playlist_id, exc)
            raise

        current_snapshot = sp_playlist.snapshot_id
        changed = current_snapshot != known_snapshot_id

        if changed:
            logger.info(
                "Playlist %s has changed (snapshot %s -> %s)",
                playlist_id,
                known_snapshot_id,
                current_snapshot,
            )
        else:
            logger.debug("Playlist %s is unchanged (snapshot %s)", playlist_id, known_snapshot_id)

        return changed


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_isrc(track: Any) -> str | None:
    """Safely extract the ISRC from a Tekore track's external_ids."""
    try:
        ext_ids = track.external_ids
        if ext_ids and hasattr(ext_ids, "isrc"):
            return ext_ids.isrc
    except AttributeError:
        pass
    return None


def _parse_added_at(item: Any) -> datetime | None:
    """Parse the ``added_at`` timestamp from a Tekore playlist item."""
    try:
        if item.added_at is not None:
            if isinstance(item.added_at, datetime):
                return item.added_at
            return datetime.fromisoformat(str(item.added_at).replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        pass
    return None


def _extract_added_by(item: Any) -> str | None:
    """Extract the Spotify user ID of whoever added this track."""
    try:
        if item.added_by and hasattr(item.added_by, "id"):
            return item.added_by.id
    except AttributeError:
        pass
    return None
