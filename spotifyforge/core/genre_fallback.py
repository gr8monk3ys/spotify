"""Genres for the artists Spotify has not tagged.

Spotify puts genres on artists, and on a real library it leaves a lot of
them blank: 753 of 2,662 liked songs here had a primary artist with no
genres at all, so more than a quarter of the library could only ever
land in a "beyond genre" playlist. MusicBrainz has community tags for
many of the same artists.

The lookup is by ISRC, not by name. An ISRC identifies the recording, so
the artist MusicBrainz credits on it is the artist of *this* song;
searching by name instead would hand "Part Time" or "Diners" the tags of
whichever act of that name MusicBrainz lists first. As a second check
the credited artist's name has to agree with Spotify's, which costs a
handful of aliases (an artist filed under their name in another script)
and removes the wrong-artist case entirely.

MusicBrainz tags are free text ("seen live", "fifth beatle"), so a tag
only becomes a genre if it is already a genre somewhere in this library
— one Spotify itself assigned to another artist. That keeps a single
vocabulary, which is what lets a newly classified song join a playlist
that already exists. On the library above this placed 424 of the 753.

Nothing is looked up while planning. :func:`fetch_artist_tags` fills a
cache on disk (``curate genres``), and :func:`apply_fallback` reads it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from media_core.names import fold

from spotifyforge.config import sidecar_path, write_json_atomic
from spotifyforge.core.audio_features import _MUSICBRAINZ_INTERVAL, _USER_AGENT, _RateLimiter
from spotifyforge.core.clock import utc_now

if TYPE_CHECKING:
    from spotifyforge.core.curation import CurationTrack

logger = logging.getLogger(__name__)

# ISRCs per recording search and artists per artist search. Both are one
# rate-limited request, so a few hundred artists cost well under a minute.
_ISRC_BATCH = 25
_ARTIST_BATCH = 40

# Recordings tried per artist. MusicBrainz knows about half of the ISRCs
# asked of it, so a second song by the same artist is worth one more slot.
_ISRCS_PER_ARTIST = 2

# Spotify gives an artist a handful of genres; more than this from a
# fallback would let one well-tagged artist reach a dozen playlists.
_MAX_GENRES = 3

# An artist MusicBrainz had nothing for is asked about again after this
# long: tags are added over time, but not so fast that every run should ask.
_RETRY_AFTER = timedelta(days=30)

# MusicBrainz answers 503 when it wants a client to slow down, even one
# keeping to a request a second. Three of thirty-one batches came back that
# way on the first real run; each is retried after a growing pause.
_RETRIES = 2
_BACKOFF_SECONDS = 3.0


def cache_path() -> Path:
    """Where fallback tags live: ``<data dir>/artist_genres.json``."""
    return sidecar_path("artist_genres.json")


class TagCache:
    """Spotify artist id -> MusicBrainz tags, most voted first, on disk.

    An artist with no tags is stored too (as an empty list), so a repeat
    run does not ask MusicBrainz about the same blanks again.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or cache_path()
        self._entries: dict[str, dict[str, Any]] = {}
        if self.path.exists():
            try:
                self._entries = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                logger.warning("Ignoring unreadable genre cache at %s", self.path)

    def __len__(self) -> int:
        return len(self._entries)

    def tags(self) -> dict[str, tuple[str, ...]]:
        """Every artist that has at least one tag."""
        return {
            artist: tuple(entry["tags"]) for artist, entry in self._entries.items() if entry["tags"]
        }

    def due(self, artist_id: str) -> bool:
        """Whether *artist_id* should be looked up (never asked, or a stale blank)."""
        entry = self._entries.get(artist_id)
        if entry is None:
            return True
        if entry["tags"]:
            return False
        return utc_now() - datetime.fromisoformat(entry["checked_at"]) > _RETRY_AFTER

    def put(self, artist_id: str, tags: Sequence[str], mbid: str | None) -> None:
        self._entries[artist_id] = {
            "tags": list(tags),
            "mbid": mbid,
            "checked_at": utc_now().isoformat(),
        }

    def save(self) -> None:
        write_json_atomic(self.path, self._entries)


def load_fallback_tags(path: Path | None = None) -> dict[str, tuple[str, ...]]:
    """Cached tags by Spotify artist id; empty when ``curate genres`` never ran."""
    return TagCache(path).tags()


@dataclass(frozen=True)
class FetchReport:
    """What one ``curate genres`` run learned."""

    artists_asked: int
    artists_tagged: int


def untagged_artists(tracks: Iterable[CurationTrack]) -> dict[str, tuple[str, list[str]]]:
    """Primary artists without genres -> (name, ISRCs of their songs)."""
    out: dict[str, tuple[str, list[str]]] = {}
    for track in tracks:
        if track.genres or not track.artist_ids or not track.isrc:
            continue
        _, isrcs = out.setdefault(track.artist_ids[0], (track.artist_names[0], []))
        if len(isrcs) < _ISRCS_PER_ARTIST:
            isrcs.append(track.isrc)
    return out


