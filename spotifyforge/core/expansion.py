"""Grow thin playlists with unheard tracks from the same niche.

The catalogue starts as a pure function of the liked library, which is
what makes ``reflow`` safe to run blindly — but it also means a genre
with nine liked songs stays a nine-song playlist forever. This module
searches Spotify for more of the same niche (tracks the user has never
heard, which the goal explicitly welcomes), and pins the picks in a
local sidecar.

Pinning is the load-bearing part. Search results change run to run, so
candidates are captured once and replayed, never re-derived — the same
reasoning that keys the tempo/key cache by ISRC. And nothing here
writes to Spotify: :func:`spotifyforge.core.curation.merge_expansions`
folds the pins into the plan, and ``reflow`` remains the single write
path, so a playlist keeps its identity, followers, and additions.
"""

from __future__ import annotations

import json
import logging
import os
from collections import Counter
from dataclasses import asdict, replace
from typing import TYPE_CHECKING, Any

import httpx
import tekore as tk

from spotifyforge.core.curation import CurationTrack, song_key, to_curation_track

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


def expansions_path() -> Path:
    """Where pinned expansions live: ``<db_path parent>/expansions.json``."""
    from spotifyforge.config import sidecar_path

    return sidecar_path("expansions.json")


def load_expansions(path: Path | None = None) -> dict[str, list[CurationTrack]]:
    """The pinned expansion tracks, keyed by playlist title."""
    target = path or expansions_path()
    if not target.exists():
        return {}
    data = json.loads(target.read_text(encoding="utf-8"))
    return {
        title: [_track_from_dict(entry) for entry in entries] for title, entries in data.items()
    }


def save_expansions(expansions: dict[str, list[CurationTrack]], path: Path | None = None) -> Path:
    """Persist the pins atomically — a killed run must not tear the file."""
    target = path or expansions_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {title: [asdict(t) for t in tracks] for title, tracks in expansions.items()}
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, target)
    return target


def _track_from_dict(entry: dict[str, Any]) -> CurationTrack:
    return CurationTrack(
        id=entry["id"],
        uri=entry["uri"],
        name=entry["name"],
        artist_ids=tuple(entry["artist_ids"]),
        artist_names=tuple(entry["artist_names"]),
        release_year=entry["release_year"],
        popularity=entry["popularity"],
        isrc=entry.get("isrc"),
        genres=tuple(entry.get("genres", ())),
    )


def _search_query(spec: PlaylistSpec) -> str:
    """The genre-filtered search for *spec*, era-bounded on decade splits."""
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

    Filters out anything the catalogue already holds — by track id and
    by song identity, so a remaster of a liked song doesn't sneak in —
    plus chart-level tracks and more than a couple per artist.
    *taken_ids*/*taken_keys* are updated in place so one run never picks
    the same track for two playlists.
    """
    try:
        (page,) = await spotify.search(_search_query(spec), types=("track",), limit=_SEARCH_LIMIT)
    except (tk.HTTPError, httpx.HTTPError) as exc:
        logger.warning("Search failed for %r: %s", spec.genre_label, exc)
        return []

    picked: list[CurationTrack] = []
    per_artist: Counter[str] = Counter()
    for track in page.items or []:
        if track is None or track.id is None:
            continue
        candidate = replace(to_curation_track(track), genres=(spec.genre,) if spec.genre else ())
        key = song_key(candidate.name, candidate.artist_names[0] if candidate.artist_names else "")
        lead = candidate.artist_ids[0] if candidate.artist_ids else ""
        if candidate.id in taken_ids or key in taken_keys:
            continue
        if candidate.popularity > _MAX_POPULARITY:
            continue
        if per_artist[lead] >= _PER_ARTIST_CAP:
            continue
        per_artist[lead] += 1
        taken_ids.add(candidate.id)
        taken_keys.add(key)
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
) -> tuple[dict[str, list[CurationTrack]], Path]:
    """Pick unheard same-niche tracks for playlists below *target* tracks.

    *specs* must already include previous expansions (``plan_catalogue``
    with ``expansions=`` does that), so a playlist stops qualifying once
    pins have brought it up to size — repeat runs continue the catalogue
    instead of piling more onto the same playlists. Returns what was
    newly pinned this run, keyed by title.
    """
    expansions = load_expansions(path)
    taken_ids = {t.id for s in specs for t in s.tracks}
    taken_ids |= {t.id for tracks in expansions.values() for t in tracks}
    taken_keys = {
        song_key(t.name, t.artist_names[0] if t.artist_names else "")
        for s in specs
        for t in s.tracks
    }

    added: dict[str, list[CurationTrack]] = {}
    thin = [s for s in specs if s.genre is not None and len(s.tracks) < target]
    for spec in thin[:limit]:
        picked = await _find_candidates(
            spotify, spec, taken_ids, taken_keys, target - len(spec.tracks)
        )
        if not picked:
            continue
        expansions[spec.title] = expansions.get(spec.title, []) + picked
        added[spec.title] = picked

    if added:
        saved_to = save_expansions(expansions, path)
    else:
        saved_to = path or expansions_path()
    logger.info(
        "Pinned %d track(s) across %d playlist(s)", sum(map(len, added.values())), len(added)
    )
    return added, saved_to
