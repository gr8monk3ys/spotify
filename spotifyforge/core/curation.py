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
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING, Any

import httpx
import tekore as tk

from spotifyforge.core.audio_features import AudioFeature, key_distance
from spotifyforge.core.playlist_manager import extract_isrc

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable

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
    """A liked track: what curation decides on, plus album identity.

    Album fields are carried for :mod:`spotifyforge.core.export` rather
    than for any curation decision — clustering never groups by album.
    They ride along because the saved-tracks payload already contains
    them; recovering them later would cost a second library walk.
    """

    id: str
    uri: str
    name: str
    artist_ids: tuple[str, ...]
    artist_names: tuple[str, ...]
    release_year: int | None
    popularity: int
    isrc: str | None = None  # global recording id; the key for tempo/key lookups
    genres: tuple[str, ...] = ()
    # Album identity. Curation itself never groups by album — these exist
    # so the library can be exported at album level, which is the unit
    # other platforms (Discogs releases, RYM ratings) are keyed by.
    album_id: str | None = None
    album_name: str = ""
    album_total_tracks: int | None = None


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
    # Liked songs that reached a playlist. Kept as a field rather than
    # derived from the specs because those also carry pinned tracks the
    # user has never heard: counting rows in the catalogue and calling
    # them placed liked songs reported more songs placed than exist,
    # and "-4 unplaced".
    placed_liked_count: int = 0

    @property
    def collapsed_count(self) -> int:
        """Duplicate recordings removed before clustering."""
        return self.liked_count - self.unique_count

    @property
    def unplaced_count(self) -> int:
        """Liked songs in genres too small to fill a playlist."""
        return self.unique_count - self.placed_liked_count

    @property
    def placed_count(self) -> int:
        """Distinct songs in the catalogue, pinned discoveries included."""
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
        for page in await gather_bounded(
            [partial(self._sp.saved_tracks, limit=_LIKED_PAGE, offset=o) for o in offsets]
        ):
            if page is not None:
                out.extend(_saved_page_to_tracks(page))

        logger.info("Fetched %d liked tracks", len(out))
        return out

    async def enrich_genres(self, tracks: list[CurationTrack]) -> list[CurationTrack]:
        """Fill each track's ``genres`` from its **primary** artist.

        Spotify tags genres on artists, never on tracks, so a track's
        genres can only be inherited. Inheriting them from *every*
        credited artist looked more generous and was the single largest
        source of wrong placements: a feature drags its own genres onto
        a song it guests on. Rico Nasty's "Vvgina" landed on an acid
        techno playlist because Locked Club guests on it; Riot Shift's
        hardstyle "666" landed on hard techno because its feature
        carries that tag. Measured over this library, 251 tracks carried
        a genre their primary artist does not have, and the worst
        offenders — all multi-artist collaborations — reached nine
        playlists each.

        A track whose primary artist Spotify has not tagged gets no
        genres at all rather than borrowing its features'. That is what
        the unclassified playlists are for, and "we do not know" beats a
        confident wrong answer on a catalogue whose whole claim is that
        the genres mean something.
        """
        artist_ids = sorted({aid for t in tracks for aid in t.artist_ids})
        batches = [
            artist_ids[offset : offset + _ARTIST_BATCH]
            for offset in range(0, len(artist_ids), _ARTIST_BATCH)
        ]

        genres_by_artist: dict[str, tuple[str, ...]] = {}
        for artists in await gather_bounded([partial(self._sp.artists, b) for b in batches]):
            for artist in artists or ():
                genres_by_artist[artist.id] = tuple(artist.genres or ())

        enriched = [
            replace(t, genres=genres_by_artist.get(t.artist_ids[0], ()) if t.artist_ids else ())
            for t in tracks
        ]
        logger.info(
            "Enriched %d tracks from %d artists (%d without genres)",
            len(enriched),
            len(artist_ids),
            sum(1 for t in enriched if not t.genres),
        )
        return enriched


async def gather_bounded(factories: list[Any]) -> list[Any]:
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
        to_curation_track(item.track)
        for item in paging.items or []
        if item.track is not None and item.track.id is not None
    ]


