"""Bulk playlist curation from the user's liked songs.

Turns a Spotify library into a catalogue of niche playlists:

1. :func:`plan_catalogue` reads every saved track, batch-fetches artist
   genres (Spotify's genre taxonomy lives on artists, not tracks), and
   collapses duplicate recordings of the same song.
2. :func:`cluster_library` groups tracks by genre — by default a track
   joins every genre it carries — splitting oversized genres by decade
   and gathering genre-less tracks into "beyond genre" playlists.
3. :func:`order_for_flow` sequences each playlist: a popularity arc that
   opens familiar, descends into the deep cuts and resurfaces, with
   same-artist runs pulled apart.
4. :func:`forge_next` creates the next batch on Spotify, skipping
   playlists that already exist so runs are resumable, and
   :func:`reflow` re-sequences ones already created.

Spotify withdrew its own audio-features endpoint, so tempo and key come
from :mod:`spotifyforge.core.audio_features` (Deezer and AcousticBrainz,
looked up by ISRC). When enough of a playlist has been analysed the
ordering switches to Camelot-wheel harmonic mixing; otherwise it falls
back to the popularity arc, which needs no outside data.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING, Any

import httpx
import tekore as tk

from spotifyforge.core.audio_features import AudioFeature, key_distance
from spotifyforge.core.playlist_manager import extract_isrc

if TYPE_CHECKING:
    from collections.abc import Iterable

    from tekore import Spotify

    from spotifyforge.core.playlist_manager import PlaylistManager
    from spotifyforge.models.models import Playlist

logger = logging.getLogger(__name__)

_ARTIST_BATCH = 50  # Spotify max for GET /v1/artists
_LIKED_PAGE = 50  # Spotify max for GET /v1/me/tracks
_REPLACE_CHUNK = 100  # Spotify max URIs for PUT /v1/playlists/{id}/tracks

# Concurrent API calls during library reads. Bounded so a large library
# does not open hundreds of sockets or trip Spotify's rate limiter.
_READ_CONCURRENCY = 5
# Transient timeouts are common across a few hundred requests; retry
# before giving up, because a lost page silently corrupts the catalogue.
_READ_ATTEMPTS = 3
_READ_BACKOFF = 1.0

# One creation per _FORGE_DELAY seconds keeps bulk runs well inside
# Spotify's rate limits (each creation is 1 create + ~1 add call).
_FORGE_DELAY = 1.0

# Harmonic ordering only beats the popularity arc when most of the
# playlist is actually analysed; below this it would chain a handful of
# known keys and scatter the rest.
_HARMONIC_MIN_COVERAGE = 0.5
# BPM difference treated as one step of "further away".
_TEMPO_BUCKET = 6.0
# Cost charged when either side has no tempo — worse than a close match,
# better than a jarring one, so unanalysed tracks fill gaps naturally.
_UNKNOWN_TEMPO_GAP = 4


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class CurationTrack:
    """A liked track reduced to the fields curation decisions need."""

    id: str
    uri: str
    name: str
    artist_ids: tuple[str, ...]
    artist_names: tuple[str, ...]
    release_year: int | None
    popularity: int
    isrc: str | None = None  # global recording id; the key for tempo/key lookups
    genres: tuple[str, ...] = ()


@dataclass
class PlaylistSpec:
    """A planned playlist: title, description, and ordered tracks."""

    title: str
    description: str
    genre: str | None  # None = no genre Spotify would name
    decade: int | None
    tracks: list[CurationTrack] = field(default_factory=list)
    # How the tracks were sequenced. Coverage is judged per playlist, so
    # --harmonic can apply to some and not others; without this the flag
    # would have no visible effect to check.
    ordering: str = "arc"

    @property
    def genre_label(self) -> str:
        """Genre for display, naming the unclassified case explicitly."""
        return self.genre or "unclassified"


@dataclass(frozen=True, kw_only=True)
class CurationOptions:
    """Clustering knobs, shared by every caller so previews stay honest."""

    min_size: int = 12
    max_size: int = 60
    max_tracks: int | None = None
    exclusive: bool = False


@dataclass
class CurationPlan:
    """The catalogue a library would produce, plus how it got there."""

    liked_count: int
    unique_count: int
    specs: list[PlaylistSpec]

    @property
    def collapsed_count(self) -> int:
        """Duplicate recordings removed before clustering."""
        return self.liked_count - self.unique_count

    @property
    def placed_count(self) -> int:
        """Distinct songs that landed in at least one playlist."""
        return len({t.id for s in self.specs for t in s.tracks})

    @property
    def entry_count(self) -> int:
        """Total playlist slots (a song can belong to several genres)."""
        return sum(len(s.tracks) for s in self.specs)

    @property
    def harmonic_count(self) -> int:
        """Playlists with enough key data to be sequenced harmonically."""
        return sum(1 for s in self.specs if s.ordering == "harmonic")


# ---------------------------------------------------------------------------
# Library fetching / enrichment
# ---------------------------------------------------------------------------


class CurationEngine:
    """Reads the user's library and prepares it for clustering."""

    def __init__(self, spotify: Spotify) -> None:
        self._sp = spotify

    async def fetch_liked(self, max_tracks: int | None = None) -> list[CurationTrack]:
        """Return every saved ("liked") track, in library order.

        The first page reports the library total, so the remaining pages
        are fetched concurrently instead of one round trip at a time.
        When *max_tracks* is set the walk stays serial — the whole point
        of that argument is to avoid fetching the rest.
        """
        first = await self._sp.saved_tracks(limit=_LIKED_PAGE)
        out = _saved_page_to_tracks(first)

        if max_tracks is not None:
            paging = first
            while len(out) < max_tracks and paging.next is not None:
                paging = await self._sp.next(paging)
                if paging is None:
                    break
                out.extend(_saved_page_to_tracks(paging))
            return out[:max_tracks]

        offsets = list(range(_LIKED_PAGE, first.total, _LIKED_PAGE))
        for page in await _gather_bounded(
            [partial(self._sp.saved_tracks, limit=_LIKED_PAGE, offset=o) for o in offsets]
        ):
            if page is not None:
                out.extend(_saved_page_to_tracks(page))

        logger.info("Fetched %d liked tracks", len(out))
        return out

    async def enrich_genres(self, tracks: list[CurationTrack]) -> list[CurationTrack]:
        """Fill each track's ``genres`` from its artists (batched, 50/call)."""
        artist_ids = sorted({aid for t in tracks for aid in t.artist_ids})
        batches = [
            artist_ids[offset : offset + _ARTIST_BATCH]
            for offset in range(0, len(artist_ids), _ARTIST_BATCH)
        ]

        genres_by_artist: dict[str, tuple[str, ...]] = {}
        for artists in await _gather_bounded([partial(self._sp.artists, b) for b in batches]):
            for artist in artists or ():
                genres_by_artist[artist.id] = tuple(artist.genres or ())

        enriched = [
            replace(
                t,
                genres=_ordered_unique(
                    g for aid in t.artist_ids for g in genres_by_artist.get(aid, ())
                ),
            )
            for t in tracks
        ]
        logger.info(
            "Enriched %d tracks from %d artists (%d without genres)",
            len(enriched),
            len(artist_ids),
            sum(1 for t in enriched if not t.genres),
        )
        return enriched


