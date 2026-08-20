"""Rename forged playlists in place, without orphaning them.

Playlist identity in this catalogue *is* the title: ``forge`` skips a
spec whose name already exists, and ``reflow``, ``covers`` and
``describe`` all look their target up by name. So changing the naming
scheme is not a cosmetic edit — do it without renaming the live
playlists and the next forge run creates three hundred duplicates and
strands the originals, along with their followers, artwork and
descriptions.

This module is the safe path. It pairs each spec's *legacy* title with
its current one, renames the live playlist in place (Spotify keeps the
id, followers, cover and description across a name change), and moves
the photo-cover picks — which are also keyed by title — across to the
new key in the same pass.

Resumability matters because a partial run leaves the catalogue in two
naming schemes at once: a playlist already renamed is simply skipped, so
an interrupted run can be re-run. Nothing is touched unless its current
name matches the legacy scheme exactly, which is what keeps personal
playlists — renamed by hand, never forged — out of it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from tekore import Spotify

    from spotifyforge.core.curation import PlaylistSpec
    from spotifyforge.core.playlist_manager import PlaylistManager

logger = logging.getLogger(__name__)

_RENAME_DELAY = 1.0  # one write per second, same pacing as forge

# Shared fraction of the smaller side above which a live playlist and a
# planned one are the same playlist. Re-clustering moves a minority of
# any playlist's tracks; a genuinely different genre shares far less.
_MIN_OVERLAP = 0.5


@dataclass(frozen=True)
class Rename:
    """One playlist's move from its legacy name to its current one."""

    old: str
    new: str


def plan_renames(specs: list[PlaylistSpec]) -> list[Rename]:
    """Pair every spec's legacy title with the name it now wants."""
    from spotifyforge.core.curation import legacy_title

    return [
        Rename(old=old, new=spec.title)
        for spec in specs
        if (old := legacy_title(spec.genre, spec.decade)) != spec.title
    ]


def match_by_contents(
    specs: list[PlaylistSpec],
    live: dict[str, set[str]],
    min_overlap: float = _MIN_OVERLAP,
) -> tuple[list[Rename], list[PlaylistSpec]]:
    """Pair each spec with the live playlist that already holds it.

    :func:`plan_renames` can only follow a change of naming *template*,
    because it re-derives the old title from ``(genre, decade)``. Change
    how tracks are assigned to genres and that key moves too: a playlist
    live as "acid techno | techno | hard techno" is planned as "hard
    techno | techno | tekno", and matching by key would call it a new
    playlist, create it, and strand the original along with its
    followers, artwork and search ranking.

    So matching happens on the one thing that survives a re-clustering:
    the songs. *live* maps playlist name to its current track ids.
    Scoring is the overlap coefficient — the shared fraction of the
    smaller side — so a small playlist still matches the big one it came
    out of. Pairs are taken greedily, best first, one spec to one
    playlist, which keeps two similar specs from claiming the same live
    playlist.

    An exact title match is taken first and unconditionally: a playlist
    already carrying its planned name is that playlist, whatever a
    re-sequenced tracklist does to the overlap.

    Returns ``(renames, unmatched)`` — the moves to apply, and the specs
    that are genuinely new and should be forged.
    """
    renames: list[Rename] = []
    spec_pool = list(specs)
    live_pool = dict(live)

    exact = [s for s in spec_pool if s.title in live_pool]
    for spec in exact:
        del live_pool[spec.title]
        spec_pool.remove(spec)

    scored: list[tuple[float, str, str]] = []
    for spec in spec_pool:
        wanted = {t.id for t in spec.tracks}
        if not wanted:
            continue
        for name, held in live_pool.items():
            shared = len(wanted & held)
            if not shared:
                continue
            overlap = shared / min(len(wanted), len(held))
            if overlap >= min_overlap:
                scored.append((overlap, spec.title, name))

    # Sort by overlap, then by name so a tie resolves the same way twice
    # — a dry run has to predict what --apply will do.
    scored.sort(key=lambda row: (-row[0], row[1], row[2]))
    claimed_specs: set[str] = set()
    claimed_live: set[str] = set()
    for _, title, name in scored:
        if title in claimed_specs or name in claimed_live:
            continue
        claimed_specs.add(title)
        claimed_live.add(name)
        renames.append(Rename(old=name, new=title))

    unmatched = [s for s in spec_pool if s.title not in claimed_specs]
    logger.info(
        "Matched %d spec(s) by contents, %d already correctly named, %d genuinely new",
        len(renames),
        len(exact),
        len(unmatched),
    )
    return renames, unmatched


def migrate_cover_picks(renames: list[Rename], path: Path | None = None) -> int:
    """Move photo-cover picks onto the new titles.

    The picks sidecar is keyed by title, so a rename without this leaves
    every cover orphaned: the next ``covers`` run would treat all three
    hundred playlists as uncovered and re-pick fresh photographs for
    them, changing artwork that was chosen once and is meant to be
    stable.
    """
    from spotifyforge.core.photo_covers import load_picks, save_picks

    picks = load_picks(path)
    moved = 0
    for rename in renames:
        if rename.old in picks and rename.new not in picks:
            picks[rename.new] = picks.pop(rename.old)
            moved += 1
    if moved:
        save_picks(picks, path)
    logger.info("Moved %d cover pick(s) onto renamed playlists", moved)
    return moved


async def apply_renames(
    manager: PlaylistManager,
    spotify: Spotify,
    renames: list[Rename],
    picks_path: Path | None = None,
    delay: float = _RENAME_DELAY,
) -> tuple[list[Rename], list[str], list[str]]:
    """Rename live playlists in place.

    Returns ``(renamed, already, failed)``: the ones moved this run, the
    ones already carrying their new name (a re-run after an interrupted
    one), and the titles that could not be found or written.
    """
    owned: dict[str, dict[str, Any]] = {}
    me = await spotify.current_user()
    for playlist in await manager.get_user_playlists():
        if playlist["owner_id"] == me.id:
            owned[playlist["name"]] = playlist

    renamed: list[Rename] = []
    already: list[str] = []
    failed: list[str] = []

    for rename in renames:
        if rename.new in owned:
            already.append(rename.new)
            continue
        live = owned.get(rename.old)
        if live is None:
            failed.append(rename.old)
            continue
        try:
            await spotify.playlist_change_details(live["id"], name=rename.new)
        except Exception as exc:  # noqa: BLE001 — one failure must not strand the rest
            logger.warning("Could not rename %r: %s", rename.old, exc)
            failed.append(rename.old)
            continue
        renamed.append(rename)
        if delay:
            await asyncio.sleep(delay)

    # Only the picks that actually moved, so a dry-run mapping never
    # rewrites the sidecar for playlists still carrying their old name.
    migrate_cover_picks(renamed, picks_path)
    logger.info(
        "Renamed %d playlist(s); %d already renamed, %d failed",
        len(renamed),
        len(already),
        len(failed),
    )
    return renamed, already, failed
