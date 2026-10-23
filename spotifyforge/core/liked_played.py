"""Liked against played: songs you play but never liked, and likes you no longer hear.

Two records say what gets played, and neither is enough alone. The local
log (:mod:`spotifyforge.core.history`) is exact, play by play, but it
only starts on the day ``history sync`` was first put on a timer.
Spotify's top tracks reach further back: ``short_term`` is about four
weeks, ``medium_term`` six months, ``long_term`` about a year. They also
page much deeper than the fifty the ``top_tracks`` tool shows; on
2026-10-22 this account's three lists ran to 528, 3,685 and 4,031 tracks.

The two lists ask different things of that evidence:

* **Played but not liked** wants repetition. A song is a candidate when
  the log has it at least :data:`MIN_PLAYS` times, or when it sits in the
  first :data:`TOP_HEAD` of a top list. Deeper in a list a rank may stand
  for a single play, which is no reason to like anything.
* **Liked but not heard lately** is a claim about absence, so it uses
  every rank read: a liked song is listed only when no top list and no
  logged play has it. Even then it is not "never played". The top lists
  are Spotify's ranking, not a play count, and the longest stops at about
  a year, so the claim is only "not in your top lists or your log".

A song is matched by track id, then ISRC, then title and primary artist
as edition de-duplication reads them, so playing the remaster counts for
the album cut you liked.

Everything here reads, except :func:`write_playlist`, which the CLI calls
only when asked to.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Hashable, Sequence
from dataclasses import dataclass
from datetime import date
from functools import partial
from typing import TYPE_CHECKING, Any

from spotifyforge.core.curation import (
    CurationEngine,
    CurationTrack,
    gather_bounded,
    song_key,
    track_song_key,
)
from spotifyforge.core.playlist_manager import CreatedPlaylist, PlaylistManager, extract_isrc

if TYPE_CHECKING:
    from tekore import Spotify

    from spotifyforge.core.history import Play

RANGES: tuple[str, ...] = ("short_term", "medium_term", "long_term")
# How far back each list reaches, as Spotify describes it.
RANGE_SPANS = {
    "short_term": "about 4 weeks",
    "medium_term": "about 6 months",
    "long_term": "about a year",
}

# Plays in the log that make a song a candidate to like. Over the log's
# first fortnight 126 of 154 songs were played once and 10 three times or
# more: three separates a habit from a shuffle.
MIN_PLAYS = 3
# Ranks of a top list that nominate a song on their own.
TOP_HEAD = 50
# The deepest rank read from each list. Spotify pages them fifty at a
# time, so this bounds one list at a hundred requests.
MAX_DEPTH = 5000
_TOP_PAGE = 50


@dataclass(frozen=True)
class Heard:
    """A track one of the records says was played."""

    track_id: str
    name: str
    artists: tuple[str, ...]
    isrc: str | None = None


@dataclass(frozen=True)
class TopList:
    """One of Spotify's top-track lists, in rank order."""

    time_range: str
    tracks: tuple[Heard, ...]


@dataclass(frozen=True)
class Unliked:
    """A song you keep playing that is not among your liked songs."""

    track_id: str
    name: str
    artists: str
    plays: int  # in the log
    top_ranks: tuple[tuple[str, int], ...]  # (time range, rank from 1), heads only


@dataclass(frozen=True)
class Unheard:
    """A liked song that neither record has."""

    track_id: str
    uri: str
    name: str
    artists: str
    album: str


@dataclass(frozen=True)
class LikedVsPlayed:
    """Both lists, with the coverage of the records they were drawn from."""

    liked: int
    log_plays: int
    log_first: str | None
    log_last: str | None
    top_lists: tuple[tuple[str, int], ...]  # (time range, tracks read)
    min_plays: int
    played_not_liked: tuple[Unliked, ...]
    liked_not_heard: tuple[Unheard, ...]  # liked longest ago first

    @property
    def log_since(self) -> str | None:
        """The day the log starts, ``YYYY-MM-DD``."""
        return self.log_first[:10] if self.log_first else None

    @property
    def not_heard_wording(self) -> str:
        """What "not heard lately" means here, in words that do not overclaim."""
        if self.log_since:
            return f"not in your top lists or your log since {self.log_since}"
        return "not in your top lists (the log is empty)"


def _keys(track_id: str, isrc: str | None, name: str, artists: Sequence[str]) -> list[Hashable]:
    keys: list[Hashable] = [("id", track_id)]
    if isrc:
        keys.append(("isrc", isrc.upper()))
    # Without an artist the key is a bare title, and "Intro" would match
    # every intro in the library.
    if name and artists:
        keys.append(("song", song_key(name, artists[0])))
    return keys


def _song(heard: Heard) -> Hashable:
    return song_key(heard.name, heard.artists[0]) if heard.artists else heard.track_id