async def _gather_bounded(factories: list[Any]) -> list[Any]:
    """Await *factories* concurrently, at most ``_READ_CONCURRENCY`` at once.

    Each item is a zero-argument callable returning a fresh coroutine, so
    a failed call can be retried — a coroutine cannot be awaited twice.

    Failures are retried with backoff and then re-raised rather than
    skipped. A dropped page of liked songs would silently compute the
    catalogue from an incomplete library, and reflow would then *remove*
    the missing tracks from playlists; failing loudly is the safe choice.
    """
    if not factories:
        return []
    semaphore = asyncio.Semaphore(_READ_CONCURRENCY)

    async def _run(make: Any) -> Any:
        async with semaphore:
            for attempt in range(_READ_ATTEMPTS):
                try:
                    return await make()
                except (tk.HTTPError, httpx.HTTPError) as exc:
                    if attempt == _READ_ATTEMPTS - 1:
                        logger.error("Library read failed after %d tries: %s", attempt + 1, exc)
                        raise
                    logger.warning("Library read failed (%s); retrying", exc)
                    await asyncio.sleep(_READ_BACKOFF * (attempt + 1))
            return None

    return list(await asyncio.gather(*(_run(f) for f in factories)))


def _ordered_unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _saved_page_to_tracks(paging: Any) -> list[CurationTrack]:
    return [
        _to_curation_track(item.track)
        for item in paging.items or []
        if item.track is not None and item.track.id is not None
    ]


