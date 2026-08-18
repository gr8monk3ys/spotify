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

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
import tekore as tk

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tekore import Spotify

logger = logging.getLogger(__name__)

_SEARCH_LIMIT = 20  # playlists per genre query
_TRACKS_SAMPLED = 100  # tracks read per candidate playlist
# Candidate playlists inspected at once. Searching a dozen genres means
# a couple of hundred reads; sequentially that is over a minute of pure
# waiting for what is only a report.
_CONCURRENCY = 6


@dataclass
class Curator:
    """A Spotify user whose public playlists overlap the library."""

    user_id: str
    display_name: str
    shared_tracks: int = 0
    playlists_seen: int = 0
    example_playlist: str = ""

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

    Candidates are inspected concurrently and each playlist is read only
    once, however many genre searches surfaced it.
    """
    candidates: dict[str, Any] = {}

    for genre in genres:
        try:
            (page,) = await spotify.search(genre, types=("playlist",), limit=_SEARCH_LIMIT)
        except (tk.HTTPError, httpx.HTTPError) as exc:
            logger.warning("Playlist search failed for %r: %s", genre, exc)
            continue

        for playlist in page.items or []:
            if playlist is None or playlist.owner is None:
                continue
            owner_id = playlist.owner.id
            if not owner_id or owner_id in {me, "spotify"}:
                continue
            # Neighbouring genres surface the same playlists; reading one
            # twice costs a round trip and changes no answer.
            candidates.setdefault(playlist.id, playlist)

    shared_counts = await _count_shared_many(spotify, list(candidates), liked_track_ids)

    curators: dict[str, Curator] = {}
    for playlist_id, playlist in candidates.items():
        shared = shared_counts.get(playlist_id, 0)
        if not shared:
            continue
        owner_id = playlist.owner.id
        curator = curators.setdefault(
            owner_id,
            Curator(user_id=owner_id, display_name=playlist.owner.display_name or owner_id),
        )
        curator.playlists_seen += 1
        if shared > curator.shared_tracks:
            curator.shared_tracks = shared
            curator.example_playlist = playlist.name or ""

    ranked = sorted(
        curators.values(),
        key=lambda c: (-c.shared_tracks, -c.playlists_seen, c.user_id),
    )
    logger.info(
        "Inspected %d playlists, found %d curators with overlapping taste",
        len(candidates),
        len(ranked),
    )
    return ranked[:limit]


async def _count_shared_many(
    spotify: Spotify, playlist_ids: Sequence[str], liked: set[str]
) -> dict[str, int]:
    """Count overlap for many playlists at once, tolerating failures.

    Strangers' playlists routinely 404 or are region-restricted, so a
    failed read means "no overlap known", never an aborted report.
    """
    semaphore = asyncio.Semaphore(_CONCURRENCY)

    async def one(playlist_id: str) -> tuple[str, int]:
        async with semaphore:
            try:
                # Ask for only the track ids; the default payload carries
                # every field of 100 fully-hydrated tracks to read one.
                page = await spotify.playlist_items(
                    playlist_id, fields="items(track(id))", limit=_TRACKS_SAMPLED
                )
            except (tk.HTTPError, httpx.HTTPError) as exc:
                logger.debug("Could not read playlist %s: %s", playlist_id, exc)
                return playlist_id, 0
            return playlist_id, _count_matches(page, liked)

    return dict(await asyncio.gather(*(one(p) for p in playlist_ids)))


def _count_matches(page: Any, liked: set[str]) -> int:
    """Count liked ids in a playlist page, whether dict or model."""
    items = page.get("items") if isinstance(page, dict) else (page.items or [])
    return sum(1 for item in items or [] if _track_id(item) in liked)


def _track_id(item: Any) -> str | None:
    """The track id of a playlist item, from a dict or a tekore model."""
    if isinstance(item, dict):
        track = item.get("track")
        return track.get("id") if isinstance(track, dict) else None
    track = getattr(item, "track", None)
    return getattr(track, "id", None) if track is not None else None


def top_genres(tracks: Sequence[Any], count: int = 12) -> list[str]:
    """The genres worth searching: common enough to have a scene, rare
    enough to be a niche rather than a chart."""
    counts: Counter[str] = Counter(g for t in tracks for g in t.genres)
    # Skip the handful of catch-all labels; their playlists are dominated
    # by editorial accounts and tell us nothing about peers.
    generic = {"pop", "rock", "rap", "hip hop", "indie", "electronic", "edm", "dance"}
    ranked = [g for g, _ in counts.most_common() if g not in generic]
    return ranked[:count]
