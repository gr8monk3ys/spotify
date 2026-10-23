"""The Discogs shelves joined to the liked library and the play log."""

from __future__ import annotations

import json

import pytest

from spotifyforge.core.curation import CurationTrack
from spotifyforge.core.history import Play
from spotifyforge.core.library import DISCOGS_SCHEMA, LibraryFileError
from spotifyforge.core.records import (
    ShelfRecord,
    join_records,
    read_discogs_shelves,
    report_dict,
)


def _song(track_id: str, artist: str, album: str, year: int | None = 2000) -> CurationTrack:
    return CurationTrack(
        id=track_id,
        uri=f"spotify:track:{track_id}",
        name=track_id,
        artist_ids=(artist.lower(),),
        artist_names=(artist,),
        release_year=year,
        popularity=50,
        album_id=album.lower(),
        album_name=album,
    )


def _songs(prefix: str, artist: str, album: str, n: int) -> list[CurationTrack]:
    return [_song(f"{prefix}{i}", artist, album) for i in range(n)]


def _record(shelf, artist, title, release_id=1, year=None) -> ShelfRecord:
    return ShelfRecord(shelf, (artist,), title, year, release_id)


def _play(artist: str, album: str, at: str = "2026-10-10T00:00:00+00:00") -> Play:
    return Play(at, "t", "t", (artist,), (artist.lower(),), album, None, 1000)


# ---------------------------------------------------------------------------
# Reading the file
# ---------------------------------------------------------------------------


def _item(release_id, title, artists, year=2000):
    return {
        "release_id": release_id,
        "master_id": None,
        "title": title,
        "artists": artists,
        "year": year,
        "formats": [],
        "added_at": "2026-01-01T00:00:00+00:00",
    }


def test_reads_both_shelves_collection_first(tmp_path):
    path = tmp_path / "discogs.json"
    doc = {
        "schema": DISCOGS_SCHEMA,
        "generated_at": "2026-08-26T00:00:00+00:00",
        "username": "u",
        "wantlist": [_item(9, "Wanted", ["Nobody"], 0)],
        "collection": [_item(1, "In Utero", ["Nirvana"]), _item(2, "", ["Untitled Band"])],
    }
    path.write_text(json.dumps(doc), encoding="utf-8")

    records = read_discogs_shelves(path)

    assert records == [
        ShelfRecord("collection", ("Nirvana",), "In Utero", 2000, 1),
        ShelfRecord("wantlist", ("Nobody",), "Wanted", None, 9),  # Discogs writes 0 for unknown
    ]


def test_missing_file_names_the_producing_command(tmp_path):
    with pytest.raises(LibraryFileError, match="discogs export"):
        read_discogs_shelves(tmp_path / "nope.json")


# ---------------------------------------------------------------------------
# The join
# ---------------------------------------------------------------------------


def test_editions_on_spotify_add_up_and_match_the_plain_record():
    tracks = [
        *_songs("a", "Nirvana", "In Utero", 2),
        *_songs("b", "Nirvana", "In Utero (20th Anniversary Remaster)", 3),
    ]
    report = join_records([_record("wantlist", "Nirvana", "In Utero")], tracks)

    (match,) = report.wanted_and_loved
    assert match.liked == 5
    assert match.album is not None
    assert match.album.title == "In Utero"  # the plainest edition's name


def test_a_reused_artist_name_and_a_leading_article_still_match():
    tracks = _songs("s", "The Strokes", "Is This It", 2)
    report = join_records([_record("collection", "Strokes (2)", "Is This It")], tracks)

    assert report.collection[0].liked == 2
    assert report.owned_not_streamed == []


def test_either_side_of_a_translated_title_matches():
    tracks = _songs("s", "Ichiko Aoba", "Scenery", 4)
    report = join_records([_record("wantlist", "Ichiko Aoba", "シーナリィ = Scenery")], tracks)

    assert report.wantlist[0].liked == 4


def test_two_copies_of_a_record_are_one_record():
    records = [
        _record("collection", "Mac DeMarco", "Salad Days", 1),
        _record("collection", "Mac DeMarco", "Salad Days", 2),
    ]
    report = join_records(records, _songs("m", "Mac DeMarco", "Salad Days", 3))

    (match,) = report.collection
    assert match.record.copies == 2
    assert report.discogs_rows == 2


def test_wanted_and_loved_ranks_by_liked_songs_then_plays():
    tracks = [
        *_songs("a", "A", "Few", 2),
        *_songs("b", "B", "Many", 6),
        *_songs("c", "C", "Tie", 2),
    ]
    records = [
        _record("wantlist", "A", "Few"),
        _record("wantlist", "B", "Many"),
        _record("wantlist", "C", "Tie"),
        _record("wantlist", "D", "Played Only"),
        _record("wantlist", "E", "Never"),
    ]
    plays = [_play("C", "Tie"), _play("D", "Played Only"), _play("D", "Played Only")]

    report = join_records(records, tracks, plays)

    assert [m.record.title for m in report.wanted_and_loved] == [
        "Many",
        "Tie",  # 2 liked and a play beats 2 liked
        "Few",
        "Played Only",  # no liked song, but played: streamed all the same
    ]
    assert report.plays == 3
    assert report.plays_since == "2026-10-10T00:00:00+00:00"


def test_owned_but_not_streamed_is_the_collection_with_no_liked_song_or_play():
    records = [
        _record("collection", "A", "Loved"),
        _record("collection", "B", "Shelved"),
        _record("collection", "C", "Played Once"),
    ]
    report = join_records(records, _songs("a", "A", "Loved", 1), [_play("C", "Played Once")])

    assert [m.record.title for m in report.owned_not_streamed] == ["Shelved"]


def test_loved_unshelved_needs_the_taste_pages_album_threshold():
    tracks = [
        *_songs("a", "A", "Three", 3),
        *_songs("b", "B", "Two", 2),
        *_songs("c", "C", "Owned", 5),
    ]
    report = join_records([_record("collection", "C", "Owned")], tracks)

    assert [(a.title, a.liked) for a in report.loved_unshelved] == [("Three", 3)]


def test_unmatched_records_show_the_suspect_joins_first():
    tracks = [*_songs("z", "Led Zeppelin", "Led Zeppelin IV (Remaster)", 4)]
    records = [
        _record("wantlist", "Nobody", "Obscure"),
        _record("collection", "Led Zeppelin", "Untitled"),
    ]

    report = join_records(records, tracks)

    first, second = report.unmatched
    assert first.record.title == "Untitled"
    assert first.artist_liked == 4
    assert first.artist_albums == ("Led Zeppelin IV (Remaster)",)
    assert second.record.title == "Obscure"
    assert second.artist_liked == 0


def test_report_dict_counts_match_rates_and_cuts_lists():
    tracks = [*_songs("a", "A", "One", 3), *_songs("b", "B", "Two", 3)]
    records = [
        _record("wantlist", "A", "One"),
        _record("wantlist", "B", "Two"),
        _record("wantlist", "C", "Missing"),
        _record("collection", "B", "Two"),
    ]

    data = report_dict(join_records(records, tracks), top=1)

    assert data["collection"] == {"records": 1, "matched": 1}
    assert data["wantlist"] == {"records": 3, "matched": 2}
    assert data["wanted_and_loved_count"] == 2
    assert len(data["wanted_and_loved"]) == 1
    assert data["unmatched_count"] == 1
    assert data["unmatched_examples"][0]["title"] == "Missing"
    assert data["unmatched_examples"][0]["shelf"] == "wantlist"
