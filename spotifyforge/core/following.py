"""Follow the artists the library already says you listen to.

Following an artist is a statement about taste, and this module only
ever makes one the library already supports: an artist is a candidate
because several of your liked songs are theirs, not because following
them might be repaid. Candidates are ranked by that count and capped per
run, so a run is reviewable before it is applied.

**Following listeners is deliberately not here.** Following strangers in
bulk to collect follow-backs is the artificial-engagement pattern
Spotify's platform rules prohibit, and an account actioned for it loses
exactly the reach it was chasing. :mod:`spotifyforge.core.curators`
produces a ranked shortlist of people whose taste genuinely overlaps
yours; deciding among those is a person's job, and a short list is what
makes that possible.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tekore import Spotify

    from spotifyforge.core.curation import CurationTrack

logger = logging.getLogger(__name__)

_BATCH = 50  # Spotify max for PUT /v1/me/following
_FOLLOW_DELAY = 1.0  # one write per second, same pacing as forge

# An artist credited on a single liked song is usually a guest, not
# someone you follow. Two is the smallest number that means "again".
_MIN_LIKED = 2


@dataclass(frozen=True)
class ArtistCandidate:
    """An artist the library supports following, and by how much."""

    id: str
    name: str
    liked_tracks: int

    @property
    def url(self) -> str:
        return f"https://open.spotify.com/artist/{self.id}"


def rank_candidates(
    tracks: Sequence[CurationTrack], min_liked: int = _MIN_LIKED
) -> list[ArtistCandidate]:
    """Artists in *tracks*, most-liked first, above *min_liked* songs.

    Every credited artist counts, guests included — unlike genre
    inheritance, where a guest's tags misdescribe the song, being on
    several songs you saved is a real signal about the artist. The
    *min_liked* floor is what keeps one-off features out.
    """
    counts: dict[str, int] = {}
    names: dict[str, str] = {}
    for track in tracks:
        for artist_id, name in zip(track.artist_ids, track.artist_names, strict=False):
            counts[artist_id] = counts.get(artist_id, 0) + 1
            names.setdefault(artist_id, name)

    candidates = [
        ArtistCandidate(id=aid, name=names.get(aid, aid), liked_tracks=n)
        for aid, n in counts.items()
        if n >= min_liked
    ]
    # Name breaks ties so a run is reproducible and a dry run tells the
    # truth about what the next --apply will do.
    candidates.sort(key=lambda c: (-c.liked_tracks, c.name))
    return candidates


async def unfollowed(spotify: Spotify, artist_ids: Sequence[str]) -> list[str]:
    """Which of *artist_ids* are not followed yet, in the order given."""
    pending: list[str] = []
    for start in range(0, len(artist_ids), _BATCH):
        batch = list(artist_ids[start : start + _BATCH])
        flags = await spotify.artists_is_following(batch)
        pending.extend(aid for aid, followed in zip(batch, flags, strict=True) if not followed)
    return pending


async def follow_artists(
    spotify: Spotify,
    candidates: Sequence[ArtistCandidate],
    delay: float = _FOLLOW_DELAY,
) -> tuple[list[ArtistCandidate], list[str]]:
    """Follow *candidates* that are not already followed.

    Returns ``(followed, failed)``. Checking first is what makes a re-run
    cheap and honest: a second run reports nothing to do rather than
    re-sending every follow and claiming it did the work again.
    """
    by_id = {c.id: c for c in candidates}
    pending = await unfollowed(spotify, [c.id for c in candidates])

    followed: list[ArtistCandidate] = []
    failed: list[str] = []
    for start in range(0, len(pending), _BATCH):
        batch = pending[start : start + _BATCH]
        try:
            await spotify.artists_follow(batch)
        except Exception as exc:  # noqa: BLE001 — one batch must not strand the rest
            logger.warning("Could not follow %d artist(s): %s", len(batch), exc)
            failed.extend(batch)
            continue
        followed.extend(by_id[aid] for aid in batch)
        if delay:
            await asyncio.sleep(delay)

    logger.info(
        "Followed %d artist(s); %d already followed, %d failed",
        len(followed),
        len(candidates) - len(pending),
        len(failed),
    )
    return followed, failed