def to_curation_track(track: Any) -> CurationTrack:
    album = track.album
    release_year: int | None = None
    if album is not None and album.release_date:
        try:
            release_year = int(str(album.release_date)[:4])
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
        album_id=album.id if album else None,
        album_name=(album.name or "") if album else "",
        # Only ``total_tracks`` is genuinely optional: a LocalAlbum has no
        # such attribute. Hedging the other two the same way would turn a
        # model change into 1700 albums silently exported with a null
        # affinity — the very field consumers are told to rank by.
        album_total_tracks=getattr(album, "total_tracks", None),
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


def track_song_key(track: CurationTrack) -> tuple[str, str]:
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
        key = track_song_key(track)
        if key not in best or track.popularity > tracks[best[key]].popularity:
            best[key] = index
    if len(best) < len(tracks):
        logger.info("Collapsed %d duplicate recordings", len(tracks) - len(best))
    return [tracks[i] for i in sorted(best.values())]


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------


def _genre_affinity(tracks: list[CurationTrack], viable: set[str]) -> dict[tuple[str, str], int]:
    """How often each pair of viable genres sits on the same track."""
    affinity: dict[tuple[str, str], int] = defaultdict(int)
    for t in tracks:
        carried = [g for g in t.genres if g in viable]
        for a in carried:
            for b in carried:
                if a != b:
                    affinity[(a, b)] += 1
    return affinity


def _core_genre(
    candidates: list[str],
    counts: Counter[str],
    affinity: dict[tuple[str, str], int],
) -> str:
    """The one genre that best describes a track carrying *candidates*.

    Scores each candidate by how much of *its own* membership sits
    inside the track's other genres. A specific subgenre is almost
    always co-tagged with its parents, so nearly all of it falls inside
    the cluster and it scores high; an umbrella like "rock" has most of
    its tracks outside and scores low. The winner is therefore the
    tightest genre that still genuinely describes the track — which is
    also the nichest one that is actually true.

    Normalising by the *sibling's* size instead inverts this and elects
    the umbrella every time: 15 of 15 slowcore tracks being rock says
    slowcore is contained in rock, and reads as evidence for "rock".

    Picking the plain *rarest* candidate — the obvious reading of "most
    niche" — is a different failure. Rarity says how few artists carry a
    tag, not how well it fits: it filed five Raxeller hard techno tracks
    under "industrial" (8 tracks library-wide, and their rarest tag) and
    dissolved the techno playlists altogether. Rarity survives only as
    the tie-break, where two genres describe the track equally well and
    the nicher one is the better calling card.
    """

    def score(genre: str) -> tuple[float, int, str]:
        containment = sum(
            affinity[(genre, other)] / counts[genre] for other in candidates if other != genre
        )
        return (-containment, counts[genre], genre)

    return min(candidates, key=score)


def cluster_library(
    tracks: list[CurationTrack],
    min_size: int = 12,
    max_size: int = 60,
    exclusive: bool = False,
    include_unclassified: bool = True,
    features: dict[str, AudioFeature] | None = None,
    discovery_keys: Collection[tuple[str, int | None]] = (),
) -> list[PlaylistSpec]:
    """Group tracks into niche playlist specs.

    A genre is *viable* if at least *min_size* of the given tracks carry
    it; genres above *max_size* are split by decade.

    By default a track joins **every** viable genre it carries: a song
    really is both psychedelic rock and neo-psychedelic, and that overlap
    is what turns a few dozen playlists into a few hundred. With
    *exclusive* it joins only the one genre that best describes it (see
    :func:`_core_genre`), so no song is ever in two playlists — which
    also dissolves the near-identical playlists that heavy overlap
    produces, where four techno lists shared the same five tracks.

    *discovery_keys* names ``(genre, decade)`` pairs to keep as specs
    even when too few tracks land in them — the playlists that exclusive
    assignment empties out. They come back with no liked songs at all,
    for :func:`merge_expansions` to fill with pinned unheard music;
    :func:`plan_catalogue` drops any that stay under *min_size*, so a
    live playlist is never reflowed down to nothing.

    With *include_unclassified*, tracks that no viable genre claims —
    Spotify tags no genre at all on plenty of small artists — are
    gathered into per-decade playlists rather than discarded. On a niche
    account those are the most on-brand tracks in the library.

    Callers are expected to have applied :func:`dedupe_versions` first;
    :func:`plan_catalogue` does.
    """
    genre_counts: Counter[str] = Counter(g for t in tracks for g in t.genres)
    viable = {g for g, count in genre_counts.items() if count >= min_size}

    affinity = _genre_affinity(tracks, viable) if exclusive else {}

    by_genre: dict[str, list[CurationTrack]] = {}
    for t in tracks:
        candidates = [g for g in t.genres if g in viable]
        if not candidates:
            continue
        if exclusive:
            candidates = [_core_genre(candidates, genre_counts, affinity)]
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

    # A genre with enough tracks to be worth a playlist, that exclusive
    # assignment then left too thin to be one, becomes a discovery spec:
    # the playlist stays, and expand fills it with music from that niche
    # the user has never heard. This is what stops the fix from quietly
    # abandoning a live playlist to the songs it no longer deserves.
    built = {(s.genre or "", s.decade) for s in specs}
    assigned = {s.genre for s in specs if s.genre}
    emptied = {(genre, None) for genre in viable if genre not in assigned}
    # Sorted only for a stable spec order; the undated key sorts first
    # rather than blowing up comparing None against a decade.
    keys = emptied | {k for k in discovery_keys if k[0]}
    for genre, decade in sorted(keys, key=lambda k: (k[0], -1 if k[1] is None else k[1])):
        if (genre, decade) not in built:
            specs.append(_make_spec(genre, decade, [], features))

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


