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
from typing import TYPE_CHECKING, Any

import httpx
import tekore as tk

from spotifyforge.core.curation import (
    CurationTrack,
    primary_artist,
    to_curation_track,
    track_song_key,
)

if TYPE_CHECKING:
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
        # Read defensively: pins written before album identity existed
        # carry none of these, and must keep loading.
        album_id=entry.get("album_id"),
        album_name=entry.get("album_name", ""),
        album_total_tracks=entry.get("album_total_tracks"),
    )


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
) -> list[CurationTrack]:
    """Up to *count* unheard tracks in *spec*'s niche.

    Skips anything in *taken_ids*/*taken_keys* (read-only here — the
    caller owns the run-level uniqueness invariant), chart-level tracks,
    remasters of songs the catalogue already holds, and more than a
    couple of tracks per artist.
    """
    try:
        (page,) = await spotify.search(_search_query(spec), types=("track",), limit=_SEARCH_LIMIT)
    except (tk.HTTPError, httpx.HTTPError) as exc:
        logger.warning("Search failed for %r: %s", spec.genre_label, exc)
        return []

    picked: list[CurationTrack] = []
    new_ids: set[str] = set()
    new_keys: set[tuple[str, str]] = set()
    per_artist: Counter[str] = Counter()
    for track in page.items or []:
        if track is None or track.id is None or track.id in taken_ids or track.id in new_ids:
            continue
        candidate = replace(to_curation_track(track), genres=(spec.genre,) if spec.genre else ())
        if candidate.popularity > _MAX_POPULARITY:
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
) -> tuple[dict[str, list[CurationTrack]], int]:
    """Pick unheard same-niche tracks for playlists below *target* tracks.

    *specs* must already include previous expansions (``plan_catalogue``
    with ``expansions=`` does that), so a playlist stops qualifying once
    pins have brought it up to size — repeat runs continue the catalogue
    instead of piling more onto the same playlists. Pass *expansions*
    when the caller already loaded them; otherwise the sidecar is read.

    Returns what was newly pinned this run (keyed by title, for
    display) and how many playlists were below the target.
    """
    pins = load_expansions(path) if expansions is None else expansions
    taken_ids = {t.id for s in specs for t in s.tracks}
    taken_keys = {track_song_key(t) for s in specs for t in s.tracks}

    added: dict[str, list[CurationTrack]] = {}
    thin = [s for s in specs if s.genre is not None and len(s.tracks) < target]
    for spec in thin[:limit]:
        picked = await _find_candidates(
            spotify, spec, taken_ids, taken_keys, target - len(spec.tracks)
        )
        if not picked:
            continue
        taken_ids.update(t.id for t in picked)
        taken_keys.update(track_song_key(t) for t in picked)
        key = (spec.genre or "", spec.decade)
        pins[key] = pins.get(key, []) + picked
        added[spec.title] = picked

    if added:
        save_expansions(pins, path)
    logger.info(
        "Pinned %d track(s) across %d playlist(s)", sum(map(len, added.values())), len(added)
    )
    return added, len(thin)
