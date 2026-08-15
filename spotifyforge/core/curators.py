"""Find other curators whose taste genuinely overlaps the user's.

Growth on Spotify comes from being findable by people who like the same
music, and the honest way to start is to know who those people are. This
module searches for playlists in the genres the user actually listens to,
then ranks their owners by how many of the user's own liked songs appear
in their playlists — a measured overlap, not a guess.

It is **read-only on purpose**. Following hundreds of strangers to
collect follow-backs is the artificial-engagement pattern Spotify's
platform rules prohibit, and an account flagged for it loses the reach it
was chasing. This produces a shortlist a person can act on; the deciding
and the following stay with them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import tekore as tk

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tekore import Spotify

logger = logging.getLogger(__name__)

_SEARCH_LIMIT = 20  # playlists per genre query
_TRACKS_SAMPLED = 100  # tracks read per candidate playlist


@dataclass
class Curator:
    """A Spotify user whose public playlists overlap the library."""

    user_id: str
    display_name: str
    shared_tracks: int = 0
    playlists_seen: int = 0
    genres: set[str] = field(default_factory=set)
    example_playlist: str = ""
    example_playlist_id: str = ""

    @property
    def url(self) -> str:
        return f"https://open.spotify.com/user/{self.user_id}"


async def find_curators(
    spotify: Spotify,
    genres: Sequence[str],
    liked_track_ids: set[str],
    me: str,
    limit: int = 25,
) -> list[Curator]:
    """Rank curators by how much of *liked_track_ids* their playlists hold.

    Searches each genre in *genres*, samples the playlists it finds, and
    counts genuine overlap. Spotify's own editorial account is excluded —
    it is not a peer, and it will never notice a follow.
    """
    curators: dict[str, Curator] = {}

    for genre in genres:
        try:
            (page,) = await spotify.search(genre, types=("playlist",), limit=_SEARCH_LIMIT)
        except tk.HTTPError as exc:
            logger.warning("Playlist search failed for %r: %s", genre, exc)
            continue

        for playlist in page.items or []:
            if playlist is None or playlist.owner is None:
                continue
            owner_id = playlist.owner.id
            if owner_id in {me, "spotify"} or not owner_id:
                continue

            shared = await _count_shared(spotify, playlist.id, liked_track_ids)
            if not shared:
                continue

            curator = curators.setdefault(
                owner_id,
                Curator(
                    user_id=owner_id,
                    display_name=playlist.owner.display_name or owner_id,
                ),
            )
            curator.playlists_seen += 1
            curator.genres.add(genre)
            if shared > curator.shared_tracks:
                curator.shared_tracks = shared
                curator.example_playlist = playlist.name or ""
                curator.example_playlist_id = playlist.id

    ranked = sorted(
        curators.values(),
        key=lambda c: (-c.shared_tracks, -c.playlists_seen, c.user_id),
    )
    logger.info("Found %d curators with overlapping taste", len(ranked))
    return ranked[:limit]


async def _count_shared(spotify: Spotify, playlist_id: str, liked: set[str]) -> int:
    """How many of *liked* appear in the first page of a playlist."""
    try:
        paging = await spotify.playlist_items(playlist_id, limit=_TRACKS_SAMPLED)
    except tk.HTTPError as exc:
        logger.debug("Could not read playlist %s: %s", playlist_id, exc)
        return 0
    return sum(1 for item in paging.items or [] if _track_id(item) in liked)


def _track_id(item: Any) -> str | None:
    track = getattr(item, "track", None)
    return getattr(track, "id", None) if track is not None else None


def top_genres(tracks: Sequence[Any], count: int = 12) -> list[str]:
    """The genres worth searching: common enough to have a scene, rare
    enough to be a niche rather than a chart."""
    from collections import Counter

    counts: Counter[str] = Counter(g for t in tracks for g in t.genres)
    # Skip the handful of catch-all labels; their playlists are dominated
    # by editorial accounts and tell us nothing about peers.
    generic = {"pop", "rock", "rap", "hip hop", "indie", "electronic", "edm", "dance"}
    ranked = [g for g, _ in counts.most_common() if g not in generic]
    return ranked[:count]
