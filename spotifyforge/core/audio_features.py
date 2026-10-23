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
* :class:`PreviewTempoProvider` — for what neither knows, downloads
  Deezer's 30-second preview and estimates the BPM locally
  (:mod:`spotifyforge.core.tempo`). Needs the optional ``tempo`` extra.

Results are cached on disk by ISRC (checkpointed as it goes, so an
interrupted run keeps its work), and the slow walk happens once. Coverage
is partial by nature; :mod:`spotifyforge.core.curation` decides when
there is enough of it to sequence harmonically at all.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from spotifyforge import __version__

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Sequence

logger = logging.getLogger(__name__)

# MusicBrainz requires a truthful User-Agent and blocks clients that
# misreport, so this tracks the real package version.
_USER_AGENT = f"SpotifyForge/{__version__} (https://github.com/gr8monk3ys/spotify)"

# MusicBrainz asks unauthenticated clients for at most one request per
# second and blocks clients that ignore it.
_MUSICBRAINZ_INTERVAL = 1.05

# ISRCs resolved per MusicBrainz search, and recordings analysed per
# AcousticBrainz call. Both APIs accept batches; asking one at a time
# turns a whole library from minutes into hours.
_MB_BATCH = 25
_AB_BATCH = 25

# Deezer allows 50 requests per 5 seconds and answers the 51st with HTTP
# 200 and an error body, which reads exactly like "no BPM" unless checked.
_DEEZER_INTERVAL = 0.11
_DEEZER_QUOTA_ERROR = 4
_DEEZER_ATTEMPTS = 3
_DEEZER_QUOTA_PAUSE = 5.0

# Previews are ~0.5 MB; anything far larger is not a 30-second clip.
_MAX_PREVIEW_BYTES = 5_000_000

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

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> AudioFeature:
        """Read a feature out of its cached JSON shape."""
        return cls(tempo=raw.get("tempo"), key=raw.get("key"), mode=raw.get("mode"))

    def write_into(self, raw: dict[str, Any]) -> None:
        """Write this feature into a cache entry, leaving other keys alone."""
        raw["tempo"], raw["key"], raw["mode"] = self.tempo, self.key, self.mode


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
        return None if raw is None else AudioFeature.from_raw(raw)

    def tried(self, isrc: str, provider: str) -> bool:
        """Has *provider* already been asked about *isrc*?"""
        return provider in (self._data.get(isrc, {}).get("sources") or [])

    def forget_tempo_misses(self, provider: str) -> int:
        """Let *provider* be asked again about every entry still without a tempo.

        For a provider whose earlier answers cannot be trusted as misses —
        Deezer's quota error looked exactly like "no BPM" until it was
        checked for, and runs before that check cached it as one.
        """
        forgotten = 0
        for entry in self._data.values():
            sources = entry.get("sources") or []
            if entry.get("tempo") is None and provider in sources:
                sources.remove(provider)
                forgotten += 1
        return forgotten

    def tempo_source(self, isrc: str) -> str | None:
        """The provider that supplied *isrc*'s tempo, where that was recorded."""
        source = self._data.get(isrc, {}).get("tempo_from")
        return source if isinstance(source, str) else None

    def all(self) -> dict[str, AudioFeature]:
        """Every cached reading, keyed by ISRC."""
        return {isrc: AudioFeature.from_raw(raw) for isrc, raw in self._data.items()}

    def put(self, isrc: str, feature: AudioFeature, provider: str) -> None:
        """Merge a provider's reading in, remembering that it was asked.

        Values already cached win; a new provider only fills blanks. That
        keeps the cache stable — re-running never quietly reshuffles a
        playlist because two sources disagree by half a BPM.
        """
        entry = self._data.setdefault(isrc, {"sources": []})
        if entry.get("tempo") is None and feature.tempo is not None:
            # Which source a tempo came from, so an estimated one can be
            # told apart from one a database measured.
            entry["tempo_from"] = provider
        AudioFeature.from_raw(entry).merged_with(feature).write_into(entry)
        sources = entry.setdefault("sources", [])
        if provider not in sources:
            sources.append(provider)

    def save(self) -> None:
        """Write the cache out atomically — checkpointing means many
        writes, and an interrupt must not tear one."""
        from spotifyforge.config import write_json_atomic

        write_json_atomic(self.path, self._data)
        logger.debug("Wrote %d cached features to %s", len(self._data), self.path)


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


