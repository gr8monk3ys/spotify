"""Tempo and musical key for tracks, from sources that still serve it.

Spotify withdrew ``GET /v1/audio-features`` for apps created after
November 2024 — it answers 403 — so key and BPM have to come from
elsewhere. Every Spotify track carries an **ISRC**, the global recording
identifier, and other music databases are keyed by it:

* :class:`DeezerProvider` — ``api.deezer.com/track/isrc:{isrc}`` returns
  a BPM. No auth, fast, but no key.
* :class:`AcousticBrainzProvider` — resolves the ISRC to a MusicBrainz
  recording, then reads AcousticBrainz's analysis of it, which has both
  key and BPM. Authoritative but slow: MusicBrainz asks for one request
  per second, so a full library is a background job, not a page load.

Results are cached on disk by ISRC (checkpointed as it goes, so an
interrupted run keeps its work), and the slow walk happens once. Coverage
is partial by nature; :mod:`spotifyforge.core.curation` decides when
there is enough of it to sequence harmonically at all.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

_USER_AGENT = "SpotifyForge/1.0 (https://github.com/gr8monk3ys/spotify)"

# MusicBrainz asks unauthenticated clients for at most one request per
# second and blocks clients that ignore it.
_MUSICBRAINZ_INTERVAL = 1.05

# Write the cache to disk every this many lookups, so an interrupted run
# keeps the work it has already paid for.
_CHECKPOINT_EVERY = 50

# Pitch classes in the order Spotify used, so a key name maps to the same
# integer the Camelot table expects.
_PITCH_CLASSES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
_ENHARMONIC = {"Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#", "Bb": "A#"}


@dataclass(frozen=True)
class AudioFeature:
    """Tempo and key for one recording. ``None`` means "not known"."""

    tempo: float | None = None
    key: int | None = None  # pitch class, 0 = C
    mode: int | None = None  # 1 = major, 0 = minor

    @property
    def has_key(self) -> bool:
        return self.key is not None and self.mode is not None

    def merged_with(self, other: AudioFeature) -> AudioFeature:
        """Combine two partial readings, preferring values already set."""
        return AudioFeature(
            tempo=self.tempo if self.tempo is not None else other.tempo,
            key=self.key if self.key is not None else other.key,
            mode=self.mode if self.mode is not None else other.mode,
        )


def parse_key(key_name: str | None, scale: str | None) -> tuple[int | None, int | None]:
    """Turn AcousticBrainz's ("F", "minor") into (pitch class, mode)."""
    if not key_name:
        return (None, None)
    name = _ENHARMONIC.get(key_name.strip(), key_name.strip())
    if name not in _PITCH_CLASSES:
        return (None, None)
    mode = 1 if (scale or "").lower().startswith("major") else 0
    return (_PITCH_CLASSES.index(name), mode)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


class FeatureCache:
    """A JSON file of ISRC → feature, so the slow lookups happen once.

    Each entry records which providers have already been asked. That is
    what stops a miss being re-queried on every run, and what lets a
    later ``--deep`` pass fetch keys for tracks a quick tempo-only run
    already cached.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, dict[str, Any]] = {}
        if path.exists():
            try:
                self._data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                logger.warning("Feature cache at %s unreadable; starting empty", path)

    def __len__(self) -> int:
        return len(self._data)

    def get(self, isrc: str) -> AudioFeature | None:
        raw = self._data.get(isrc)
        if raw is None:
            return None
        return AudioFeature(tempo=raw.get("tempo"), key=raw.get("key"), mode=raw.get("mode"))

    def tried(self, isrc: str, provider: str) -> bool:
        """Has *provider* already been asked about *isrc*?"""
        return provider in (self._data.get(isrc, {}).get("sources") or [])

    def all(self) -> dict[str, AudioFeature]:
        """Every cached reading, keyed by ISRC."""
        return {
            isrc: AudioFeature(tempo=raw.get("tempo"), key=raw.get("key"), mode=raw.get("mode"))
            for isrc, raw in self._data.items()
        }

    def put(self, isrc: str, feature: AudioFeature, provider: str) -> None:
        """Merge a provider's reading in, remembering that it was asked.

        Values already cached win; a new provider only fills blanks. That
        keeps the cache stable — re-running never quietly reshuffles a
        playlist because two sources disagree by half a BPM.
        """
        entry = self._data.setdefault(isrc, {"sources": []})
        merged = AudioFeature(entry.get("tempo"), entry.get("key"), entry.get("mode")).merged_with(
            feature
        )
        entry["tempo"], entry["key"], entry["mode"] = merged.tempo, merged.key, merged.mode
        sources = entry.setdefault("sources", [])
        if provider not in sources:
            sources.append(provider)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data))
        logger.info("Wrote %d cached features to %s", len(self._data), self.path)


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


class DeezerProvider:
    """BPM by ISRC from Deezer's public API. No key, no auth, quick."""

    name = "deezer"
    concurrency = 4

    async def fetch(self, client: httpx.AsyncClient, isrc: str) -> AudioFeature:
        response = await client.get(f"https://api.deezer.com/track/isrc:{isrc}")
        if response.status_code != 200:
            return AudioFeature()
        bpm = response.json().get("bpm")
        # Deezer reports 0 when it has not analysed the track.
        return AudioFeature(tempo=float(bpm)) if bpm else AudioFeature()


