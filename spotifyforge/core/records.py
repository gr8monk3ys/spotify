"""Records against streams: the Discogs shelves joined to the liked library.

The collection and the wantlist arrive as ``discogs.json`` in the shared
music directory (written by ``discogs export`` in its own repo); nothing
here talks to Discogs, and nothing here writes to it. Three lists come
out of the join:

* **wanted and already loved**: wantlist albums with songs liked (or
  played) on Spotify, most liked first. Buy these first;
* **owned but not streamed**: records on the shelf with no liked song
  and no logged play;
* **loved but not on either shelf**: albums with as many liked songs as
  the taste page needs to list an album, neither owned nor wanted.
  Candidates for the wantlist, reported and never added.

The join is offline. A record matches an album in the liked library when
artist and title agree after ``media_core.names.key``, which drops edition
noise ("(Deluxe)", "- Remastered 2011", a trailing bracket, Discogs's
"(2)" after a reused artist name) and a leading article. Editions of one
album on Spotify fall onto one key, so their liked songs add up, and the
songs are already one per song (``dedupe_versions``) so a song liked on
two editions counts once.

A record that matches nothing is either not streamed or badly joined, and
the join cannot tell which. So unmatched records carry how many songs the
library has by the same artist, and that artist's most liked album titles:
an artist with 50 liked songs and no matching album is a title the rules
did not reconcile ("Untitled" against "Led Zeppelin IV"), not a record
never played.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

from media_core.export import ExportError, read_export
from media_core.names import fold, key, normalise, strip_article

from spotifyforge.core.library import DISCOGS_SCHEMA, LibraryFileError
from spotifyforge.core.taste_page import _MIN_ALBUM_SONGS

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from spotifyforge.core.curation import CurationTrack
    from spotifyforge.core.history import Play

Shelf = Literal["collection", "wantlist"]

# Discogs gives a title in two scripts or languages as "Scenery = シーナリィ";
# Spotify carries one of them. Each side of the "=" is tried as the title.
_TRANSLATION = re.compile(r"\s*=\s*")

# Album titles shown for an unmatched record's artist, most liked first.
_ARTIST_ALBUMS = 3


@dataclass(frozen=True)
class ShelfRecord:
    """One record on a Discogs shelf; ``copies`` counts rows merged into it."""

    shelf: Shelf
    artists: tuple[str, ...]
    title: str
    year: int | None
    release_id: int
    copies: int = 1

    @property
    def artist(self) -> str:
        return self.artists[0]


@dataclass(frozen=True)
class AlbumStreams:
    """One album in the liked library, editions merged."""

    artist: str
    title: str
    year: int | None
    liked: int
    plays: int


@dataclass(frozen=True)
class RecordMatch:
    record: ShelfRecord
    album: AlbumStreams | None  # None: no liked song on any edition
    plays: int  # plays of the album in the listening log, liked or not
    artist_liked: int  # liked songs by the record's artist, on any album
    artist_albums: tuple[str, ...] = ()  # filled when nothing matched

    @property
    def liked(self) -> int:
        return self.album.liked if self.album else 0

    @property
    def matched(self) -> bool:
        return self.liked > 0 or self.plays > 0


@dataclass(frozen=True)
class RecordsReport:
    collection: tuple[RecordMatch, ...]
    wantlist: tuple[RecordMatch, ...]
    loved_unshelved: tuple[AlbumStreams, ...]
    discogs_rows: int  # rows in the file, before duplicates were merged
    plays: int  # plays in the listening log
    plays_since: str | None  # the log's first play

    @property
    def wanted_and_loved(self) -> list[RecordMatch]:
        found = [m for m in self.wantlist if m.matched]
        return sorted(found, key=lambda m: (-m.liked, -m.plays, m.record.title.casefold()))

    @property
    def owned_not_streamed(self) -> list[RecordMatch]:
        return [m for m in self.collection if not m.matched]

    @property
    def unmatched(self) -> list[RecordMatch]:
        """Records that met nothing on Spotify, the suspect joins first.

        Suspect means the library has songs by the artist: the record is
        more likely a title the rules missed than one never streamed.
        """
        missed = [m for m in (*self.collection, *self.wantlist) if not m.matched]
        return sorted(missed, key=lambda m: (-m.artist_liked, m.record.artist.casefold()))


def read_discogs_shelves(path: Path) -> list[ShelfRecord]:
    """Both shelves of a ``discogs/1`` file, the collection first.

    Unlike :func:`spotifyforge.core.library.read_discogs_collection`, the
    wantlist is read too: here it is only compared, never saved, so it
    cannot make the library claim records the user does not own.
    """
    if not path.exists():
        raise LibraryFileError(f"{path} does not exist — run `discogs export` first.")
    try:
        document = read_export(path, DISCOGS_SCHEMA)
    except ExportError as exc:
        raise LibraryFileError(f"{exc} It is produced by `discogs export`.") from exc

    records = []
    shelves: tuple[Shelf, ...] = ("collection", "wantlist")
    for shelf in shelves:
        for item in document.get(shelf) or []:
            artists = tuple(str(a) for a in item.get("artists") or [] if a)
            if not artists or not item.get("title"):
                continue
            records.append(
                ShelfRecord(
                    shelf=shelf,
                    artists=artists,
                    title=str(item["title"]),
                    # Discogs writes 0 for an unknown year.
                    year=item.get("year") or None,
                    release_id=int(item["release_id"]),
                )
            )
    return records


def _record_keys(record: ShelfRecord) -> list[tuple[str, str]]:
    titles = [record.title, *_TRANSLATION.split(record.title)]
    return list(
        dict.fromkeys(key(artist, title) for artist in record.artists for title in titles if title)
    )


def _merge_copies(records: Sequence[ShelfRecord]) -> list[ShelfRecord]:
    """One row per record per shelf: two copies of a pressing are one album."""
    merged: dict[tuple[Shelf, tuple[str, str]], ShelfRecord] = {}
    for record in records:
        slot = (record.shelf, key(record.artist, record.title))
        first = merged.get(slot)
        merged[slot] = record if first is None else replace(first, copies=first.copies + 1)
    return list(merged.values())


def _album_title(names: Counter[str]) -> str:
    """The plainest edition's name: one without edition noise, else the most liked."""
    plain = [name for name in names if fold(name) == normalise(name)]
    return max(plain or names, key=lambda name: (names[name], -len(name)))


