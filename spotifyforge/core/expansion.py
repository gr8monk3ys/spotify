"""Grow thin playlists with unheard tracks from the same niche.

The catalogue starts as a pure function of the liked library, which is
what makes ``reflow`` safe to run blindly — but it also means a genre
with nine liked songs stays a nine-song playlist forever. This module
searches Spotify for more of the same niche (tracks the user has never
heard, which the goal explicitly welcomes), and pins the picks in a
local sidecar.

Pinning is the load-bearing design. Search results change run to run,
so candidates are captured once and replayed, never re-derived — the
same reasoning that keys the tempo/key cache by ISRC. Pins are keyed by
``(genre, decade)``, the stable inputs a playlist title is derived
from, so retitling (a template edit, a genre starting to split by
decade) never orphans them. And nothing here writes to Spotify:
:func:`spotifyforge.core.curation.merge_expansions` folds the pins into
the plan, and ``reflow`` remains the single write path, so a playlist
keeps its identity, followers, and additions.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import asdict, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypeVar

import httpx
import tekore as tk

from spotifyforge.core.curation import (
    CurationTrack,
    primary_artist,
    to_curation_track,
    track_song_key,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from tekore import Spotify

    from spotifyforge.core.curation import PlaylistSpec

logger = logging.getLogger(__name__)

_SEARCH_LIMIT = 50  # Spotify max for one search page
# One unfamiliar artist should season a playlist, not take it over.
_PER_ARTIST_CAP = 2
# Anything hotter than this isn't "niche" and sticks out of the set.
_MAX_POPULARITY = 70

# The sidecar's key type: (genre, decade-or-None).
Pins = dict[tuple[str, int | None], list[CurationTrack]]


def expansions_path() -> Path:
    """Where pinned expansions live: ``<db_path parent>/expansions.json``."""
    from spotifyforge.config import sidecar_path

    return sidecar_path("expansions.json")


def load_expansions(path: Path | None = None) -> Pins:
    """The pinned expansion tracks, keyed by ``(genre, decade)``.

    Deliberately no corrupt-file fallback: pretending an unreadable file
    is empty would hand ``reflow`` a pin-free plan, which strips every
    pinned track off the live playlists. Failing loudly is the safe
    behaviour here.
    """
    target = path or expansions_path()
    if not target.exists():
        return {}
    data = json.loads(target.read_text(encoding="utf-8"))
    return {
        _parse_key(raw): [_track_from_dict(entry) for entry in entries]
        for raw, entries in data.items()
    }


def save_expansions(expansions: Pins, path: Path | None = None) -> Path:
    from spotifyforge.config import write_json_atomic

    target = path or expansions_path()
    payload = {_format_key(key): [asdict(t) for t in tracks] for key, tracks in expansions.items()}
    write_json_atomic(target, payload)
    return target


def _format_key(key: tuple[str, int | None]) -> str:
    genre, decade = key
    return f"{genre}|{decade or ''}"


def _parse_key(raw: str) -> tuple[str, int | None]:
    genre, _, decade = raw.rpartition("|")
    return genre, int(decade) if decade else None


def _track_from_dict(entry: dict[str, Any]) -> CurationTrack:
    return CurationTrack(
        id=entry["id"],
        uri=entry["uri"],
        name=entry["name"],
        artist_ids=tuple(entry["artist_ids"]),
        artist_names=tuple(entry["artist_names"]),
        release_year=entry["release_year"],
        popularity=entry["popularity"],
        isrc=entry["isrc"],
        genres=tuple(entry["genres"]),
        # ``.get`` because the 801 pins on disk predate album identity.
        # This buys round-trip fidelity, not compatibility: the dataclass
        # defaults would load those files fine, but a field read with []
        # here would be dropped on load and then written back as its
        # default by the next save — silently erasing it from the pins.
        album_id=entry.get("album_id"),
        album_name=entry.get("album_name", ""),
        album_total_tracks=entry.get("album_total_tracks"),
    )


def _cached_keyed_isrcs() -> set[str]:
    """ISRCs with a cached musical key — the tracks we can sequence.

    This is the default preference for every candidate search, resolved
    here rather than by callers: a caller that forgot to pass it would
    silently reintroduce the key-coverage dilution expansion measurably
    caused before the preference existed.
    """
    from spotifyforge.core.audio_features import load_cached_features

    return {isrc for isrc, feature in load_cached_features().items() if feature.has_key}


def _search_query(spec: PlaylistSpec) -> str:
    """The genre-filtered search for *spec*, era-bounded on decade splits.

    The quotes matter: an unquoted multi-word genre leaks its tail into
    free-text search.
    """
    query = f'genre:"{spec.genre}"'
    if spec.decade:
        query += f" year:{spec.decade}-{spec.decade + 9}"
    return query


async def _find_candidates(
    spotify: Spotify,
    spec: PlaylistSpec,
    taken_ids: set[str],
    taken_keys: set[tuple[str, str]],
    count: int,
    keyed_isrcs: set[str] | None = None,
    freed_track_id: str | None = None,
    prefer_recent: bool = False,
) -> list[CurationTrack]:
    """Up to *count* unheard tracks in *spec*'s niche.

    Skips anything in *taken_ids*/*taken_keys* (read-only here — the
    caller owns the run-level uniqueness invariant), chart-level tracks,
    remasters of songs the catalogue already holds, and more than a
    couple of tracks per artist. The cap is seeded from the playlist's
    own composition here, not by callers — a counter starting empty
    each call would let a weekly refresh stack one artist a track at a
    time. *freed_track_id* is a track about to leave the playlist (a
    rotated-out pin), whose artist slot is free again.

    *keyed_isrcs* — ISRCs with a cached musical key — is a preference,
    not a filter: candidates we can harmonically sequence claim the
    per-artist and count budget first, so growing a playlist stops
    costing it its key coverage, but a niche too obscure for the
    analysis databases still fills. *prefer_recent* adds a secondary
    preference for tracks released in the last two years — rotation
    uses it so the catalogue visibly carries new music — subordinate to
    the key preference: freshness reads in the tracklist, but a broken
    mix reads in the ears.
    """
    try:
        (page,) = await spotify.search(_search_query(spec), types=("track",), limit=_SEARCH_LIMIT)
    except (tk.HTTPError, httpx.HTTPError) as exc:
        logger.warning("Search failed for %r: %s", spec.genre_label, exc)
        return []

    candidates = []
    for track in page.items or []:
        if track is None or track.id is None or track.id in taken_ids:
            continue
        candidate = replace(to_curation_track(track), genres=(spec.genre,) if spec.genre else ())
        if candidate.popularity <= _MAX_POPULARITY:
            candidates.append(candidate)
    keyed = keyed_isrcs or set()
    recent_floor = datetime.now(UTC).year - 1 if prefer_recent else None
    if keyed or recent_floor:
        candidates.sort(  # stable: keyed first, then fresh releases
            key=lambda c: (
                bool(keyed) and c.isrc not in keyed,
                recent_floor is not None and (c.release_year or 0) < recent_floor,
            )
        )

    picked: list[CurationTrack] = []
    new_ids: set[str] = set()
    new_keys: set[tuple[str, str]] = set()
    per_artist = Counter(primary_artist(t) for t in spec.tracks if t.id != freed_track_id)
    for candidate in candidates:
        if candidate.id in new_ids:
            continue
        key = track_song_key(candidate)
        if key in taken_keys or key in new_keys:
            continue
        if per_artist[primary_artist(candidate)] >= _PER_ARTIST_CAP:
            continue
        per_artist[primary_artist(candidate)] += 1
        new_ids.add(candidate.id)
        new_keys.add(key)
        picked.append(candidate)
        if len(picked) == count:
            break
    return picked


async def expand_catalogue(
    spotify: Spotify,
    specs: list[PlaylistSpec],
    target: int = 12,
    limit: int = 10,
    path: Path | None = None,
    expansions: Pins | None = None,
    keyed_isrcs: set[str] | None = None,
    cursor_path: Path | None = None,
) -> tuple[dict[str, list[CurationTrack]], int]:
    """Pick unheard same-niche tracks for playlists below *target* tracks.

    *specs* must already include previous expansions (``plan_catalogue``
    with ``expansions=`` does that), so a playlist stops qualifying once
    pins have brought it up to size — repeat runs continue the catalogue
    instead of piling more onto the same playlists. Pass *expansions*
    when the caller already loaded them; otherwise the sidecar is read.

    *limit* counts playlists that actually gained pins, and the walk is
    round-robin from a cursor sidecar: a niche too barren to ever yield
    candidates gets its search and is moved past, instead of permanently
    occupying a head-of-queue slot every run (with a stable order and
    attempt-counted limits, the same barren genres blocked every run).

    Returns what was newly pinned this run (keyed by title, for
    display) and how many playlists were below the target.
    """
    pins = load_expansions(path) if expansions is None else expansions
    if keyed_isrcs is None:
        keyed_isrcs = _cached_keyed_isrcs()
    taken_ids = {t.id for s in specs for t in s.tracks}
    taken_keys = {track_song_key(t) for s in specs for t in s.tracks}

    def spec_key(spec: PlaylistSpec) -> str:
        return _format_key((spec.genre or "", spec.decade))

    cpath = _cursor_path("expand_cursor.json", cursor_path, path)
    thin = [s for s in specs if s.genre is not None and len(s.tracks) < target]
    order = _rotate_after(sorted(thin, key=spec_key), _load_cursor(cpath), spec_key)

    added: dict[str, list[CurationTrack]] = {}
    for spec in order:
        if len(added) == limit:
            break
        picked = await _find_candidates(
            spotify, spec, taken_ids, taken_keys, target - len(spec.tracks), keyed_isrcs
        )
        if picked:
            taken_ids.update(t.id for t in picked)
            taken_keys.update(track_song_key(t) for t in picked)
            key = (spec.genre or "", spec.decade)
            pins[key] = pins.get(key, []) + picked
            added[spec.title] = picked
            save_expansions(pins, path)
        _save_cursor(spec_key(spec), cpath)

    logger.info(
        "Pinned %d track(s) across %d playlist(s)", sum(map(len, added.values())), len(added)
    )
    return added, len(thin)


# An explored niche below this many tracks isn't a playlist, it's a stub.
_EXPLORE_FLOOR = 4


async def explore_niches(
    spotify: Spotify,
    specs: list[PlaylistSpec],
    genres: list[str],
    size: int = 12,
    path: Path | None = None,
    expansions: Pins | None = None,
    keyed_isrcs: set[str] | None = None,
) -> tuple[dict[str, list[CurationTrack]], list[str]]:
    """Forge playlists for niches the library has no seed for.

    The goal explicitly welcomes genres the user has never heard, but
    until now every playlist grew from at least four liked songs. This
    searches a genre from nothing, pins what it finds under
    ``(genre, None)``, and lets :func:`curation.merge_expansions` turn
    orphan pin keys into full specs — after which forge, reflow,
    covers, describe, and refresh treat the niche like any other.

    Genres the catalogue already covers are skipped (grow those with
    ``expand``), as are niches yielding fewer than four usable tracks —
    a two-track playlist reads as a stub, not a crate. Returns what was
    pinned (keyed by genre) and the list of skipped genres.
    """
    from spotifyforge.core.curation import PlaylistSpec

    pins = load_expansions(path) if expansions is None else expansions
    if keyed_isrcs is None:
        keyed_isrcs = _cached_keyed_isrcs()
    known = {spec.genre for spec in specs if spec.genre} | {genre for genre, _ in pins}
    taken_ids = {t.id for s in specs for t in s.tracks}
    taken_keys = {track_song_key(t) for s in specs for t in s.tracks}

    added: dict[str, list[CurationTrack]] = {}
    skipped: list[str] = []
    for genre in dict.fromkeys(g.strip().lower() for g in genres):
        if not genre or genre in known:
            skipped.append(genre)
            continue
        probe = PlaylistSpec(title=genre, description="", genre=genre, decade=None, tracks=[])
        picked = await _find_candidates(spotify, probe, taken_ids, taken_keys, size, keyed_isrcs)
        if len(picked) < _EXPLORE_FLOOR:
            skipped.append(genre)
            continue
        taken_ids.update(t.id for t in picked)
        taken_keys.update(track_song_key(t) for t in picked)
        pins[(genre, None)] = picked
        added[genre] = picked
        save_expansions(pins, path)

    logger.info("Explored %d new niche(s), skipped %d", len(added), len(skipped))
    return added, skipped


def _cursor_path(name: str, cursor_path: Path | None, pins_path: Path | None) -> Path:
    """A walk cursor lives beside the pins file it paces through."""
    if cursor_path is not None:
        return cursor_path
    if pins_path is not None:
        return pins_path.with_name(name)
    from spotifyforge.config import sidecar_path

    return sidecar_path(name)


def _load_cursor(path: Path) -> str:
    """The last key a round-robin walk handled, or "" to start over.

    Unlike the pins file, a corrupt cursor is harmless — it only decides
    *which* playlists get attention next, never what is on them — so it
    degrades to "start from the top" instead of failing.
    """
    try:
        last = json.loads(path.read_text(encoding="utf-8")).get("last", "")
        return last if isinstance(last, str) else ""
    except (OSError, ValueError):
        return ""


def _save_cursor(last: str, path: Path) -> None:
    from spotifyforge.config import write_json_atomic

    write_json_atomic(path, {"last": last})


_T = TypeVar("_T")


def _rotate_after(ordered: list[_T], cursor: str, key: Callable[[_T], str]) -> list[_T]:
    """*ordered* re-started just past *cursor*, wrapping to the top."""
    start = next((i for i, item in enumerate(ordered) if key(item) > cursor), 0)
    return ordered[start:] + ordered[:start]


async def refresh_pins(
    spotify: Spotify,
    specs: list[PlaylistSpec],
    limit: int = 10,
    path: Path | None = None,
    cursor_path: Path | None = None,
    keyed_isrcs: set[str] | None = None,
    expansions: Pins | None = None,
) -> tuple[dict[str, tuple[CurationTrack, CurationTrack]], int]:
    """Rotate one pinned track per playlist: oldest pin out, a new find in.

    A catalogue that never changes reads as abandoned — to listeners and
    to Spotify's surfacing alike. This swaps the oldest pin of up to
    *limit* pinned playlists for a fresh unheard track from the same
    niche, keeping each playlist's size steady. Nothing is written to
    Spotify: like ``expand``, the pins change locally and ``reflow``
    stays the single write path.

    Runs walk the pinned playlists round-robin — a cursor sidecar
    remembers where the last run stopped, so a weekly refresh visits
    every playlist over time instead of churning the same few. Returns
    ``{title: (out, in)}`` and how many playlists hold pins. Pass
    *expansions* when the caller already loaded them (the plan does).
    """
    pins = load_expansions(path) if expansions is None else expansions
    if keyed_isrcs is None:
        keyed_isrcs = _cached_keyed_isrcs()
    spec_by_key = {(s.genre or "", s.decade): s for s in specs if s.genre is not None}
    eligible = sorted(
        (key for key, tracks in pins.items() if tracks and key in spec_by_key),
        key=_format_key,
    )
    if not eligible:
        return {}, 0

    cpath = _cursor_path("refresh_cursor.json", cursor_path, path)
    order = _rotate_after(eligible, _load_cursor(cpath), _format_key)

    taken_ids = {t.id for s in specs for t in s.tracks}
    taken_keys = {track_song_key(t) for s in specs for t in s.tracks}
    swapped: dict[str, tuple[CurationTrack, CurationTrack]] = {}
    for key in order:
        if len(swapped) == limit:
            break
        spec = spec_by_key[key]
        outgoing = pins[key][0]
        found = await _find_candidates(
            spotify,
            spec,
            taken_ids,
            taken_keys,
            1,
            keyed_isrcs,
            freed_track_id=outgoing.id,
            prefer_recent=True,
        )
        if found:
            incoming = found[0]
            pins[key].pop(0)
            pins[key].append(incoming)
            taken_ids.add(incoming.id)
            taken_keys.add(track_song_key(incoming))
            swapped[spec.title] = (outgoing, incoming)
            # Checkpoint pins before the cursor: saving the cursor first
            # would let a crash advance the walk past rotations that were
            # never persisted.
            save_expansions(pins, path)
        _save_cursor(_format_key(key), cpath)

    logger.info("Rotated pins on %d playlist(s) of %d pinned", len(swapped), len(eligible))
    return swapped, len(eligible)
