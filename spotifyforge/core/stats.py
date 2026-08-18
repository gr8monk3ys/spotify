"""Measure whether the catalogue is actually growing.

Forging hundreds of playlists is half the goal; the other half is
followers, and Spotify shows follower counts one playlist at a time
with no history. Each run snapshots the account — profile followers
plus every owned playlist's follower count — into a local JSONL log,
so the next run can say what changed. Which niches convert is a
question only a time series can answer.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

import httpx
import tekore as tk

from spotifyforge.models.models import utc_now

if TYPE_CHECKING:
    from pathlib import Path

    from tekore import Spotify

logger = logging.getLogger(__name__)

# Follower counts come one playlist at a time; bounded like the curator
# scan so a 200-playlist account doesn't open hundreds of sockets.
_CONCURRENCY = 6


@dataclass
class PlaylistStat:
    """One owned playlist's audience at a moment in time."""

    id: str
    name: str
    followers: int
    tracks: int


@dataclass
class Snapshot:
    """The account's audience at a moment in time."""

    taken_at: str  # ISO 8601, UTC
    account_followers: int
    playlists: list[PlaylistStat] = field(default_factory=list)

    @property
    def playlist_followers(self) -> int:
        return sum(p.followers for p in self.playlists)

    @property
    def followed_playlists(self) -> int:
        return sum(1 for p in self.playlists if p.followers)


@dataclass
class Growth:
    """What changed between two snapshots."""

    since: str  # taken_at of the older snapshot
    account_delta: int
    playlist_delta: int
    movers: list[tuple[str, int]]  # (playlist name, follower delta), biggest first


async def take_snapshot(spotify: Spotify) -> Snapshot:
    """Read the account's current follower state (nothing is written).

    Playlists the user merely follows are excluded — their follower
    counts measure someone else's audience. A playlist whose read fails
    is skipped with a warning rather than recorded as zero, so a blip
    never shows up later as a fake follower drop.
    """
    me = await spotify.current_user()
    owned: list[Any] = []
    offset = 0
    while True:
        page = await spotify.playlists(me.id, limit=50, offset=offset)
        owned.extend(p for p in page.items or [] if p.owner and p.owner.id == me.id)
        offset += 50
        if page.next is None:
            break

    semaphore = asyncio.Semaphore(_CONCURRENCY)

    async def read(pl: Any) -> PlaylistStat | None:
        async with semaphore:
            try:
                # Ask for only the follower count; the full payload
                # carries 100 hydrated tracks to answer one number.
                data = await spotify.playlist(pl.id, fields="followers.total")
            except (tk.HTTPError, httpx.HTTPError) as exc:
                logger.warning("Could not read followers for %r: %s", pl.name, exc)
                return None
            followers = data.get("followers") if isinstance(data, dict) else None
            return PlaylistStat(
                id=pl.id,
                name=pl.name,
                followers=(followers or {}).get("total", 0),
                tracks=pl.tracks.total if pl.tracks else 0,
            )

    stats = [s for s in await asyncio.gather(*(read(p) for p in owned)) if s is not None]
    stats.sort(key=lambda s: (-s.followers, s.name))
    return Snapshot(
        taken_at=utc_now().isoformat(),
        account_followers=me.followers.total if me.followers else 0,
        playlists=stats,
    )


def history_path() -> Path:
    """Where snapshots accumulate: ``<db_path parent>/stats_history.jsonl``.

    A sidecar beside ``audio_features.json`` for the same reason — this
    is a plain time series with nothing to join against, and JSONL keeps
    every append independent of every earlier line.
    """
    from spotifyforge.config import settings

    return settings.db_path.parent / "stats_history.jsonl"


def append_snapshot(snapshot: Snapshot, path: Path | None = None) -> Path:
    target = path or history_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(snapshot)) + "\n")
    return target


def load_previous(path: Path | None = None) -> Snapshot | None:
    """The most recent stored snapshot, or ``None`` on a first run.

    A malformed line (a process killed mid-append) is skipped, never
    fatal — losing one historical point must not brick the command.
    """
    target = path or history_path()
    if not target.exists():
        return None
    last = None
    for line in target.read_text(encoding="utf-8").splitlines():
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("Skipping malformed stats line in %s", target)
            continue
        last = data
    if last is None:
        return None
    return Snapshot(
        taken_at=last["taken_at"],
        account_followers=last["account_followers"],
        playlists=[PlaylistStat(**p) for p in last["playlists"]],
    )


def growth_since(previous: Snapshot, current: Snapshot) -> Growth:
    """Deltas between two snapshots, matched by playlist id.

    A playlist absent from the older snapshot has no baseline and is not
    a mover — counting a new playlist's whole audience as growth would
    flatter every run that follows a forge.
    """
    before = {p.id: p for p in previous.playlists}
    movers = []
    for p in current.playlists:
        old = before.get(p.id)
        if old is not None and p.followers != old.followers:
            movers.append((p.name, p.followers - old.followers))
    movers.sort(key=lambda m: (-m[1], m[0]))
    return Growth(
        since=previous.taken_at,
        account_delta=current.account_followers - previous.account_followers,
        playlist_delta=current.playlist_followers - previous.playlist_followers,
        movers=movers,
    )