class _RateLimiter:
    """Enforce a minimum gap between calls, counting time already spent.

    A flat ``sleep(interval)`` after every request pays the full gap even
    when the request itself took longer than the gap — which, against
    MusicBrainz, it almost always does. Waiting only for the remainder
    honours the same limit at a fraction of the wall clock.
    """

    def __init__(self, interval: float) -> None:
        self._interval = interval
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            gap = time.monotonic() - self._last
            if gap < self._interval:
                await asyncio.sleep(self._interval - gap)
            self._last = time.monotonic()


class FeatureProvider(Protocol):
    """A source of readings, asked only about ISRCs it has not been asked before."""

    name: str

    def stream(
        self, client: httpx.AsyncClient, isrcs: Sequence[str]
    ) -> AsyncIterator[tuple[str, AudioFeature]]: ...


class _DeezerQuotaError(Exception):
    """Deezer kept refusing for quota; the ISRC was not really asked."""


async def _deezer_track(
    client: httpx.AsyncClient, isrc: str, limiter: _RateLimiter
) -> dict[str, Any] | None:
    """Deezer's track payload for *isrc*, or ``None`` if it has none.

    A quota error is waited out and retried rather than returned: it
    arrives as HTTP 200, and caching it as a miss would mark a track
    Deezer does know as one it does not, for good. Before this check,
    a burst of four concurrent requests drew it on a quarter of them.
    If it persists, :class:`_DeezerQuotaError` is raised and the caller
    leaves the ISRC unrecorded, for the next run to ask again.
    """
    for attempt in range(_DEEZER_ATTEMPTS):
        await limiter.wait()
        response = await client.get(f"https://api.deezer.com/track/isrc:{isrc}")
        if response.status_code != 200:
            return None
        body = response.json()
        if not isinstance(body, dict):
            return None
        error = body.get("error")
        if not error:
            return body
        if not isinstance(error, dict) or error.get("code") != _DEEZER_QUOTA_ERROR:
            return None  # "no data": Deezer does not know this ISRC
        logger.debug("deezer quota hit for %s (attempt %d)", isrc, attempt + 1)
        await asyncio.sleep(_DEEZER_QUOTA_PAUSE)
    raise _DeezerQuotaError(isrc)


async def _yield_answered(
    jobs: list[Awaitable[tuple[str, AudioFeature | None]]],
) -> AsyncIterator[tuple[str, AudioFeature]]:
    """Yield results as they finish, dropping ones that were never answered."""
    for finished in asyncio.as_completed(jobs):
        isrc, feature = await finished
        if feature is not None:
            yield isrc, feature


class DeezerProvider:
    """BPM by ISRC from Deezer's public API. No key, no auth, quick."""

    name = "deezer"
    concurrency = 4

    def __init__(self) -> None:
        self._limiter = _RateLimiter(_DEEZER_INTERVAL)

    async def stream(
        self, client: httpx.AsyncClient, isrcs: Sequence[str]
    ) -> AsyncIterator[tuple[str, AudioFeature]]:
        semaphore = asyncio.Semaphore(self.concurrency)

        async def one(isrc: str) -> tuple[str, AudioFeature | None]:
            async with semaphore:
                try:
                    body = await _deezer_track(client, isrc, self._limiter)
                    bpm = (body or {}).get("bpm")
                    # Deezer reports 0 when it has not analysed the track.
                    return isrc, (AudioFeature(tempo=float(bpm)) if bpm else AudioFeature())
                except _DeezerQuotaError:
                    return isrc, None
                except (httpx.HTTPError, ValueError, KeyError) as exc:
                    logger.debug("deezer failed for %s: %s", isrc, exc)
                    return isrc, AudioFeature()

        async for answered in _yield_answered([one(i) for i in isrcs]):
            yield answered