def _to_curation_track(track: Any) -> CurationTrack:
    release_year: int | None = None
    if track.album is not None and track.album.release_date:
        try:
            release_year = int(str(track.album.release_date)[:4])
        except ValueError:
            release_year = None
    return CurationTrack(
        id=track.id,
        uri=track.uri or f"spotify:track:{track.id}",
        name=track.name or "",
        artist_ids=tuple(a.id for a in track.artists or [] if a.id),
        artist_names=tuple(a.name for a in track.artists or [] if a.name),
        release_year=release_year,
        popularity=track.popularity if track.popularity is not None else 0,
        isrc=extract_isrc(track),
    )


# ---------------------------------------------------------------------------
# Version de-duplication
# ---------------------------------------------------------------------------

# Trailing edition markers Spotify appends after " - ": "Remastered 2011",
# "Remasterizado 2007", "Radio Edit", "Live at ...", "Single Version".
# Stems are open-ended (``remaster\w*``) so inflected and non-English
# forms — remastered, remasterizado, versión — all match.
_EDITION_WORDS = (
    r"remaster\w*|mix|edits?|versi[oó]n|version|vers[aã]o|"
    r"mono|stereo|live|en vivo|ao vivo|directo|demo|take|single|"
    r"deluxe|acoustic|ac[uú]stic\w*|instrumental|radio|bonus|reissue"
)
_EDITION_SUFFIX = re.compile(rf"\s+-\s+.*\b({_EDITION_WORDS})\b.*$", re.IGNORECASE)
# Only parentheticals that *are* edition markers — "(Remastered)" goes,
# "(Reprise)" and "(continued)" stay, because those are distinct songs.
_EDITION_PARENTHETICAL = re.compile(
    rf"\s*[\(\[][^\)\]]*\b({_EDITION_WORDS})\b[^\)\]]*[\)\]]\s*$", re.IGNORECASE
)
# ``\w`` is Unicode-aware, so CJK titles survive normalisation. An
# ASCII-only class would flatten every Japanese title to "" and collapse
# an artist's whole catalogue into one entry.
_NON_WORD = re.compile(r"\W+")


def song_key(name: str, artist_name: str) -> tuple[str, str]:
    """Identity of the *song* behind a recording: (title, primary artist).

    Strips remaster/edit/live markers so "Bocanada" and "Bocanada -
    Remasterizado 2007" collapse to one entry, while leaving genuinely
    distinct variants ("(Reprise)", "(continued)") alone.
    """
    title = _EDITION_SUFFIX.sub("", name)
    title = _EDITION_PARENTHETICAL.sub("", title)
    normalised = _NON_WORD.sub("", title.lower())
    return (normalised or name.lower(), _NON_WORD.sub("", artist_name.lower()))


def _track_song_key(track: CurationTrack) -> tuple[str, str]:
    return song_key(track.name, track.artist_names[0] if track.artist_names else "")


