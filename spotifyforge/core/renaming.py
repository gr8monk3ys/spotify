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
