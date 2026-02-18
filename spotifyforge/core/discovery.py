"""Discovery and curation engine for SpotifyForge.

Provides methods to explore a user's listening history, find hidden gems
from artists' catalogues, and assemble curated playlists by genre, mood,
or time period.  All public methods use Tekore's async API.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import tekore as tk

if TYPE_CHECKING:
    from tekore import Spotify

logger = logging.getLogger(__name__)


class DiscoveryEngine:
    """Music discovery and curation operations backed by the Spotify API.

    Parameters
    ----------
    spotify:
        An authenticated :class:`tekore.Spotify` client instance.
    """

    def __init__(self, spotify: Spotify) -> None:
        self._sp = spotify

    # ------------------------------------------------------------------
    # User taste profile
    # ------------------------------------------------------------------

    async def get_user_top_tracks(
        self,
        time_range: str = "medium_term",
        limit: int = 50,
    ) -> list[Any]:
        """Return the current user's top tracks.

        Parameters
        ----------
        time_range:
            One of ``"short_term"`` (approx. 4 weeks), ``"medium_term"``
            (approx. 6 months), or ``"long_term"`` (several years).
        limit:
            Maximum number of tracks to return (1--50).

        Returns
        -------
        A list of Tekore ``FullTrack`` objects.
        """
        limit = min(max(limit, 1), 50)
        try:
            paging = await self._sp.current_user_top_tracks(time_range=time_range, limit=limit)
            return list(paging.items) if paging.items else []
        except tk.HTTPError as exc:
            logger.error("Failed to fetch top tracks (range=%s): %s", time_range, exc)
            raise

    async def get_user_top_artists(
        self,
        time_range: str = "medium_term",
        limit: int = 50,
    ) -> list[Any]:
        """Return the current user's top artists.

        Parameters
        ----------
        time_range:
            One of ``"short_term"``, ``"medium_term"``, or ``"long_term"``.
        limit:
            Maximum number of artists to return (1--50).

        Returns
        -------
        A list of Tekore ``FullArtist`` objects.
        """
        limit = min(max(limit, 1), 50)
        try:
            paging = await self._sp.current_user_top_artists(time_range=time_range, limit=limit)
            return list(paging.items) if paging.items else []
        except tk.HTTPError as exc:
            logger.error("Failed to fetch top artists (range=%s): %s", time_range, exc)
            raise

    async def get_recently_played(self, limit: int = 50) -> list[Any]:
        """Return the user's recently played tracks.

        Parameters
        ----------
        limit:
            Maximum number of play history items (1--50).

        Returns
        -------
        A list of Tekore ``PlayHistory`` objects, each containing a
        ``track``, ``played_at``, and ``context``.
        """
        limit = min(max(limit, 1), 50)
        try:
            cursor_paging = await self._sp.playback_recently_played(limit=limit)
            return list(cursor_paging.items) if cursor_paging.items else []
        except tk.HTTPError as exc:
            logger.error("Failed to fetch recently played tracks: %s", exc)
            raise

    # ------------------------------------------------------------------
    # Deep cuts
    # ------------------------------------------------------------------

    async def find_deep_cuts(
        self,
        artist_id: str,
        popularity_threshold: int = 30,
    ) -> list[Any]:
        """Discover lesser-known tracks from an artist's full catalogue.

        Iterates over every album (including singles and compilations) for
        the given artist, collects all tracks, and returns those whose
        Spotify popularity score falls *below* the given threshold.

        Parameters
        ----------
        artist_id:
            The Spotify artist ID.
        popularity_threshold:
            Tracks with popularity **strictly less than** this value are
            considered "deep cuts".  Defaults to 30.

        Returns
        -------
        A list of Tekore ``FullTrack`` objects sorted by popularity
        (ascending).
        """
        try:
            # Fetch all album groups for the artist.
            albums_paging = await self._sp.artist_albums(
                artist_id,
                include_groups=["album", "single", "compilation"],
                limit=50,
            )
            albums: list[Any] = list(albums_paging.items) if albums_paging.items else []

            # Paginate through remaining album pages.
            while albums_paging.next is not None:
                albums_paging = await self._sp.next(albums_paging)
                if albums_paging is None:
                    break
                albums.extend(albums_paging.items if albums_paging.items else [])

        except tk.HTTPError as exc:
            logger.error("Failed to fetch albums for artist %s: %s", artist_id, exc)
            raise

        # Collect track IDs from every album.
        all_track_ids: list[str] = []
        for album in albums:
            try:
                tracks_paging = await self._sp.album_tracks(album.id, limit=50)
                page_tracks: list[Any] = list(tracks_paging.items) if tracks_paging.items else []

                while tracks_paging.next is not None:
                    tracks_paging = await self._sp.next(tracks_paging)
                    if tracks_paging is None:
                        break
                    page_tracks.extend(tracks_paging.items if tracks_paging.items else [])

                for t in page_tracks:
                    if t.id is not None:
                        all_track_ids.append(t.id)

            except tk.HTTPError as exc:
                logger.warning("Skipping album %s due to error: %s", album.id, exc)
                continue

        if not all_track_ids:
            return []

        # Fetch full track objects in batches of 50 (Spotify max for /tracks).
        full_tracks: list[Any] = []
        for offset in range(0, len(all_track_ids), 50):
            chunk = all_track_ids[offset : offset + 50]
            try:
                batch = await self._sp.tracks(chunk)
                full_tracks.extend(batch)
            except tk.HTTPError as exc:
                logger.warning("Failed to fetch track batch at offset %d: %s", offset, exc)
                continue

        # Filter to deep cuts and deduplicate by ID.
        seen: set[str] = set()
        deep_cuts: list[Any] = []
        for track in full_tracks:
            if track.id in seen:
                continue
            seen.add(track.id)
            if track.popularity is not None and track.popularity < popularity_threshold:
                deep_cuts.append(track)

        deep_cuts.sort(key=lambda t: t.popularity if t.popularity is not None else 0)

        logger.info(
            "Found %d deep cuts (popularity < %d) for artist %s",
            len(deep_cuts),
            popularity_threshold,
            artist_id,
        )
        return deep_cuts

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    async def search_tracks(
        self,
        query: str,
        filters: dict[str, str] | None = None,
        limit: int = 50,
    ) -> list[Any]:
        """Search for tracks with optional field filters.

        Parameters
        ----------
        query:
            Free-text search query.
        filters:
            Optional mapping of Spotify search field filters.  Supported
            keys: ``year``, ``genre``, ``artist``, ``tag`` (e.g.
            ``"new"`` or ``"hipster"``).  Values are appended to the query
            string using Spotify's ``field:value`` syntax.
        limit:
            Maximum number of results (1--50).

        Returns
        -------
        A list of Tekore ``FullTrack`` objects.
        """
        limit = min(max(limit, 1), 50)

        # Build the enriched query string.
        enriched_query = query
        if filters:
            filter_parts: list[str] = []
            for key, value in filters.items():
                if key in ("year", "genre", "artist", "tag"):
                    filter_parts.append(f"{key}:{value}")
                else:
                    logger.warning("Ignoring unsupported search filter: %s", key)
            if filter_parts:
                enriched_query = f"{query} {' '.join(filter_parts)}"

        try:
            results = await self._sp.search(enriched_query, types=("track",), limit=limit)
            tracks_paging = results[0]  # First element is the track paging
            return list(tracks_paging.items) if tracks_paging.items else []
        except tk.HTTPError as exc:
            logger.error("Track search failed for query '%s': %s", enriched_query, exc)
            raise

    # ------------------------------------------------------------------
    # Playlist builders
    # ------------------------------------------------------------------

    async def build_genre_playlist(
        self,
        genre: str,
        limit: int = 50,
    ) -> list[Any]:
        """Build a track list for a given genre via Spotify search.

        This is a convenience wrapper around :meth:`search_tracks` that
        applies the ``genre`` filter automatically.

        Parameters
        ----------
        genre:
            A Spotify genre seed string (e.g. ``"indie-rock"``,
            ``"jazz"``).
        limit:
            Maximum number of tracks (1--50).

        Returns
        -------
        A list of Tekore ``FullTrack`` objects matching the genre.
        """
        return await self.search_tracks(query=genre, filters={"genre": genre}, limit=limit)

    async def build_mood_playlist(
        self,
        valence_range: tuple[float, float],
        energy_range: tuple[float, float],
        limit: int = 50,
    ) -> list[Any]:
        """Build a track list filtered by mood (valence & energy).

        .. note::

            **Third-party integration point.**  Spotify's
            ``/recommendations`` endpoint (now deprecated for new apps)
            previously allowed filtering by ``target_valence`` and
            ``target_energy``.  For production use, integrate with a
            third-party audio analysis API such as **SoundNet** or
            **Cyanite** to obtain valence/energy scores and filter a
            pre-fetched track pool accordingly.

            The current implementation uses Spotify's recommendations
            endpoint as a best-effort fallback.  If the endpoint is
            unavailable, an empty list is returned and a warning is logged.

        Parameters
        ----------
        valence_range:
            A ``(min, max)`` tuple for valence (0.0--1.0).  Higher values
            indicate more positive/happy-sounding tracks.
        energy_range:
            A ``(min, max)`` tuple for energy (0.0--1.0).  Higher values
            indicate more energetic tracks.
        limit:
            Maximum number of tracks (1--100).

        Returns
        -------
        A list of Tekore ``FullTrack`` objects, or an empty list if the
        recommendations endpoint is unavailable.
        """
        limit = min(max(limit, 1), 100)
        target_valence = (valence_range[0] + valence_range[1]) / 2.0
        target_energy = (energy_range[0] + energy_range[1]) / 2.0

        try:
            # Attempt to use the recommendations endpoint.
            # A seed genre is required; we use "pop" as a neutral default.
            # Callers building a production integration should replace this
            # with a SoundNet/Cyanite-backed approach that filters from the
            # user's library or a curated track pool.
            recs = await self._sp.recommendations(
                genre_seeds=["pop"],
                limit=limit,
                target_valence=target_valence,
                target_energy=target_energy,
                min_valence=valence_range[0],
                max_valence=valence_range[1],
                min_energy=energy_range[0],
                max_energy=energy_range[1],
            )
            tracks = list(recs.tracks) if recs.tracks else []
            logger.info(
                "Built mood playlist: valence=%.2f--%.2f, energy=%.2f--%.2f -> %d tracks",
                valence_range[0],
                valence_range[1],
                energy_range[0],
                energy_range[1],
                len(tracks),
            )
            return tracks

        except tk.HTTPError as exc:
            logger.warning(
                "Recommendations endpoint unavailable (mood playlist): %s. "
                "Integrate SoundNet or Cyanite for robust mood filtering.",
                exc,
            )
            return []

    async def build_time_capsule(
        self,
        time_range: str = "short_term",
    ) -> list[Any]:
        """Generate a 'time capsule' playlist from the user's current top tracks.

        Captures a snapshot of the user's most-listened tracks for the
        given time range.  Useful for creating periodic archives (e.g. a
        monthly "Discover Weekly" style backup).

        Parameters
        ----------
        time_range:
            One of ``"short_term"`` (4 weeks), ``"medium_term"``
            (6 months), or ``"long_term"`` (years).

        Returns
        -------
        A list of Tekore ``FullTrack`` objects representing the user's
        current top tracks.
        """
        tracks = await self.get_user_top_tracks(time_range=time_range, limit=50)
        logger.info("Built time capsule (%s): %d tracks", time_range, len(tracks))
        return tracks


# ---------------------------------------------------------------------------
# Module-level convenience functions (used by the web routes layer)
# ---------------------------------------------------------------------------


def _build_spotify_client(user: Any) -> Spotify:
    """Create a Tekore async Spotify client from a User model's stored token."""
    return tk.Spotify(user.access_token_enc, asynchronous=True)


