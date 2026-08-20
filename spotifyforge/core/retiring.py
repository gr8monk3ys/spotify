"""Retire forged playlists the catalogue no longer plans.

Improving how tracks are assigned to genres leaves playlists behind:
a genre that used to hold forty songs, because every song carrying it
also carried three other genres, may hold four once each song belongs to
one playlist only. Those playlists stay live holding songs that now
belong elsewhere, which is the duplication the assignment fix was for.

Retiring one is ``playlist_unfollow`` — Spotify has no delete. The
playlist leaves the profile and library, and Spotify keeps it
recoverable from the web client for about ninety days.

The whole risk here is retiring something the user made by hand, which
no re-run can undo. So eligibility is never inferred from a name, a size
or an absence: a playlist qualifies only if its description is one this
code generated, matched against the templates in
:mod:`spotifyforge.core.curation` themselves. A playlist whose
description was typed by a person matches nothing and is never a
candidate, whatever else is true of it.
"""

from __future__ import annotations

import asyncio
import logging
import re
from functools import cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping

    from tekore import Spotify

logger = logging.getLogger(__name__)

_RETIRE_DELAY = 1.0  # one write per second, same pacing as forge


@cache
def _template_patterns() -> tuple[re.Pattern[str], ...]:
    """Every description this code can emit, as a matcher.

    Built from the templates rather than restated, so a new template
    cannot quietly make its own playlists unretirable — and, more
    importantly, so nothing else can accidentally start matching.
    """
    from spotifyforge.core.curation import (
        _DESCRIPTION_TEMPLATES,
        _FALLBACK_DESCRIPTION,
        _UNCLASSIFIED_DESCRIPTION,
        _UNCLASSIFIED_FALLBACK,
    )

    patterns = []
    for template in [
        *_DESCRIPTION_TEMPLATES,
        _FALLBACK_DESCRIPTION,
        _UNCLASSIFIED_DESCRIPTION,
        _UNCLASSIFIED_FALLBACK,
    ]:
        # Escape the literal wording, then re-open the two slots. A
        # description is also allowed the decade and dj-set suffixes, and
        # to be cut short at Spotify's 300-character limit.
        body = re.escape(template).replace(r"\{genre\}", ".+?").replace(r"\{artists\}", ".+?")
        patterns.append(re.compile(rf"^{body}", re.IGNORECASE))
    return tuple(patterns)


def was_forged(description: str) -> bool:
    """Whether *description* is one this code wrote."""
    text = (description or "").strip()
    return bool(text) and any(p.match(text) for p in _template_patterns())


def plan_retirements(
    live: Mapping[str, str],
    planned_titles: Collection[str],
) -> tuple[list[str], list[str]]:
    """Split the unplanned playlists into retirable and kept.

    *live* maps playlist name to its current description. Returns
    ``(retirable, kept)`` — the forged playlists the catalogue no longer
    plans, and everything else it will not touch.
    """
    planned = set(planned_titles)
    retirable: list[str] = []
    kept: list[str] = []
    unplanned = [name for name in live if name not in planned]
    for name in unplanned:
        (retirable if was_forged(live[name]) else kept).append(name)
    retirable.sort()
    kept.sort()
    logger.info(
        "%d unplanned playlist(s): %d forged and retirable, %d kept (not forged by this tool)",
        len(unplanned),
        len(retirable),
        len(kept),
    )
    return retirable, kept


async def retire_playlists(
    spotify: Spotify,
    ids_by_name: Mapping[str, str],
    names: Collection[str],
    delay: float = _RETIRE_DELAY,
) -> tuple[list[str], list[str]]:
    """Unfollow *names*. Returns ``(retired, failed)``."""
    retired: list[str] = []
    failed: list[str] = []
    for name in names:
        playlist_id = ids_by_name.get(name)
        if playlist_id is None:
            failed.append(name)
            continue
        try:
            await spotify.playlist_unfollow(playlist_id)
        except Exception as exc:  # noqa: BLE001 — one failure must not strand the rest
            logger.warning("Could not retire %r: %s", name, exc)
            failed.append(name)
            continue
        retired.append(name)
        if delay:
            await asyncio.sleep(delay)

    logger.info("Retired %d playlist(s); %d failed", len(retired), len(failed))
    return retired, failed