def primary_artist(track: CurationTrack) -> str:
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
    left: Counter[str] = Counter(primary_artist(t) for t in remaining)
    out: list[CurationTrack] = []

    while remaining:
        index = 0
        if out:
            candidates = [i for i, t in enumerate(remaining) if not _shares_artist(t, out[-1])]
            if candidates:
                index = max(candidates, key=lambda i: (left[primary_artist(remaining[i])], -i))
        chosen = remaining.pop(index)
        left[primary_artist(chosen)] -= 1
        out.append(chosen)
    return out


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------

# Naming follows what actually gets followed. A sample of 212 playlists
# other people own, pulled from Spotify search across sixteen niche
# genres, says: 67% keep the genre word in the title (it is the strongest
# thing search indexes), 18% stack sibling genres behind separators, 15%
# lead with an era or nationality, and the median name is 20 characters
# and three words. So a title takes the shape its own data supports —
# stacked when the playlist genuinely spans siblings, era-led when it is
# a decade split, an atmospheric hook when neither — and always names its
# genre.
#
# The old scheme was eight templates over three hundred playlists, so
# every phrasing repeated ~38 times. Those templates survive as
# _LEGACY_TITLE_TEMPLATES purely so ``curate rename`` can find the live
# playlists it has to rename; nothing else should read them.
_LEGACY_TITLE_TEMPLATES = [
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

# A sibling has to carry a real share of the playlist before it earns
# billing — stacking a genre that two tracks happen to mention is the
# keyword-spam version of this pattern, and it misdescribes the mix.
_PARTNER_SHARE = 0.4
_MAX_PARTNERS = 2
# Spotify allows 100 characters, but the sampled median was 20; a stacked
# title past this reads as a tag dump, so it sheds partners instead.
_MAX_TITLE = 48

# Atmospheric hooks for playlists with no sibling to stack and no decade
# to lead with. Grouped by family so a jazz playlist sounds like jazz —
# the same reasoning as the cover-art scenes. First match wins.
# Keywords match on word boundaries, never as substrings: "core" inside
# "score" put film scores in the metal family, and "minimal" inside
# "minimalism" put Philip Glass in the coldwave one. Compound genres are
# therefore listed in full ("hardcore punk", not "core").
_HOOKS: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
    (
        ("jazz", "bop", "bebop", "swing", "bossa nova", "dixieland"),
        (
            "{genre} after the last set",
            "the {genre} smoke room",
            "{genre} at 3am",
            "blue hour {genre}",
            "{genre} on a slow night",
        ),
    ),
    (
        (
            "techno",
            "house",
            "rave",
            "edm",
            "electro",
            "club",
            "trance",
            "dance",
            "gabber",
            "tekno",
            "dubstep",
            "riddim",
            "drum and bass",
            "jungle",
            "footwork",
            "juke",
            "breakcore",
            "speedcore",
            "hardcore techno",
        ),
        (
            "{genre} until the lights",
            "concrete {genre}",
            "{genre} for the long room",
            "{genre}, no encore",
            "6am {genre}",
        ),
    ),
    (
        ("ambient", "drone", "new age", "field recordings", "minimalism"),
        (
            "{genre} for empty rooms",
            "{genre}, no hurry",
            "the {genre} long view",
            "{genre} at low tide",
        ),
    ),
    (
        (
            "metal",
            "metalcore",
            "mathcore",
            "hardcore",
            "hardcore punk",
            "punk",
            "grindcore",
            "sludge",
            "doom",
            "screamo",
            "deathstep",
            "trap metal",
        ),
        ("{genre}, no apologies", "heavy {genre}", "{genre} at full volume", "the {genre} pit"),
    ),
    (
        ("hip hop", "rap", "trap", "drill", "grime", "boom bap", "horrorcore"),
        (
            "{genre}, dust and vinyl",
            "the {genre} basement",
            "{genre} on tape",
            "{genre} after midnight",
        ),
    ),
    (
        ("folk", "country", "americana", "bluegrass", "singer"),
        (
            "{genre} at the treeline",
            "{genre}, porch light on",
            "back road {genre}",
            "{genre} in the morning",
        ),
    ),
    (
        ("afrobeat", "afrobeats", "amapiano", "highlife", "afro r&b", "alté", "gqom"),
        (
            "{genre} after dark",
            "{genre}, sun and dust",
            "the {genre} hours",
            "{genre} on the corner",
        ),
    ),
    (
        (
            "latin",
            "reggaeton",
            "salsa",
            "cumbia",
            "bolero",
            "tango",
            "trova",
            "mpb",
            "samba",
            "bachata",
            "chicha",
            "soca",
            "candombe",
            "ranchera",
            "bossa",
        ),
        (
            "{genre} after dark",
            "{genre}, sun and shade",
            "the {genre} hours",
            "{genre} on the corner",
        ),
    ),
    (
        ("soul", "r&b", "funk", "disco", "motown"),
        ("{genre}, lights low", "the {genre} groove", "{genre} on 45", "slow {genre}"),
    ),
    (
        (
            "psychedelic",
            "neo-psychedelic",
            "shoegaze",
            "dream pop",
            "space rock",
            "krautrock",
            "acid rock",
        ),
        (
            "{genre} in slow collapse",
            "{genre}, dissolved",
            "the {genre} haze",
            "{genre} underwater",
        ),
    ),
    (
        (
            "coldwave",
            "darkwave",
            "new wave",
            "minimal wave",
            "synthpop",
            "synthwave",
            "goth",
            "gothic rock",
            "deathrock",
            "industrial",
            "ebm",
            "post-punk",
        ),
        (
            "{genre} after dark",
            "{genre} and streetlight",
            "{genre}, concrete and neon",
            "the {genre} winter",
        ),
    ),
    (
        ("dungeon synth", "medieval folk", "neofolk", "ritual", "gregorian chant"),
        (
            "{genre} for long winters",
            "{genre} by candle",
            "the {genre} keep",
            "{genre}, moss and stone",
        ),
    ),
    (
        (
            "library music",
            "musique concrete",
            "tape music",
            "noise",
            "japanoise",
            "avant-garde",
            "experimental",
            "score",
            "soundtrack",
            "sound collage",
        ),
        (
            "{genre}, reel to reel",
            "the {genre} archive",
            "found {genre}",
            "{genre} in the wrong room",
        ),
    ),
    (
        ("rock", "indie", "garage", "emo", "grunge", "britpop", "madchester"),
        (
            "{genre}, loud and early",
            "the {genre} basement",
            "{genre} on rotation",
            "{genre} after hours",
        ),
    ),
    (
        ("pop", "bedroom pop", "hyperpop", "city pop", "art pop"),
        (
            "{genre}, quietly",
            "{genre} at the end of the night",
            "the {genre} hours",
            "{genre} on repeat",
        ),
    ),
]
_DEFAULT_HOOKS = (
    "{genre}, quietly",
    "the {genre} hours",
    "{genre} in the margins",
    "a room of {genre}",
    "{genre}, unfiled",
    "deep {genre}",
)


