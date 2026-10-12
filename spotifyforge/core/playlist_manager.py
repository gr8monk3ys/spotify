"""Central playlist management service for SpotifyForge.

Provides high-level operations for creating and manipulating Spotify
playlists.  All public methods use Tekore's async API and handle chunking, pagination, and error
recovery internally so callers never have to worry about Spotify rate
limits or page sizes.
"""

from __future__ import annotations

import html
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import tekore as tk

if TYPE_CHECKING:
    from tekore import Spotify

logger = logging.getLogger(__name__)

# Spotify imposes a maximum of 100 track URIs per add/remove request.
_CHUNK_SIZE = 100


@dataclass(frozen=True)
class CreatedPlaylist:
    """What Spotify handed back for a playlist this package just created."""

    spotify_id: str
    name: str
    snapshot_id: str


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
        ``description``, ``owner_id``, ``track_count``, ``public``.
        The listing includes playlists the user merely follows —
        ``owner_id`` is what tells them apart. (Spotify's list endpoint
        does not include follower counts; use
        :meth:`get_playlist_details`.)
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
                            # Spotify serves descriptions back HTML-escaped;
                            # unescape here so callers compare plain text.
                            "description": html.unescape(pl.description or ""),
                            "owner_id": pl.owner.id if pl.owner else None,
                            "track_count": pl.tracks.total if pl.tracks else 0,
                            "public": pl.public,
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
    # Create
    # ------------------------------------------------------------------

    async def create_playlist(
        self,
        name: str,
        description: str = "",
        public: bool = True,
        collaborative: bool = False,
    ) -> CreatedPlaylist:
        """Create a new playlist on Spotify.

        The current user's Spotify ID is resolved automatically from the
        authenticated client. Spotify's create endpoint has no
        ``collaborative`` flag, so collaborative playlists are created
        private and then flipped via ``playlist_change_details`` (Spotify
        requires collaborative playlists to be non-public).
        """
        try:
            user = await self._sp.current_user()
            sp_playlist = await self._sp.playlist_create(
                user.id,
                name,
                public=False if collaborative else public,
                description=description,
            )
            if collaborative:
                await self._sp.playlist_change_details(sp_playlist.id, collaborative=True)
        except tk.HTTPError as exc:
            logger.error("Failed to create playlist on Spotify: %s", exc)
            raise

        logger.info("Created playlist '%s' (%s)", name, sp_playlist.id)
        return CreatedPlaylist(
            spotify_id=sp_playlist.id, name=sp_playlist.name, snapshot_id=sp_playlist.snapshot_id
        )

    async def create_playlist_with_tracks(
        self,
        name: str,
        tracks: Sequence[Any],
        description: str = "",
        public: bool = True,
    ) -> CreatedPlaylist:
        """Create a playlist and fill it with *tracks* (tekore track objects).

        The composed flow behind genre playlists, time capsules, and any
        other "build a list from discovered tracks" feature — one place
        for the create-then-add sequence and the local-track filtering.
        """
        playlist = await self.create_playlist(name=name, description=description, public=public)
        uris = [t.uri for t in tracks if t.uri]
        if uris:
            await self.add_tracks(playlist.spotify_id, uris)
        return playlist

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
        """Remove duplicate tracks from a playlist, keeping one copy of each.

        Spotify's remove-by-URI endpoint deletes *every* occurrence of a
        URI, so naive removal would destroy the kept copy too. Instead:

        1. Remove all occurrences of each duplicated URI (guarded by the
           playlist's ``snapshot_id`` so a concurrently modified playlist
           aborts rather than removing the wrong rows).
        2. Re-insert one copy of each at its position in the final,
           deduplicated ordering (ascending, so earlier inserts never
           shift later targets).

        Returns the number of duplicate occurrences removed.
        """
        try:
            # fields= makes tekore return a raw dict with just the snapshot.
            snapshot = await self._sp.playlist(playlist_id, fields="snapshot_id")
        except tk.HTTPError as exc:
            logger.error("Failed to fetch playlist %s: %s", playlist_id, exc)
            raise
        snapshot_id = snapshot["snapshot_id"] if isinstance(snapshot, dict) else None

        all_items = await self.get_playlist_tracks(playlist_id)

        original_uris = [
            item.track.uri
            for item in all_items
            if item.track is not None and item.track.uri is not None
        ]

        seen: set[str] = set()
        final_sequence: list[str] = []  # original order with 2nd+ occurrences dropped
        duplicated: set[str] = set()

        for uri in original_uris:
            if uri in seen:
                duplicated.add(uri)
            else:
                seen.add(uri)
                final_sequence.append(uri)
        removed_count = len(original_uris) - len(final_sequence)

        if not duplicated:
            logger.info("No duplicates found in playlist %s", playlist_id)
            return 0

        # Step 1: remove every copy of each duplicated URI.
        dup_list = sorted(duplicated)
        for offset in range(0, len(dup_list), _CHUNK_SIZE):
            chunk = dup_list[offset : offset + _CHUNK_SIZE]
            await self._sp.playlist_remove(playlist_id, chunk, snapshot_id)

        # Step 2: re-insert one copy of each at its final position, in
        # ascending order so every earlier element of the final sequence is
        # already in place. Consecutive positions are batched into a single
        # request (duplicates cluster in practice).
        runs: list[tuple[int, list[str]]] = []
        for position, uri in enumerate(final_sequence):
            if uri in duplicated:
                if runs and runs[-1][0] + len(runs[-1][1]) == position:
                    runs[-1][1].append(uri)
                else:
                    runs.append((position, [uri]))
        for start, uris in runs:
            for offset in range(0, len(uris), _CHUNK_SIZE):
                await self._sp.playlist_add(
                    playlist_id, uris[offset : offset + _CHUNK_SIZE], position=start + offset
                )

        logger.info(
            "Removed %d duplicate occurrence(s) of %d track(s) from playlist %s",
            removed_count,
            len(duplicated),
            playlist_id,
        )
        return removed_count

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


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def extract_isrc(track: Any) -> str | None:
    """Return a Tekore track's ISRC, the global recording identifier.

    Tekore parses ``external_ids`` into a plain dict; the attribute form
    is accepted too so any model shape works. The ISRC is the join key
    for tempo/key lookups, so this must stay the only definition.
    """
    external = getattr(track, "external_ids", None)
    if isinstance(external, dict):
        return external.get("isrc")
    return getattr(external, "isrc", None)
