"""A listening history that outlives Spotify's fifty-play window.

``GET /me/player/recently-played`` returns the last fifty plays and
nothing older; there is no endpoint that hands the rest back later. So
the only way to have a history is to keep one: poll before fifty new
plays have gone by, and append what is new to a local file.

The file is JSON Lines, one play per line, oldest first. Appending is
the only write, so an interrupted run can at worst leave a short file,
never a corrupt one, and a re-run picks up where the last line stops.

Fifty plays is roughly two and a half hours of music, so the poll
should run about hourly. A run that finds all fifty plays are new
cannot tell whether a fifty-first was missed, and says so
(:attr:`SyncResult.possible_gap`) rather than letting the history
quietly claim to be complete.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from spotifyforge.config import sidecar_path
from spotifyforge.core.playlist_manager import extract_isrc

if TYPE_CHECKING:
    from tekore import Spotify

logger = logging.getLogger(__name__)

# Spotify's maximum for the endpoint, and all it remembers.
WINDOW = 50


@dataclass(frozen=True)
class Play:
    """One play of one track."""

    played_at: str  # ISO 8601, UTC
    track_id: str
    name: str
    artists: tuple[str, ...]
    artist_ids: tuple[str, ...]
    album: str
    album_id: str | None
    duration_ms: int
    isrc: str | None = None
    # What it was played from (a playlist, album or artist URI), when Spotify says.
    context: str | None = None

    @property
    def at(self) -> datetime:
        return datetime.fromisoformat(self.played_at)


@dataclass(frozen=True)
class SyncResult:
    """What one poll added."""

    added: int
    total: int
    # Every play in the window was new, so plays older than the window may
    # have been lost between this run and the previous one.
    possible_gap: bool
    path: Path


def history_path() -> Path:
    """Where the history lives: ``<data dir>/listening_history.jsonl``."""
    return sidecar_path("listening_history.jsonl")


def _play_from_item(item: Any) -> Play | None:
    track = item.track
    if track is None or track.id is None:
        return None  # local files and removed tracks have no id to key on
    album = getattr(track, "album", None)
    context = getattr(item, "context", None)
    return Play(
        played_at=item.played_at.astimezone(UTC).isoformat(),
        track_id=track.id,
        name=track.name or "",
        artists=tuple(a.name for a in track.artists or ()),
        artist_ids=tuple(a.id for a in track.artists or () if a.id),
        album=album.name if album else "",
        album_id=album.id if album else None,
        duration_ms=track.duration_ms or 0,
        isrc=extract_isrc(track),
        context=context.uri if context is not None else None,
    )


def load_plays(path: Path | None = None, since: datetime | None = None) -> list[Play]:
    """Read the history, oldest first; optionally only plays at or after *since*."""
    target = path or history_path()
    if not target.exists():
        return []
    plays: list[Play] = []
    with target.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                # A run killed mid-write leaves at most one torn last line.
                logger.warning("Skipping an unreadable line in %s", target)
                continue
            raw["artists"] = tuple(raw.get("artists") or ())
            raw["artist_ids"] = tuple(raw.get("artist_ids") or ())
            play = Play(**raw)
            if since is None or play.at >= since:
                plays.append(play)
    return plays


async def sync_history(spotify: Spotify, path: Path | None = None) -> SyncResult:
    """Append the plays Spotify remembers that the history does not have yet."""
    target = path or history_path()
    known = load_plays(target)
    latest = known[-1].at if known else None

    paging = await spotify.playback_recently_played(limit=WINDOW)
    window = [p for p in map(_play_from_item, paging.items or ()) if p is not None]
    fresh = sorted((p for p in window if latest is None or p.at > latest), key=lambda p: p.at)

    if fresh:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8", newline="\n") as handle:
            for play in fresh:
                handle.write(json.dumps(asdict(play), ensure_ascii=False) + "\n")

    possible_gap = latest is not None and len(window) >= WINDOW and len(fresh) == len(window)
    if possible_gap:
        logger.warning(
            "All %d plays in Spotify's window were new; older plays may have been missed", WINDOW
        )
    return SyncResult(
        added=len(fresh), total=len(known) + len(fresh), possible_gap=possible_gap, path=target
    )


@dataclass(frozen=True)
class Summary:
    """The history over a period, counted."""

    plays: int
    minutes: int
    distinct_tracks: int
    first: str | None
    last: str | None
    top_tracks: list[tuple[str, str, int]]  # (track, artists, plays)
    top_artists: list[tuple[str, int]]
    top_contexts: list[tuple[str, int]]  # (context URI, plays)


def summarise(plays: list[Play], top: int = 10) -> Summary:
    """Count *plays* by track, artist and what they were played from."""
    by_track: Counter[str] = Counter(p.track_id for p in plays)
    label = {p.track_id: (p.name, ", ".join(p.artists)) for p in plays}
    by_artist: Counter[str] = Counter(p.artists[0] for p in plays if p.artists)
    by_context: Counter[str] = Counter(p.context for p in plays if p.context)
    return Summary(
        plays=len(plays),
        minutes=round(sum(p.duration_ms for p in plays) / 60_000),
        distinct_tracks=len(by_track),
        first=plays[0].played_at if plays else None,
        last=plays[-1].played_at if plays else None,
        top_tracks=[(*label[tid], n) for tid, n in by_track.most_common(top)],
        top_artists=by_artist.most_common(top),
        top_contexts=by_context.most_common(top),
    )


# Below this many days of history, "never played" mostly means "not yet".
MIN_DAYS_TO_JUDGE = 28


@dataclass(frozen=True)
class PlaylistUsage:
    """Where the plays in a period came from, against the playlists you own."""

    plays: int
    days_covered: int
    first: str | None
    last: str | None
    from_own_playlists: int
    from_other_playlists: int  # followed, editorial, or made for you by Spotify
    from_albums: int
    from_artists: int
    from_elsewhere: int  # liked songs, search, queue: Spotify reports no context
    played: list[tuple[str, str, int]]  # (playlist id, name, plays), most played first
    unplayed: list[tuple[str, str]]  # (playlist id, name)

    @property
    def long_enough_to_judge(self) -> bool:
        return self.days_covered >= MIN_DAYS_TO_JUDGE


def playlist_usage(plays: list[Play], owned: Mapping[str, str]) -> PlaylistUsage:
    """Count *plays* by source and split *owned* (id -> name) into played and not.

    A play counts for a playlist only when Spotify says it was started
    from that playlist. A song that sits in one of your playlists but was
    played from Liked Songs is not evidence the playlist is used.
    """
    by_playlist: Counter[str] = Counter()
    kinds: Counter[str] = Counter()
    for play in plays:
        parts = (play.context or "").split(":")
        kind = parts[1] if len(parts) == 3 and parts[0] == "spotify" else "none"
        if kind == "playlist":
            by_playlist[parts[2]] += 1
            kind = "own" if parts[2] in owned else "other"
        kinds[kind] += 1

    played = sorted(
        ((pid, owned[pid], count) for pid, count in by_playlist.items() if pid in owned),
        key=lambda row: (-row[2], row[1]),
    )
    span = (plays[-1].at - plays[0].at).days + 1 if plays else 0
    return PlaylistUsage(
        plays=len(plays),
        days_covered=span,
        first=plays[0].played_at if plays else None,
        last=plays[-1].played_at if plays else None,
        from_own_playlists=kinds["own"],
        from_other_playlists=kinds["other"],
        from_albums=kinds["album"],
        from_artists=kinds["artist"],
        from_elsewhere=len(plays)
        - kinds["own"]
        - kinds["other"]
        - kinds["album"]
        - kinds["artist"],
        played=played,
        unplayed=sorted(
            ((pid, name) for pid, name in owned.items() if pid not in by_playlist),
            key=lambda row: row[1],
        ),
    )