def _partner_genres(genre: str, tracks: list[CurationTrack]) -> list[str]:
    """Sibling genres carried by enough of *tracks* to deserve billing."""
    if not tracks:
        return []
    counts = Counter(g for t in tracks for g in t.genres if g != genre)
    floor = max(2, round(len(tracks) * _PARTNER_SHARE))
    return [g for g, n in counts.most_common(_MAX_PARTNERS) if n >= floor]


def _hook_title(genre: str) -> str:
    """A stable atmospheric title, flavoured by the genre's family."""
    pool: tuple[str, ...] = _DEFAULT_HOOKS
    for keywords, hooks in _HOOKS:
        if any(re.search(rf"\b{re.escape(k)}\b", genre) for k in keywords):
            pool = hooks
            break
    return pool[hashlib.sha256(genre.encode()).digest()[0] % len(pool)].format(genre=genre)


def title_for(genre: str | None, decade: int | None, tracks: list[CurationTrack]) -> str:
    """The playlist's name: stacked, era-led, or hooked — always genre-named."""
    if genre is None:
        return f"{_UNCLASSIFIED_TITLE} ('{decade % 100:02d}s)" if decade else _UNCLASSIFIED_TITLE
    if decade:
        # Real curators lead with the era ("70s spiritual jazz"); the old
        # trailing "('70s)" read as a footnote rather than a hook.
        return f"{decade % 100:02d}s {genre}"
    partners = _partner_genres(genre, tracks)
    while partners:
        stacked = " | ".join([genre, *partners])
        if len(stacked) <= _MAX_TITLE:
            return stacked
        partners.pop()
    return _hook_title(genre)