class AcousticBrainzProvider:
    """Key and BPM via MusicBrainz -> AcousticBrainz, in batches.

    Both hops are batched, which is what makes a whole library viable:
    one MusicBrainz search resolves up to ``_MB_BATCH`` ISRCs at once,
    and one AcousticBrainz call returns the analysis for that many
    recordings — projected down to the three fields actually read, which
    is ~90x less data than the full analysis dump.
    """

    name = "acousticbrainz"
    # Only these three of AcousticBrainz's several hundred fields are
    # used. The API separates requested paths with ';' — a comma is
    # accepted but silently returns nothing except metadata.
    _FIELDS = "tonal.key_key;tonal.key_scale;rhythm.bpm"

    def __init__(self) -> None:
        self._limiter = _RateLimiter(_MUSICBRAINZ_INTERVAL)

    async def stream(
        self, client: httpx.AsyncClient, isrcs: Sequence[str]
    ) -> AsyncIterator[tuple[str, AudioFeature]]:
        for offset in range(0, len(isrcs), _MB_BATCH):
            batch = list(isrcs[offset : offset + _MB_BATCH])
            try:
                by_isrc = await self._resolve(client, batch)
                analyses = await self._analyse(client, set(by_isrc.values()))
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                logger.debug("acousticbrainz batch failed: %s", exc)
                by_isrc, analyses = {}, {}
            for isrc in batch:
                mbid = by_isrc.get(isrc)
                yield isrc, analyses.get(mbid or "", AudioFeature())

    async def _resolve(self, client: httpx.AsyncClient, isrcs: Sequence[str]) -> dict[str, str]:
        """Map ISRC -> MusicBrainz recording id for a batch of ISRCs."""
        await self._limiter.wait()
        response = await client.get(
            "https://musicbrainz.org/ws/2/recording",
            params={
                "query": " OR ".join(f"isrc:{i}" for i in isrcs),
                "inc": "isrcs",
                "fmt": "json",
                "limit": 100,
            },
            headers={"User-Agent": _USER_AGENT},
        )
        if response.status_code != 200:
            return {}
        wanted = set(isrcs)
        found: dict[str, str] = {}
        for recording in response.json().get("recordings") or []:
            for isrc in recording.get("isrcs") or []:
                if isrc in wanted and isrc not in found:
                    found[isrc] = recording["id"]
        return found

    async def _analyse(self, client: httpx.AsyncClient, mbids: set[str]) -> dict[str, AudioFeature]:
        """Fetch key and tempo for a set of MusicBrainz recording ids."""
        ordered = [m for m in mbids if m]
        out: dict[str, AudioFeature] = {}
        for offset in range(0, len(ordered), _AB_BATCH):
            chunk = ordered[offset : offset + _AB_BATCH]
            response = await client.get(
                "https://acousticbrainz.org/api/v1/low-level",
                params={"recording_ids": ";".join(chunk), "features": self._FIELDS},
            )
            if response.status_code != 200:
                continue
            for mbid, payload in response.json().items():
                if mbid == "mbid_mapping" or not isinstance(payload, dict):
                    continue
                # A recording maps to numbered submissions; take the first.
                body: dict[str, Any] = payload.get("0") or next(iter(payload.values()), {})
                if not isinstance(body, dict):
                    continue
                key, mode = parse_key(
                    body.get("tonal", {}).get("key_key"),
                    body.get("tonal", {}).get("key_scale"),
                )
                tempo = body.get("rhythm", {}).get("bpm")
                out[mbid] = AudioFeature(tempo=float(tempo) if tempo else None, key=key, mode=mode)
        return out


class PreviewTempoProvider:
    """BPM estimated from Deezer's 30-second preview, for tracks nothing else knows.

    Asked only about tracks still without a tempo once the databases have
    had their turn (:attr:`gaps_only`), so an estimate never displaces a
    measured value. The preview URL is signed and expires, which is why
    it is fetched fresh with the track rather than kept.

    Without the ``tempo`` extra this yields nothing at all — not even
    misses — so installing the extra later picks up every track.
    """

    name = "preview"
    gaps_only = True
    concurrency = 4

    def __init__(self) -> None:
        self._limiter = _RateLimiter(_DEEZER_INTERVAL)

    async def stream(
        self, client: httpx.AsyncClient, isrcs: Sequence[str]
    ) -> AsyncIterator[tuple[str, AudioFeature]]:
        from spotifyforge.core import tempo

        if not tempo.available():
            logger.info("tempo extra not installed; skipping preview estimates")
            return
        semaphore = asyncio.Semaphore(self.concurrency)

        async def one(isrc: str) -> tuple[str, AudioFeature | None]:
            async with semaphore:
                try:
                    body = await _deezer_track(client, isrc, self._limiter)
                    url = (body or {}).get("preview")
                    if not url:
                        return isrc, AudioFeature()
                    response = await client.get(str(url))
                    if response.status_code != 200 or len(response.content) > _MAX_PREVIEW_BYTES:
                        return isrc, AudioFeature()
                    # Decoding and the FFTs are CPU work; off the event
                    # loop, the next downloads proceed meanwhile.
                    bpm = await asyncio.to_thread(tempo.estimate_from_audio, response.content)
                except _DeezerQuotaError:
                    return isrc, None
                except (httpx.HTTPError, ValueError, KeyError) as exc:
                    logger.debug("preview failed for %s: %s", isrc, exc)
                    return isrc, AudioFeature()
                return isrc, (AudioFeature(tempo=round(bpm, 2)) if bpm else AudioFeature())

        async for answered in _yield_answered([one(i) for i in isrcs]):
            yield answered