def compare(
    liked: Sequence[CurationTrack],
    plays: Sequence[Play],
    top_lists: Sequence[TopList],
    min_plays: int = MIN_PLAYS,
    head: int = TOP_HEAD,
) -> LikedVsPlayed:
    """Split the records into the two lists. *liked* is in library order, newest first."""
    liked_keys: set[Hashable] = set()
    for track in liked:
        liked_keys.update(_keys(track.id, track.isrc, track.name, track.artist_names))

    # Group by song, so two editions of one song played twice each read
    # as one song played four times.
    label: dict[Hashable, Heard] = {}
    plays_by_song: Counter[Hashable] = Counter()
    ranks: dict[Hashable, dict[str, int]] = defaultdict(dict)
    unliked: set[Hashable] = set()
    for play in plays:
        heard = Heard(play.track_id, play.name, play.artists, play.isrc)
        song = _song(heard)
        label.setdefault(song, heard)
        plays_by_song[song] += 1
        if not liked_keys.intersection(
            _keys(heard.track_id, heard.isrc, heard.name, heard.artists)
        ):
            unliked.add(song)
    for top in top_lists:
        for rank, heard in enumerate(top.tracks[:head], start=1):
            song = _song(heard)
            label.setdefault(song, heard)
            ranks[song].setdefault(top.time_range, rank)
            if not liked_keys.intersection(
                _keys(heard.track_id, heard.isrc, heard.name, heard.artists)
            ):
                unliked.add(song)

    candidates = [
        Unliked(
            track_id=label[song].track_id,
            name=label[song].name,
            artists=", ".join(label[song].artists),
            plays=plays_by_song[song],
            top_ranks=tuple((r, ranks[song][r]) for r in RANGES if r in ranks[song]),
        )
        for song in unliked
        if plays_by_song[song] >= min_plays or ranks.get(song)
    ]
    candidates.sort(
        key=lambda c: (
            -c.plays,
            min((rank for _, rank in c.top_ranks), default=head + 1),
            c.name.casefold(),
        )
    )

    heard_keys: set[Hashable] = set()
    for play in plays:
        heard_keys.update(_keys(play.track_id, play.isrc, play.name, play.artists))
    for top in top_lists:
        for heard in top.tracks:
            heard_keys.update(_keys(heard.track_id, heard.isrc, heard.name, heard.artists))

    unheard: list[Unheard] = []
    listed: set[tuple[str, str]] = set()
    for track in reversed(liked):  # oldest like first: the longest forgotten
        if heard_keys.intersection(_keys(track.id, track.isrc, track.name, track.artist_names)):
            continue
        song_id = track_song_key(track)
        if song_id in listed:
            continue  # a second edition of a song already listed
        listed.add(song_id)
        unheard.append(
            Unheard(
                track_id=track.id,
                uri=track.uri,
                name=track.name,
                artists=", ".join(track.artist_names),
                album=track.album_name,
            )
        )

    return LikedVsPlayed(
        liked=len(liked),
        log_plays=len(plays),
        log_first=plays[0].played_at if plays else None,
        log_last=plays[-1].played_at if plays else None,
        top_lists=tuple((top.time_range, len(top.tracks)) for top in top_lists),
        min_plays=min_plays,
        played_not_liked=tuple(candidates),
        liked_not_heard=tuple(unheard),
    )


def _heard(track: Any) -> Heard:
    return Heard(
        track_id=track.id,
        name=track.name or "",
        artists=tuple(a.name for a in track.artists or () if a.name),
        isrc=extract_isrc(track),
    )


async def read_top_list(sp: Spotify, time_range: str, depth: int = MAX_DEPTH) -> TopList:
    """Every rank of one top-track list Spotify will page through, up to *depth*.

    The same endpoint the ``top_tracks`` tool reads, walked past its
    first page: the first reports the list's length, and the rest are
    fetched concurrently.
    """
    first = await sp.current_user_top_tracks(time_range=time_range, limit=_TOP_PAGE)
    pages = [first]
    pages += await gather_bounded(
        [
            partial(sp.current_user_top_tracks, time_range=time_range, limit=_TOP_PAGE, offset=o)
            for o in range(_TOP_PAGE, min(first.total or 0, depth), _TOP_PAGE)
        ]
    )
    tracks = tuple(
        _heard(track)
        for page in pages
        if page is not None
        for track in page.items or ()
        if track is not None and track.id
    )
    return TopList(time_range, tracks[:depth])


async def liked_vs_played(
    sp: Spotify,
    plays: Sequence[Play],
    min_plays: int = MIN_PLAYS,
    depth: int = MAX_DEPTH,
) -> LikedVsPlayed:
    """Read the liked library and the three top lists, and compare them with *plays*.

    The liked library is read raw, before edition de-duplication, so a
    played edition counts as liked whichever one the library kept.
    """
    liked = await CurationEngine(sp).fetch_liked()
    tops = [await read_top_list(sp, time_range, depth) for time_range in RANGES]
    return compare(liked, plays, tops, min_plays)


def playlist_name(today: date) -> str:
    return f"Liked, not heard lately ({today.isoformat()})"


async def write_playlist(sp: Spotify, report: LikedVsPlayed, today: date) -> CreatedPlaylist:
    """Put the liked-but-not-heard list into a new private playlist, oldest like first.

    Always a new playlist: each one is a dated snapshot of what the
    records said that day, not a list kept up to date.
    """
    log = f"my listening log since {report.log_since}" if report.log_since else "my listening log"
    manager = PlaylistManager(sp)
    created = await manager.create_playlist(
        name=playlist_name(today),
        description=(
            f"Liked songs that were in none of my Spotify top lists and not in {log}, "
            f"as of {today.isoformat()}. Oldest like first. Made by spotifyforge."
        ),
        public=False,
    )
    await manager.add_tracks(created.spotify_id, [t.uri for t in report.liked_not_heard])
    return created