def legacy_title(genre: str | None, decade: int | None) -> str:
    """What :func:`title_for` would have produced before the rename.

    ``curate rename`` finds live playlists by this name; nothing else
    should depend on it.
    """
    if genre is None:
        title = _UNCLASSIFIED_TITLE
    else:
        template = _LEGACY_TITLE_TEMPLATES[
            sum(ord(c) for c in genre) % len(_LEGACY_TITLE_TEMPLATES)
        ]
        article = "an" if genre[:1].lower() in "aeiou" else "a"
        title = template.format(genre=genre, a=article)
    return f"{title} ('{decade % 100:02d}s)" if decade else title


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
# When even one artist name would overflow the limit (or no track carries
# a name at all), fall back to genre-only phrasing.
_FALLBACK_DESCRIPTION = "{genre}, start to finish."
_UNCLASSIFIED_FALLBACK = "songs spotify never tagged with a genre."
_DESCRIPTION_MAX = 300  # Spotify's documented limit for playlist descriptions


def _lead_artists(tracks: list[CurationTrack], count: int = 3) -> tuple[str, ...]:
    """The playlist's most recognisable artist names, most popular first."""
    ranked = sorted(tracks, key=lambda t: (-t.popularity, t.id))
    return _ordered_unique(t.artist_names[0] for t in ranked if t.artist_names)[:count]


def _join_names(names: tuple[str, ...]) -> str:
    if len(names) > 1:
        return ", ".join(names[:-1]) + " and " + names[-1]
    return names[0] if names else ""


def _describe(
    genre: str | None, decade: int | None, tracks: list[CurationTrack], ordering: str
) -> str:
    """A human-sounding description, deterministic per genre.

    Phrasing is chosen by hashing the genre (as cover palettes do, but
    salted so template and hue stay independent), so a genre keeps its
    voice across runs and neighbouring playlists don't all read alike.
    Tries three artist names, then fewer if the result would overrun
    Spotify's length limit.
    """
    if genre is None:
        base, fallback = _UNCLASSIFIED_DESCRIPTION, _UNCLASSIFIED_FALLBACK
    else:
        digest = hashlib.sha256(("description " + genre).encode()).digest()
        base = _DESCRIPTION_TEMPLATES[digest[0] % len(_DESCRIPTION_TEMPLATES)]
        fallback = _FALLBACK_DESCRIPTION

    suffix = ""
    if decade:
        suffix += f" all from the {decade}s."
    if ordering == "harmonic":
        suffix += " mixed by key, like a dj set."

    names = _lead_artists(tracks)
    for count in range(len(names), 0, -1):
        text = base.format(genre=genre, artists=_join_names(names[:count])) + suffix
        if len(text) <= _DESCRIPTION_MAX:
            return text
    text = fallback.format(genre=genre) + suffix
    return text if len(text) <= _DESCRIPTION_MAX else text[:_DESCRIPTION_MAX]