def _track_to_dict(track: Any) -> dict[str, Any]:
    """Convert a Tekore FullTrack to a plain dict for API responses."""
    artist_names = [a.name for a in track.artists] if track.artists else []
    return {
        "id": 0,
        "spotify_id": track.id or "",
        "name": track.name or "",
        "artist_names": artist_names,
        "album_name": track.album.name if track.album else None,
        "album_id": track.album.id if track.album else None,
        "duration_ms": track.duration_ms or 0,
        "popularity": track.popularity,
        "isrc": None,
        "cached_at": __import__("datetime")
        .datetime.now(__import__("datetime").timezone.utc)
        .isoformat(),
    }


def _artist_to_dict(artist: Any) -> dict[str, Any]:
    """Convert a Tekore FullArtist to a plain dict for API responses."""
    return {
        "id": artist.id or "",
        "name": artist.name or "",
        "genres": list(artist.genres) if artist.genres else [],
        "popularity": artist.popularity,
        "followers": artist.followers.total if artist.followers else 0,
    }


async def get_top_tracks(
    user: Any,
    time_range: str = "medium_term",
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Module-level convenience for fetching a user's top tracks."""
    sp = _build_spotify_client(user)
    engine = DiscoveryEngine(sp)
    tracks = await engine.get_user_top_tracks(time_range=time_range, limit=limit)
    return [_track_to_dict(t) for t in tracks]


async def get_top_artists(
    user: Any,
    time_range: str = "medium_term",
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Module-level convenience for fetching a user's top artists."""
    sp = _build_spotify_client(user)
    engine = DiscoveryEngine(sp)
    artists = await engine.get_user_top_artists(time_range=time_range, limit=limit)
    return [_artist_to_dict(a) for a in artists]


async def get_deep_cuts(
    user: Any,
    artist_id: str,
    threshold: int = 30,
) -> list[dict[str, Any]]:
    """Module-level convenience for finding an artist's deep cuts."""
    sp = _build_spotify_client(user)
    engine = DiscoveryEngine(sp)
    tracks = await engine.find_deep_cuts(artist_id=artist_id, popularity_threshold=threshold)
    return [_track_to_dict(t) for t in tracks]


async def create_genre_based_playlist(
    user: Any,
    genre: str,
    limit: int = 30,
    playlist_name: str | None = None,
    db: Any = None,
) -> Any:
    """Module-level convenience for creating a genre-based playlist.

    Discovers tracks via the DiscoveryEngine, creates a Spotify playlist,
    and adds the tracks to it.  Returns the local Playlist DB row.
    """
    from spotifyforge.core.playlist_manager import PlaylistManager

    sp = _build_spotify_client(user)
    engine = DiscoveryEngine(sp)
    tracks = await engine.build_genre_playlist(genre=genre, limit=limit)

    name = playlist_name or f"SpotifyForge: {genre.title()}"
    manager = PlaylistManager(sp)
    db_playlist = await manager.create_playlist(name=name, description=f"Genre playlist: {genre}")

    if tracks:
        uris = [t.uri for t in tracks if t.uri]
        if uris:
            await manager.add_tracks(db_playlist.spotify_id, uris)

    return db_playlist


async def create_time_capsule_playlist(
    user: Any,
    year: int | None = None,
    month: int | None = None,
    playlist_name: str | None = None,
    db: Any = None,
) -> Any:
    """Module-level convenience for creating a time-capsule playlist.

    Fetches the user's top tracks and creates a Spotify playlist with them.
    Returns the local Playlist DB row.
    """
    from spotifyforge.core.playlist_manager import PlaylistManager

    sp = _build_spotify_client(user)
    engine = DiscoveryEngine(sp)
    tracks = await engine.build_time_capsule(time_range="long_term")

    name = playlist_name or "SpotifyForge: Time Capsule"
    if year:
        suffix = f"-{month:02d}" if month else ""
        name = playlist_name or f"SpotifyForge: Time Capsule ({year}{suffix})"

    manager = PlaylistManager(sp)
    db_playlist = await manager.create_playlist(name=name, description="Time capsule playlist")

    if tracks:
        uris = [t.uri for t in tracks if t.uri]
        if uris:
            await manager.add_tracks(db_playlist.spotify_id, uris)

    return db_playlist
