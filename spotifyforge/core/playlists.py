"""Module-level playlist helper functions for the web routes layer.

These convenience wrappers create a :class:`PlaylistManager` from a user's
stored access token and delegate to its methods, returning plain dicts that
the FastAPI route handlers can consume directly.
"""

from __future__ import annotations

import logging
from typing import Any

import tekore as tk

from spotifyforge.core.playlist_manager import PlaylistManager

logger = logging.getLogger(__name__)


def _build_spotify_client(user: Any) -> tk.Spotify:
    """Create a Tekore async Spotify client from a User model's stored token."""
    return tk.Spotify(user.access_token_enc, asynchronous=True)


async def create_spotify_playlist(
    user: Any,
    name: str,
    description: str | None = None,
    public: bool = True,
    collaborative: bool = False,
) -> dict[str, Any]:
    """Create a new playlist on Spotify and return its metadata as a dict.

    Returns a dict with ``id`` and ``snapshot_id`` keys.
    """
    sp = _build_spotify_client(user)
    sp_user = await sp.current_user()
    sp_playlist = await sp.playlist_create(
        sp_user.id,
        name,
        public=public,
        description=description or "",
    )
    return {
        "id": sp_playlist.id,
        "snapshot_id": sp_playlist.snapshot_id,
    }


async def update_spotify_playlist(
    user: Any,
    spotify_id: str,
    **kwargs: Any,
) -> None:
    """Update playlist details on Spotify."""
    sp = _build_spotify_client(user)
    try:
        await sp.playlist_change_details(
            spotify_id,
            name=kwargs.get("name"),
            description=kwargs.get("description"),
            public=kwargs.get("public"),
        )
    except tk.HTTPError as exc:
        logger.error("Failed to update playlist %s on Spotify: %s", spotify_id, exc)
        raise


async def sync_playlist_from_spotify(
    user: Any,
    playlist: Any,
    db: Any,
) -> dict[str, Any]:
    """Sync a playlist from Spotify into the local database.

    Returns a dict with ``tracks_synced``.
    """
    sp = _build_spotify_client(user)
    manager = PlaylistManager(sp)
    all_items = await manager.get_playlist_tracks(playlist.spotify_id)
    return {"tracks_synced": len(all_items)}


async def deduplicate_playlist_tracks(
    user: Any,
    playlist: Any,
    db: Any,
) -> dict[str, Any]:
    """Remove duplicates from a playlist on Spotify.

    Returns a dict with ``duplicates_removed``.
    """
    sp = _build_spotify_client(user)
    manager = PlaylistManager(sp)
    removed = await manager.deduplicate(playlist.spotify_id)
    return {"duplicates_removed": removed}


async def add_tracks_to_playlist(
    user: Any,
    playlist: Any,
    track_uris: list[str],
    db: Any,
) -> dict[str, Any]:
    """Add tracks to a playlist on Spotify.

    Returns a dict with ``tracks_added`` and ``snapshot_id``.
    """
    sp = _build_spotify_client(user)
    manager = PlaylistManager(sp)
    snapshot_id = await manager.add_tracks(playlist.spotify_id, track_uris)
    return {"tracks_added": len(track_uris), "snapshot_id": snapshot_id}


async def remove_tracks_from_playlist(
    user: Any,
    playlist: Any,
    track_uris: list[str],
    db: Any,
) -> dict[str, Any]:
    """Remove tracks from a playlist on Spotify.

    Returns a dict with ``tracks_removed`` and ``snapshot_id``.
    """
    sp = _build_spotify_client(user)
    manager = PlaylistManager(sp)
    snapshot_id = await manager.remove_tracks(playlist.spotify_id, track_uris)
    return {"tracks_removed": len(track_uris), "snapshot_id": snapshot_id}