def _make_spec(
    genre: str | None,
    decade: int | None,
    members: list[CurationTrack],
    features: dict[str, AudioFeature] | None = None,
) -> PlaylistSpec:
    ordered, ordering = order_with_mode(members, features)
    return PlaylistSpec(
        title=title_for(genre, decade, ordered),
        description=_describe(genre, decade, ordered, ordering),
        genre=genre,
        decade=decade,
        tracks=ordered,
        ordering=ordering,
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def merge_expansions(
    specs: list[PlaylistSpec],
    extras: dict[tuple[str, int | None], list[CurationTrack]] | None,
    features: dict[str, AudioFeature] | None = None,
) -> list[PlaylistSpec]:
    """Fold pinned expansion tracks into their playlists' specs.

    Expansions are unheard tracks ``curate expand`` recorded for a
    playlist; merging them at plan time is what keeps ``reflow`` from
    stripping them back out. *extras* is keyed by ``(genre, decade)`` —
    the stable inputs a title is derived from — never by the title
    itself, which changes whenever the templates do or a genre starts
    splitting by decade. Rebuilding the spec from the combined tracks
    de-duplicates (a pinned track the user later likes must not appear
    twice), re-sequences, and lets the description name the new artists.

    Pin keys with no matching spec become playlists of their own: that
    is how ``curate explore`` forges niches the library has no seed for
    — the pins *are* the playlist, and dropping them here would make an
    explored niche invisible to forge, reflow, covers, and describe.
    """
    if not extras:
        return specs
    merged = [
        _make_spec(spec.genre, spec.decade, dedupe_versions(spec.tracks + additions), features)
        if (additions := extras.get((spec.genre or "", spec.decade)))
        else spec
        for spec in specs
    ]
    known = {(spec.genre or "", spec.decade) for spec in specs}
    merged += [
        _make_spec(genre or None, decade, dedupe_versions(additions), features)
        for (genre, decade), additions in sorted(extras.items())
        if (genre, decade) not in known and additions
    ]
    return merged


def writable_specs(specs: list[PlaylistSpec], min_size: int) -> list[PlaylistSpec]:
    """The specs solid enough to write to Spotify.

    A discovery spec starts empty and is only worth a playlist once
    ``expand`` has pinned enough unheard tracks to fill it. It has to
    stay in the plan so expand can find it at all, which is exactly why
    the write paths — and only the write paths — drop it: reflow must
    never replace a live playlist's songs with three of them, and forge
    must not create a playlist that thin.
    """
    return [spec for spec in specs if len(spec.tracks) >= min_size]


async def plan_catalogue(
    spotify: Spotify,
    opts: CurationOptions,
    features: dict[str, AudioFeature] | None = None,
    expansions: dict[tuple[str, int | None], list[CurationTrack]] | None = None,
) -> CurationPlan:
    """Read the library and return the catalogue it would produce.

    Pure read — nothing is created. ``curate plan`` shows this and
    ``curate forge`` acts on it, so both see the same clusters. Pass
    *features* (from :func:`load_features`) to sequence harmonically and
    *expansions* (from :func:`spotifyforge.core.expansion.load_expansions`)
    so playlists keep the unheard tracks pinned to them.
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
        discovery_keys=tuple(expansions or ()),
    )
    specs = merge_expansions(specs, expansions, features)
    in_catalogue = {t.id for s in specs for t in s.tracks}
    return CurationPlan(
        liked_count=len(liked),
        unique_count=len(unique),
        specs=specs,
        placed_liked_count=len({t.id for t in unique} & in_catalogue),
    )


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
    without touching tracks, titles, or followers. Playlists already
    carrying the wanted text are skipped, so a re-run costs nothing.
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
        if entry["description"] == spec.description:
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