class AcousticBrainzProvider:
    """Key and BPM via MusicBrainz → AcousticBrainz. Slow but complete."""

    name = "acousticbrainz"
    concurrency = 1  # MusicBrainz rate limit governs the whole chain

    async def fetch(self, client: httpx.AsyncClient, isrc: str) -> AudioFeature:
        await asyncio.sleep(_MUSICBRAINZ_INTERVAL)
        lookup = await client.get(
            f"https://musicbrainz.org/ws/2/isrc/{isrc}",
            params={"fmt": "json"},
            headers={"User-Agent": _USER_AGENT},
        )
        if lookup.status_code != 200:
            return AudioFeature()
        recordings = lookup.json().get("recordings") or []

        for recording in recordings[:2]:
            analysis = await client.get(
                f"https://acousticbrainz.org/api/v1/{recording['id']}/low-level"
            )
            if analysis.status_code != 200:
                continue
            body = analysis.json()
            key, mode = parse_key(
                body.get("tonal", {}).get("key_key"),
                body.get("tonal", {}).get("key_scale"),
            )
            tempo = body.get("rhythm", {}).get("bpm")
            return AudioFeature(tempo=float(tempo) if tempo else None, key=key, mode=mode)
        return AudioFeature()


# ---------------------------------------------------------------------------
# Enrichment
# ---------------------------------------------------------------------------


async def fetch_features(
    isrcs: Sequence[str],
    providers: Sequence[DeezerProvider | AcousticBrainzProvider],
    cache: FeatureCache,
    client: httpx.AsyncClient | None = None,
    progress: object = None,
) -> dict[str, AudioFeature]:
    """Look up *isrcs* through *providers*, filling and using *cache*.

    Providers run in the order given and their readings are merged, so a
    fast BPM-only source supplies tempo while a slower one supplies the
    key. Each provider is asked only about tracks it has not already been
    asked about, which makes repeat runs cheap and lets a later deep pass
    top up entries an earlier quick pass created.
    """
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=30, follow_redirects=True)
    try:
        for provider in providers:
            pending = [i for i in isrcs if i and not cache.tried(i, provider.name)]
            logger.info("%s: %d ISRCs to look up", provider.name, len(pending))
            semaphore = asyncio.Semaphore(provider.concurrency)
            done = {"n": 0}

            async def _one(
                isrc: str,
                provider: DeezerProvider | AcousticBrainzProvider = provider,
                semaphore: asyncio.Semaphore = semaphore,
            ) -> None:
                async with semaphore:
                    try:
                        feature = await provider.fetch(client, isrc)
                    except (httpx.HTTPError, ValueError, KeyError) as exc:
                        logger.debug("%s failed for %s: %s", provider.name, isrc, exc)
                        return
                    cache.put(isrc, feature, provider.name)
                    done["n"] += 1
                    # Checkpoint as we go. The deep provider is limited to
                    # about one track per second, so a full library is
                    # nearly an hour of work — losing all of it to an
                    # interrupted run would be the worst failure here.
                    if done["n"] % _CHECKPOINT_EVERY == 0:
                        cache.save()
                    if callable(progress):
                        progress()

            await asyncio.gather(*(_one(i) for i in pending))
    finally:
        if owns_client:
            await client.aclose()

    cache.save()
    return {i: cache.get(i) or AudioFeature() for i in isrcs if i}


# ---------------------------------------------------------------------------
# Harmonic sequencing
# ---------------------------------------------------------------------------

# Camelot wheel positions keyed by (pitch class, mode). Major = B, minor = A.
_CAMELOT: dict[tuple[int, int], tuple[int, str]] = {
    (0, 1): (8, "B"), (1, 1): (3, "B"), (2, 1): (10, "B"), (3, 1): (5, "B"),
    (4, 1): (12, "B"), (5, 1): (7, "B"), (6, 1): (2, "B"), (7, 1): (9, "B"),
    (8, 1): (4, "B"), (9, 1): (11, "B"), (10, 1): (6, "B"), (11, 1): (1, "B"),
    (0, 0): (5, "A"), (1, 0): (12, "A"), (2, 0): (7, "A"), (3, 0): (2, "A"),
    (4, 0): (9, "A"), (5, 0): (4, "A"), (6, 0): (11, "A"), (7, 0): (6, "A"),
    (8, 0): (1, "A"), (9, 0): (8, "A"), (10, 0): (3, "A"), (11, 0): (10, "A"),
}  # fmt: skip


def camelot(feature: AudioFeature) -> tuple[int, str] | None:
    """Camelot position, e.g. ``(8, "A")`` for A minor."""
    if not feature.has_key:
        return None
    return _CAMELOT[(feature.key, feature.mode)]  # type: ignore[index]


def key_distance(a: AudioFeature, b: AudioFeature) -> int:
    """0 = same key, 1 = a mix DJs consider compatible, higher = further.

    Compatible moves on the Camelot wheel are: same position, one step
    around the ring in the same mode, or the relative major/minor at the
    same number.
    """
    pa, pb = camelot(a), camelot(b)
    if pa is None or pb is None:
        return 2
    ring = min(abs(pa[0] - pb[0]), 12 - abs(pa[0] - pb[0]))
    if ring == 0:
        return 0 if pa[1] == pb[1] else 1
    if ring == 1 and pa[1] == pb[1]:
        return 1
    return 1 + ring + (pa[1] != pb[1])