def dedupe_versions(tracks: list[CurationTrack]) -> list[CurationTrack]:
    """Keep one recording per song, preferring the most popular version.

    A library accumulates the same song several times — album cut,
    remaster, single edit — each with its own track ID. Spotify's own
    de-duplication cannot see these as duplicates, and a playlist that
    repeats a song does not flow.
    """
    best: dict[tuple[str, str], int] = {}
    for index, track in enumerate(tracks):
        key = _track_song_key(track)
        if key not in best or track.popularity > tracks[best[key]].popularity:
            best[key] = index
    if len(best) < len(tracks):
        logger.info("Collapsed %d duplicate recordings", len(tracks) - len(best))
    return [tracks[i] for i in sorted(best.values())]


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------


def cluster_library(
    tracks: list[CurationTrack],
    min_size: int = 12,
    max_size: int = 60,
    exclusive: bool = False,
    include_unclassified: bool = True,
    features: dict[str, AudioFeature] | None = None,
) -> list[PlaylistSpec]:
    """Group tracks into niche playlist specs.

    A genre is *viable* if at least *min_size* of the given tracks carry
    it; genres above *max_size* are split by decade.

    By default a track joins **every** viable genre it carries: a song
    really is both psychedelic rock and neo-psychedelic, and that overlap
    is what turns a few dozen playlists into a few hundred. With
    *exclusive* it joins only its rarest viable genre, which yields
    fewer but sharper-edged playlists.

    With *include_unclassified*, tracks that no viable genre claims —
    Spotify tags no genre at all on plenty of small artists — are
    gathered into per-decade playlists rather than discarded. On a niche
    account those are the most on-brand tracks in the library.

    Callers are expected to have applied :func:`dedupe_versions` first;
    :func:`plan_catalogue` does.
    """
    genre_counts: Counter[str] = Counter(g for t in tracks for g in t.genres)
    viable = {g for g, count in genre_counts.items() if count >= min_size}

    by_genre: dict[str, list[CurationTrack]] = {}
    for t in tracks:
        candidates = [g for g in t.genres if g in viable]
        if not candidates:
            continue
        if exclusive:
            candidates = [min(candidates, key=lambda g: (genre_counts[g], g))]
        for genre in candidates:
            by_genre.setdefault(genre, []).append(t)

    specs: list[PlaylistSpec] = []
    for genre, members in sorted(by_genre.items()):
        if len(members) < min_size:
            continue
        if len(members) <= max_size:
            specs.append(_make_spec(genre, None, members, features))
            continue
        for decade, decade_members in _split_by_decade(members):
            if len(decade_members) >= min_size:
                specs.append(_make_spec(genre, decade, decade_members, features))

    if include_unclassified:
        placed = {t.id for s in specs for t in s.tracks}
        orphans = [t for t in tracks if t.id not in placed]
        for decade, members in _split_by_decade(orphans):
            if len(members) >= min_size:
                specs.append(_make_spec(None, decade, members[:max_size], features))

    # Most niche first: rarer genres make better calling cards.
    specs.sort(key=lambda s: (len(s.tracks), s.title))
    return specs