def join_records(
    records: Sequence[ShelfRecord],
    tracks: Sequence[CurationTrack],
    plays: Sequence[Play] = (),
) -> RecordsReport:
    """Join Discogs *records* to the liked *tracks* (deduplicated) and the play log."""
    by_album: dict[tuple[str, str], list[CurationTrack]] = defaultdict(list)
    by_artist: dict[str, Counter[str]] = defaultdict(Counter)
    for track in tracks:
        if track.artist_names and track.album_name:
            by_album[key(track.artist_names[0], track.album_name)].append(track)
            by_artist[strip_article(track.artist_names[0])][track.album_name] += 1

    played: Counter[tuple[str, str]] = Counter(
        key(play.artists[0], play.album) for play in plays if play.artists and play.album
    )

    albums: dict[tuple[str, str], AlbumStreams] = {}
    for album_key, songs in by_album.items():
        years = [t.release_year for t in songs if t.release_year]
        albums[album_key] = AlbumStreams(
            artist=songs[0].artist_names[0],
            title=_album_title(Counter(t.album_name for t in songs)),
            year=min(years) if years else None,
            liked=len(songs),
            plays=played[album_key],
        )

    shelved: set[tuple[str, str]] = set()

    def match(record: ShelfRecord) -> RecordMatch:
        keys = _record_keys(record)
        shelved.update(keys)
        album = next((albums[k] for k in keys if k in albums), None)
        if album is not None:
            artist_liked = sum(by_artist[strip_article(album.artist)].values())
            return RecordMatch(record, album, album.plays, artist_liked)
        artist_counts: Counter[str] = Counter()
        for artist in record.artists:
            artist_counts.update(by_artist.get(strip_article(artist), Counter()))
        return RecordMatch(
            record,
            None,
            sum(played[k] for k in keys),
            sum(artist_counts.values()),
            tuple(name for name, _ in artist_counts.most_common(_ARTIST_ALBUMS)),
        )

    merged = _merge_copies(records)
    collection = tuple(match(r) for r in merged if r.shelf == "collection")
    wantlist = tuple(match(r) for r in merged if r.shelf == "wantlist")

    loved = sorted(
        (a for k, a in albums.items() if k not in shelved and a.liked >= _MIN_ALBUM_SONGS),
        key=lambda a: (-a.liked, -a.plays, a.title.casefold()),
    )
    return RecordsReport(
        collection=collection,
        wantlist=wantlist,
        loved_unshelved=tuple(loved),
        discogs_rows=len(records),
        plays=len(plays),
        plays_since=plays[0].played_at if plays else None,
    )


def _match_dict(m: RecordMatch) -> dict[str, Any]:
    out: dict[str, Any] = {
        "artist": m.record.artist,
        "title": m.record.title,
        "year": m.record.year,
        "discogs_release_id": m.record.release_id,
        "liked_songs": m.liked,
        "plays": m.plays,
    }
    if m.album is not None:
        out["spotify_album"] = m.album.title
    if not m.matched:
        out["shelf"] = m.record.shelf
        out["artist_liked_songs"] = m.artist_liked
        out["artist_liked_albums"] = list(m.artist_albums)
    if m.record.copies > 1:
        out["copies"] = m.record.copies
    return out


def report_dict(report: RecordsReport, top: int = 20) -> dict[str, Any]:
    """The report as plain data; long lists cut to *top*, each with its full count beside it."""
    unmatched = report.unmatched
    return {
        "discogs_rows": report.discogs_rows,
        "collection": {
            "records": len(report.collection),
            "matched": sum(1 for m in report.collection if m.matched),
        },
        "wantlist": {
            "records": len(report.wantlist),
            "matched": sum(1 for m in report.wantlist if m.matched),
        },
        "plays_logged": report.plays,
        "plays_since": report.plays_since,
        "wanted_and_loved_count": len(report.wanted_and_loved),
        "wanted_and_loved": [_match_dict(m) for m in report.wanted_and_loved[:top]],
        "owned_not_streamed": [_match_dict(m) for m in report.owned_not_streamed],
        "loved_unshelved_count": len(report.loved_unshelved),
        "loved_unshelved": [
            {
                "artist": a.artist,
                "title": a.title,
                "year": a.year,
                "liked_songs": a.liked,
                "plays": a.plays,
            }
            for a in report.loved_unshelved[:top]
        ],
        "unmatched_count": len(unmatched),
        "unmatched_by_an_artist_with_liked_songs": sum(1 for m in unmatched if m.artist_liked),
        "unmatched_examples": [_match_dict(m) for m in unmatched[:top]],
    }