async def _search(
    client: httpx.AsyncClient, limiter: _RateLimiter, entity: str, query: str
) -> dict[str, Any]:
    """One MusicBrainz search, retried when the server asks for patience.

    Raises on any answer that is not a result: a refusal must never be
    read as "nothing known about these".
    """
    for attempt in range(_RETRIES + 1):
        await limiter.wait()
        response = await client.get(
            f"https://musicbrainz.org/ws/2/{entity}",
            params={"query": query, "fmt": "json", "limit": 100},
            headers={"User-Agent": _USER_AGENT},
        )
        if response.status_code == 503 and attempt < _RETRIES:
            await asyncio.sleep(_BACKOFF_SECONDS * (attempt + 1))
            continue
        response.raise_for_status()
        break
    body: dict[str, Any] = response.json()
    return body


async def _artists_by_isrc(
    client: httpx.AsyncClient, limiter: _RateLimiter, isrcs: Sequence[str]
) -> dict[str, tuple[str, str]]:
    """ISRC -> (MusicBrainz artist id, artist name) for one batch."""
    body = await _search(client, limiter, "recording", " OR ".join(f"isrc:{i}" for i in isrcs))
    wanted = set(isrcs)
    found: dict[str, tuple[str, str]] = {}
    for recording in body.get("recordings") or []:
        credit = recording.get("artist-credit") or []
        if not credit:
            continue
        artist = credit[0].get("artist") or {}
        for isrc in recording.get("isrcs") or []:
            if isrc in wanted and isrc not in found and artist.get("id"):
                found[isrc] = (artist["id"], artist.get("name") or "")
    return found


async def _tags_by_artist(
    client: httpx.AsyncClient, limiter: _RateLimiter, mbids: Sequence[str]
) -> dict[str, list[str]]:
    """MusicBrainz artist id -> tag names, most voted first, for one batch."""
    body = await _search(client, limiter, "artist", " OR ".join(f"arid:{m}" for m in mbids))
    out: dict[str, list[str]] = {}
    for artist in body.get("artists") or []:
        voted = [t for t in artist.get("tags") or [] if t.get("count", 0) > 0 and t.get("name")]
        voted.sort(key=lambda t: (-t["count"], t["name"]))
        out[artist["id"]] = [t["name"].lower() for t in voted]
    return out


def _why(exc: Exception) -> str:
    """A failure in a few words; httpx's own message repeats the whole query."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__


async def fetch_artist_tags(
    tracks: Sequence[CurationTrack],
    cache: TagCache,
    client: httpx.AsyncClient | None = None,
    progress: Callable[[int], None] | None = None,
) -> FetchReport:
    """Ask MusicBrainz about every untagged primary artist the cache is due for.

    The cache is saved after each stage, so an interrupted run keeps what
    it learned. A batch that fails is skipped and left due, not recorded
    as "no tags".
    """
    pending = {a: v for a, v in untagged_artists(tracks).items() if cache.due(a)}
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=60, follow_redirects=True)
    limiter = _RateLimiter(_MUSICBRAINZ_INTERVAL)
    tagged = 0
    try:
        # Stage 1: which MusicBrainz artist is behind each Spotify artist.
        owner = {isrc: artist for artist, (_, isrcs) in pending.items() for isrc in isrcs}
        isrcs = sorted(owner)
        mbid_of: dict[str, str] = {}
        answered: set[str] = set()
        for offset in range(0, len(isrcs), _ISRC_BATCH):
            batch = isrcs[offset : offset + _ISRC_BATCH]
            try:
                found = await _artists_by_isrc(client, limiter, batch)
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                logger.warning("MusicBrainz recording lookup failed (%s)", _why(exc))
                continue
            answered.update(owner[isrc] for isrc in batch)
            for isrc, (mbid, name) in found.items():
                artist = owner[isrc]
                if artist not in mbid_of and fold(name) == fold(pending[artist][0]):
                    mbid_of[artist] = mbid

        # Stage 2: that artist's tags.
        mbids = sorted(set(mbid_of.values()))
        tags_of: dict[str, list[str]] = {}
        failed: set[str] = set()
        for offset in range(0, len(mbids), _ARTIST_BATCH):
            batch = mbids[offset : offset + _ARTIST_BATCH]
            try:
                tags_of.update(await _tags_by_artist(client, limiter, batch))
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                logger.warning("MusicBrainz artist lookup failed (%s)", _why(exc))
                failed.update(batch)

        for artist in pending:
            artist_mbid = mbid_of.get(artist)
            if artist not in answered or (artist_mbid is not None and artist_mbid in failed):
                continue  # not asked, or asked and not answered: still due
            tags = tags_of.get(artist_mbid, []) if artist_mbid else []
            cache.put(artist, tags, artist_mbid)
            tagged += bool(tags)
            if progress is not None:
                progress(1)
        cache.save()
    finally:
        if owns_client:
            await client.aclose()
    return FetchReport(artists_asked=len(pending), artists_tagged=tagged)


def apply_fallback(
    tracks: Sequence[CurationTrack], tags: Mapping[str, Sequence[str]]
) -> list[CurationTrack]:
    """Give untagged tracks their artist's tags that are already genres here.

    Tracks Spotify did classify are returned unchanged: the fallback fills
    blanks, it never second-guesses a genre Spotify assigned.
    """
    if not tags:
        return list(tracks)
    vocabulary = {genre for track in tracks for genre in track.genres}
    out: list[CurationTrack] = []
    for track in tracks:
        if track.genres or not track.artist_ids:
            out.append(track)
            continue
        known = [t for t in tags.get(track.artist_ids[0], ()) if t in vocabulary]
        out.append(replace(track, genres=tuple(known[:_MAX_GENRES])) if known else track)
    return out