def _split_by_decade(
    tracks: list[CurationTrack],
) -> list[tuple[int | None, list[CurationTrack]]]:
    """Partition *tracks* by release decade, folding undated tracks in.

    Undated tracks join the largest decade rather than forming a bucket
    of their own, so a playlist is never titled by a year nobody knows.
    """
    by_decade: dict[int, list[CurationTrack]] = {}
    undated: list[CurationTrack] = []
    for t in tracks:
        if t.release_year is None:
            undated.append(t)
        else:
            by_decade.setdefault(t.release_year // 10 * 10, []).append(t)

    if not by_decade:
        return [(None, undated)] if undated else []
    if undated:
        by_decade[max(by_decade, key=lambda d: len(by_decade[d]))].extend(undated)
    return sorted(by_decade.items())


# ---------------------------------------------------------------------------
# Flow ordering
# ---------------------------------------------------------------------------


def _shares_artist(a: CurationTrack, b: CurationTrack) -> bool:
    return bool(set(a.artist_ids) & set(b.artist_ids))


def order_for_flow(
    tracks: list[CurationTrack],
    features: dict[str, AudioFeature] | None = None,
) -> list[CurationTrack]:
    """Sequence tracks so consecutive ones flow into each other.

    Without tempo/key data, shapes a popularity arc — most familiar
    track first, descending into the deep cuts, resurfacing at the close
    — then pulls same-artist runs apart.

    With *features* (see :mod:`spotifyforge.core.audio_features`), chains
    tracks by harmonic compatibility and tempo proximity instead,
    starting from the most popular track. Coverage from those sources is
    partial, so this is used only when enough of the playlist is
    analysed; a track with no analysis still takes part, it just carries
    no key preference.
    """
    return order_with_mode(tracks, features)[0]


def order_with_mode(
    tracks: list[CurationTrack],
    features: dict[str, AudioFeature] | None = None,
) -> tuple[list[CurationTrack], str]:
    """Like :func:`order_for_flow`, but also report which mode was used."""
    if len(tracks) <= 2:
        return list(tracks), "arc"
    if features and _harmonic_coverage(tracks, features) >= _HARMONIC_MIN_COVERAGE:
        return _order_harmonic(tracks, features), "harmonic"
    return _space_artists(_popularity_arc(tracks)), "arc"


def _harmonic_coverage(tracks: list[CurationTrack], features: dict[str, AudioFeature]) -> float:
    """Fraction of *tracks* with a known musical key."""
    known = sum(
        1 for t in tracks if t.isrc and (f := features.get(t.isrc)) is not None and f.has_key
    )
    return known / len(tracks)


def _order_harmonic(
    tracks: list[CurationTrack], features: dict[str, AudioFeature]
) -> list[CurationTrack]:
    """Chain tracks by key compatibility, then tempo, then popularity.

    Artist repetition still outranks harmony: a perfect key match by the
    same artist twice in a row reads as an accident, not a mix.
    """
    empty = AudioFeature()

    def feature_of(track: CurationTrack) -> AudioFeature:
        return (features.get(track.isrc) or empty) if track.isrc else empty

    remaining = sorted(tracks, key=lambda t: (-t.popularity, t.id))
    chain = [remaining.pop(0)]

    while remaining:
        previous = chain[-1]
        previous_feature = feature_of(previous)

        def cost(
            candidate: CurationTrack,
            previous: CurationTrack = previous,
            pf: AudioFeature = previous_feature,
        ) -> tuple[bool, int, int, int, str]:
            feature = feature_of(candidate)
            if feature.tempo is not None and pf.tempo is not None:
                # Bucket the tempo gap so near-equal tempos tie and fall
                # through to popularity rather than splitting hairs.
                tempo_gap = round(abs(feature.tempo - pf.tempo) / _TEMPO_BUCKET)
            else:
                tempo_gap = _UNKNOWN_TEMPO_GAP
            return (
                _shares_artist(candidate, previous),
                key_distance(pf, feature),
                tempo_gap,
                -candidate.popularity,
                candidate.id,
            )

        nxt = min(remaining, key=cost)
        remaining.remove(nxt)
        chain.append(nxt)
    return chain


def _popularity_arc(tracks: list[CurationTrack]) -> list[CurationTrack]:
    """Most popular first, then a descent into obscurity that resurfaces."""
    ranked = sorted(tracks, key=lambda t: (-t.popularity, t.id))
    descent = ranked[0::2]  # 1st, 3rd, 5th most popular ... down
    ascent = ranked[1::2][::-1]  # deepest cuts in the middle, rising back out
    return descent + ascent


def _primary_artist(track: CurationTrack) -> str:
    return track.artist_ids[0] if track.artist_ids else ""


def _space_artists(tracks: list[CurationTrack]) -> list[CurationTrack]:
    """Reorder so an artist never plays twice in a row, if that is possible.

    Among the tracks that do not share an artist with the one just
    placed, take the one whose artist has the most tracks still waiting,
    breaking ties by arc position. Always spending the scarce artists
    first is what strands a dominant artist's last few tracks in a run at
    the end; draining the busiest artist first avoids that, and reaches
    zero repeats whenever an arrangement without them exists.
    """
    remaining = list(tracks)
    left: Counter[str] = Counter(_primary_artist(t) for t in remaining)
    out: list[CurationTrack] = []

    while remaining:
        index = 0
        if out:
            candidates = [i for i, t in enumerate(remaining) if not _shares_artist(t, out[-1])]
            if candidates:
                index = max(candidates, key=lambda i: (left[_primary_artist(remaining[i])], -i))
        chosen = remaining.pop(index)
        left[_primary_artist(chosen)] -= 1
        out.append(chosen)
    return out


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------

_TITLE_TEMPLATES = [
    "{genre} // late transmissions",
    "strictly {genre}",
    "{genre} for empty rooms",
    "the {genre} index",
    "{genre}, annotated",
    "deep {genre} cuts",
    "{genre} after hours",
    "{a} {genre} field guide",
]
_UNCLASSIFIED_TITLE = "beyond genre"

# The description is the only text besides the name that Spotify's search
# indexes, and artist names are what searchers actually type — so every
# description leads with the playlist's own biggest names. No track counts
# (they go stale as the library grows) and no tool credit (an account
# whose every playlist names its bot reads as a bot).
_DESCRIPTION_TEMPLATES = [
    "{artists}, and the deeper cuts between them. {genre} that plays start to finish.",
    "a long sit with {genre}: {artists}, more.",
    "{genre} with the skips already taken out. {artists} inside.",
    "one mood, held all the way through: {genre} around {artists}.",
    "the {genre} shelf, filed with care. {artists}.",
    "{genre} sequenced so each song hands off to the next. {artists}, company.",
    "for when only {genre} will do. {artists}, and friends.",
    "{artists}. {genre}, end to end.",
]
_UNCLASSIFIED_DESCRIPTION = "songs spotify never tagged with a genre. {artists}, other strays."
_DESCRIPTION_MAX = 300  # Spotify's documented limit for playlist descriptions


def _lead_artists(tracks: list[CurationTrack], count: int) -> list[str]:
    """The playlist's most recognisable artist names, most popular first."""
    names: list[str] = []
    for track in sorted(tracks, key=lambda t: -t.popularity):
        name = track.artist_names[0] if track.artist_names else ""
        if name and name not in names:
            names.append(name)
        if len(names) == count:
            break
    return names


def _join_names(names: list[str]) -> str:
    if len(names) > 1:
        return ", ".join(names[:-1]) + " and " + names[-1]
    return names[0] if names else ""


def _describe(
    genre: str | None, decade: int | None, tracks: list[CurationTrack], ordering: str
) -> str:
    """A human-sounding description, deterministic per genre.

    Phrasing is chosen by hashing the genre (like cover palettes), so a
    genre keeps its voice across runs and neighbouring playlists don't
    all read alike. Tries three artist names, then fewer if the result
    would overrun Spotify's length limit.
    """
    for count in (3, 2, 1, 0):
        artists = _join_names(_lead_artists(tracks, count))
        if not artists:
            text = (
                "songs spotify never tagged with a genre."
                if genre is None
                else f"{genre}, start to finish."
            )
        elif genre is None:
            text = _UNCLASSIFIED_DESCRIPTION.format(artists=artists)
        else:
            index = hashlib.sha256(genre.encode()).digest()[0] % len(_DESCRIPTION_TEMPLATES)
            text = _DESCRIPTION_TEMPLATES[index].format(genre=genre, artists=artists)
        if decade:
            text += f" all from the {decade}s."
        if ordering == "harmonic":
            text += " mixed by key, like a dj set."
        if len(text) <= _DESCRIPTION_MAX:
            return text
    return text[:_DESCRIPTION_MAX]


def _make_spec(
    genre: str | None,
    decade: int | None,
    members: list[CurationTrack],
    features: dict[str, AudioFeature] | None = None,
) -> PlaylistSpec:
    if genre is None:
        title = _UNCLASSIFIED_TITLE
    else:
        # Deterministic template choice so re-runs produce the same names.
        template = _TITLE_TEMPLATES[sum(ord(c) for c in genre) % len(_TITLE_TEMPLATES)]
        article = "an" if genre[:1].lower() in "aeiou" else "a"
        title = template.format(genre=genre, a=article)
    if decade:
        title = f"{title} ('{decade % 100:02d}s)"
    ordered, ordering = order_with_mode(members, features)
    return PlaylistSpec(
        title=title,
        description=_describe(genre, decade, ordered, ordering),
        genre=genre,
        decade=decade,
        tracks=ordered,
        ordering=ordering,
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def plan_catalogue(
    spotify: Spotify,
    opts: CurationOptions,
    features: dict[str, AudioFeature] | None = None,
) -> CurationPlan:
    """Read the library and return the catalogue it would produce.

    Pure read — nothing is created. ``curate plan`` shows this and
    ``curate forge`` acts on it, so both see the same clusters. Pass
    *features* (from :func:`load_features`) to sequence harmonically.
    """
    engine = CurationEngine(spotify)
    liked = await engine.enrich_genres(await engine.fetch_liked(max_tracks=opts.max_tracks))
    unique = dedupe_versions(liked)
    specs = cluster_library(
        unique,
        min_size=opts.min_size,
        max_size=opts.max_size,
        exclusive=opts.exclusive,
        features=features,
    )
    return CurationPlan(liked_count=len(liked), unique_count=len(unique), specs=specs)


async def forge_next(
    manager: PlaylistManager,
    owner_id: int,
    specs: list[PlaylistSpec],
    limit: int,
    public: bool = True,
    delay: float = _FORGE_DELAY,
) -> tuple[list[tuple[PlaylistSpec, Playlist]], int]:
    """Create up to *limit* specs that are not on the account yet.

    Returns the created pairs and how many specs were still pending
    before this batch, so a caller can report progress. Skipping by
    title is what makes repeated runs resume rather than duplicate.
    """
    existing = {p["name"] for p in await manager.get_user_playlists()}
    pending = [s for s in specs if s.title not in existing]

    created: list[tuple[PlaylistSpec, Playlist]] = []
    for spec in pending[:limit]:
        playlist = await manager.create_playlist_with_tracks(
            name=spec.title,
            owner_id=owner_id,
            tracks=spec.tracks,
            description=spec.description,
            public=public,
        )
        created.append((spec, playlist))
        if delay:
            await asyncio.sleep(delay)
    return created, len(pending)


async def reflow(
    manager: PlaylistManager,
    spotify: Spotify,
    specs: list[PlaylistSpec],
    delay: float = _FORGE_DELAY,
) -> tuple[list[tuple[str, int]], list[str]]:
    """Re-sequence already-created playlists to match their spec.

    Existing playlists keep their identity — same playlist, same
    followers, same URL — while their contents are replaced with the
    freshly ordered track list. Playlists already in the right order are
    left untouched, so a repeat run is cheap and quiet.

    Returns ``(rewritten, failed)`` — ``(title, track_count)`` pairs for
    playlists actually rewritten, and the titles of any that could not
    be. A single timed-out request must not discard the work already
    done across hundreds of playlists, so failures are collected rather
    than raised.
    """
    by_title = {p["name"]: p["id"] for p in await manager.get_user_playlists()}
    rewritten: list[tuple[str, int]] = []
    failed: list[str] = []

    for spec in specs:
        playlist_id = by_title.get(spec.title)
        if playlist_id is None:
            continue
        wanted = [t.uri for t in spec.tracks]
        try:
            current = [
                item.track.uri
                for item in await manager.get_playlist_tracks(playlist_id)
                if item.track is not None and item.track.uri
            ]
            if current == wanted:
                continue
            await spotify.playlist_replace(playlist_id, wanted[:_REPLACE_CHUNK])
            if len(wanted) > _REPLACE_CHUNK:
                await manager.add_tracks(playlist_id, wanted[_REPLACE_CHUNK:])
        except (tk.HTTPError, httpx.HTTPError) as exc:
            logger.warning("Could not reflow %r: %s", spec.title, exc)
            failed.append(spec.title)
            continue
        rewritten.append((spec.title, len(wanted)))
        if delay:
            await asyncio.sleep(delay)

    logger.info("Reflowed %d playlist(s), %d failed", len(rewritten), len(failed))
    return rewritten, failed


async def apply_covers(
    manager: PlaylistManager,
    spotify: Spotify,
    specs: list[PlaylistSpec],
    delay: float = _FORGE_DELAY,
    overwrite: bool = False,
) -> tuple[list[str], list[str]]:
    """Give forged playlists generated cover art.

    Skips playlists that already carry a custom cover unless *overwrite*,
    so a re-run is cheap and never clobbers art the user chose. Returns
    ``(uploaded, failed)`` titles; as with :func:`reflow`, one failure
    must not discard the rest of the run.
    """
    from spotifyforge.core.covers import cover_payload

    by_title = {p["name"]: p["id"] for p in await manager.get_user_playlists()}
    uploaded: list[str] = []
    failed: list[str] = []

    for spec in specs:
        playlist_id = by_title.get(spec.title)
        if playlist_id is None:
            continue
        try:
            if not overwrite and await _has_custom_cover(spotify, playlist_id):
                continue
            await spotify.playlist_cover_image_upload(playlist_id, cover_payload(spec))
        except (tk.HTTPError, httpx.HTTPError) as exc:
            logger.warning("Could not set cover for %r: %s", spec.title, exc)
            failed.append(spec.title)
            continue
        uploaded.append(spec.title)
        if delay:
            await asyncio.sleep(delay)

    logger.info("Set %d cover(s), %d failed", len(uploaded), len(failed))
    return uploaded, failed


async def apply_descriptions(
    manager: PlaylistManager,
    spotify: Spotify,
    specs: list[PlaylistSpec],
    delay: float = _FORGE_DELAY,
) -> tuple[list[str], list[str]]:
    """Bring live playlists' descriptions up to date with their spec.

    Descriptions are written once at forge time and drift as the library
    (and the description templates) evolve; this pushes the current text
    without touching tracks, titles, or followers. Spotify serves
    descriptions back HTML-escaped, so comparison happens on the
    unescaped text and an already-correct playlist costs nothing.
    Returns ``(updated, failed)`` titles; one failure never discards the
    rest of the run.
    """
    by_title = {p["name"]: p for p in await manager.get_user_playlists()}
    updated: list[str] = []
    failed: list[str] = []

    for spec in specs:
        entry = by_title.get(spec.title)
        if entry is None:
            continue
        if html.unescape(entry.get("description") or "") == spec.description:
            continue
        try:
            await spotify.playlist_change_details(entry["id"], description=spec.description)
        except (tk.HTTPError, httpx.HTTPError) as exc:
            logger.warning("Could not describe %r: %s", spec.title, exc)
            failed.append(spec.title)
            continue
        updated.append(spec.title)
        if delay:
            await asyncio.sleep(delay)

    logger.info("Updated %d description(s), %d failed", len(updated), len(failed))
    return updated, failed


async def _has_custom_cover(spotify: Spotify, playlist_id: str) -> bool:
    """Whether a playlist already has art that is not Spotify's mosaic.

    Spotify serves generated mosaics from a different host than uploaded
    images, which is the only signal the API gives for the difference.
    """
    images = await spotify.playlist_cover_image(playlist_id)
    return any("mosaic" not in (img.url or "") for img in images or [])