# ---------------------------------------------------------------------------
# Enrichment
# ---------------------------------------------------------------------------


async def fetch_features(
    isrcs: Sequence[str],
    providers: Sequence[FeatureProvider],
    cache: FeatureCache,
    client: httpx.AsyncClient | None = None,
    progress: Callable[[], None] | None = None,
) -> dict[str, AudioFeature]:
    """Look up *isrcs* through *providers*, filling and using *cache*.

    Providers run in the order given and their readings are merged, so a
    fast BPM-only source supplies tempo while a slower one supplies the
    key. Each provider is asked only about tracks it has not already been
    asked about, which makes repeat runs cheap and lets a later deep pass
    top up entries an earlier quick pass created. A provider marked
    ``gaps_only`` is asked only about tracks still without a tempo.
    """
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=60, follow_redirects=True)
    try:
        for provider in providers:
            gaps_only = getattr(provider, "gaps_only", False)
            pending = [
                i
                for i in isrcs
                if i
                and not cache.tried(i, provider.name)
                and not (gaps_only and (cache.get(i) or AudioFeature()).tempo is not None)
            ]
            logger.info("%s: %d ISRCs to look up", provider.name, len(pending))
            done = 0
            try:
                async for isrc, feature in provider.stream(client, pending):
                    # Record misses as well as hits: a recording no
                    # database knows must not be re-queried forever.
                    cache.put(isrc, feature, provider.name)
                    done += 1
                    if done % _CHECKPOINT_EVERY == 0:
                        cache.save()
                    if progress is not None:
                        progress()
            finally:
                cache.save()
    finally:
        if owns_client:
            await client.aclose()

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


# ---------------------------------------------------------------------------
# Cache location and library-wide gathering
# ---------------------------------------------------------------------------


def feature_cache_path() -> Path:
    """Where the tempo/key cache lives: ``<db_path parent>/audio_features.json``.

    A sidecar file rather than the ``audio_features`` table on purpose —
    this is keyed by ISRC, a global fact about a recording, and curation
    reads the whole liked library without ever creating local ``Track``
    rows to hang a foreign key on.

    Follows ``db_path`` even under Postgres — see
    :func:`spotifyforge.config.sidecar_path`.
    """
    from spotifyforge.config import sidecar_path

    return sidecar_path("audio_features.json")


async def gather_features(
    isrcs: Sequence[str],
    deep: bool = False,
    progress: Callable[[], None] | None = None,
    estimate: bool = True,
    retry_misses: bool = False,
) -> tuple[dict[str, AudioFeature], int]:
    """Fetch and cache tempo/key for *isrcs*, returning (features, learned).

    Deezer alone supplies tempo quickly. *deep* adds the
    MusicBrainz/AcousticBrainz walk, which is the only source of musical
    key but is rate-limited to roughly one track per second. *estimate*
    then fills the tempo gaps left from previews, when the ``tempo``
    extra is installed. *retry_misses* asks Deezer again about every
    track it had no tempo for.
    """
    from spotifyforge.core import tempo

    cache = FeatureCache(feature_cache_path())
    if retry_misses:
        cache.forget_tempo_misses(DeezerProvider.name)
    wanted = sorted(set(isrcs))
    # Count recordings that actually gained data, not new cache rows: a
    # deep pass adds keys to entries a tempo pass already created, so
    # measuring the row count would report zero for a run that worked.
    before = {i: cache.get(i) for i in wanted}
    providers: list[FeatureProvider] = [DeezerProvider()]
    if deep:
        providers.append(AcousticBrainzProvider())
    if estimate and tempo.available():
        providers.append(PreviewTempoProvider())
    features = await fetch_features(wanted, providers, cache, progress=progress)
    learned = sum(1 for i in wanted if features.get(i) != before.get(i))
    return features, learned


def estimated_tempo_count(isrcs: Sequence[str]) -> int:
    """How many of *isrcs* have a tempo estimated from a preview, not measured."""
    cache = FeatureCache(feature_cache_path())
    return sum(1 for i in set(isrcs) if cache.tempo_source(i) == PreviewTempoProvider.name)


def load_cached_features() -> dict[str, AudioFeature]:
    """Every tempo/key reading gathered so far. Never hits the network."""
    return FeatureCache(feature_cache_path()).all()
