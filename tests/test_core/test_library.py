"""Tests for saving Discogs-owned records to the Spotify library (core/library.py).

A false match here saves a stranger's record to a real account, so the
matcher is tested for what it refuses as much as for what it accepts.
"""

from __future__ import annotations

import json

import pytest

from spotifyforge.core.library import (
    DISCOGS_SCHEMA,
    LibraryFileError,
    OwnedRecord,
    _is_plain,
    find_album,
    key,
    normalise,
    read_discogs_collection,
    save_albums,
    saved_status,
)

FIXTURE = {
    "schema": DISCOGS_SCHEMA,
    "generated_at": "2026-08-26T00:00:00+00:00",
    "username": "u",
    "collection": [
        {
            "release_id": 1,
            "master_id": 2,
            "title": "In Utero",
            "artists": ["Nirvana"],
            "year": 1993,
            "formats": ["Vinyl"],
            "added_at": "2026-01-01T00:00:00+00:00",
        },
        {
            "release_id": 3,
            "master_id": None,
            "title": "Kid A",
            "artists": ["Radiohead"],
            "year": None,
            "formats": ["CD"],
            "added_at": "2026-01-02T00:00:00+00:00",
        },
    ],
    "wantlist": [
        {
            "release_id": 9,
            "master_id": 9,
            "title": "Wanted",
            "artists": ["Nobody"],
            "year": 2000,
            "formats": [],
            "added_at": "2026-01-03T00:00:00+00:00",
        }
    ],
}


def _write(tmp_path, doc):
    path = tmp_path / "discogs.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_reads_the_collection_and_ignores_the_wantlist(tmp_path):
    records = read_discogs_collection(_write(tmp_path, FIXTURE))
    assert records == [
        OwnedRecord(artist="Nirvana", title="In Utero", year=1993, release_id=1),
        OwnedRecord(artist="Radiohead", title="Kid A", year=None, release_id=3),
    ]


def test_rejects_a_file_with_the_wrong_schema(tmp_path):
    with pytest.raises(LibraryFileError, match="discogs/1"):
        read_discogs_collection(_write(tmp_path, {**FIXTURE, "schema": "discogs/2"}))


def test_missing_file_names_the_producing_command(tmp_path):
    with pytest.raises(LibraryFileError, match="discogs export"):
        read_discogs_collection(tmp_path / "nope.json")


def test_records_without_an_artist_are_skipped(tmp_path):
    doc = {**FIXTURE, "collection": [{**FIXTURE["collection"][0], "artists": []}]}
    assert read_discogs_collection(_write(tmp_path, doc)) == []


def test_normalisation_strips_edition_noise_and_articles():
    assert normalise("In Utero (Deluxe Edition)") == "in utero"
    assert normalise("OK Computer - Remastered 2011") == "ok computer"
    assert key("The Beatles", "Abbey Road") == ("beatles", "abbey road")


# `_is_plain` is how find_album picks the canonical release out of several
# editions of one record, and it holds an invariant that spans a package
# boundary: it is true exactly when `media_core.names.fold` and `normalise`
# agree, i.e. when there was no edition noise for `normalise` to strip. Nothing
# in media-core knows this repo asks that question, so it is pinned here.


@pytest.mark.parametrize(
    "title",
    [
        "In Utero",
        "Sgt. Pepper's Lonely Hearts Club Band",  # punctuation is not edition noise
        "Untitled #23",  # nor is a symbol
        "Kid A",
        "The The",
    ],
)
def test_is_plain_accepts_a_title_with_no_edition_noise(title):
    assert _is_plain(title) is True


@pytest.mark.parametrize(
    "title",
    [
        "In Utero (Deluxe Edition)",
        "OK Computer - Remastered 2011",
        "Blonde on Blonde [Mono]",
        "Nevermind - Super Deluxe",
        "Illmatic (Explicit)",
    ],
)
def test_is_plain_rejects_a_decorated_edition(title):
    assert _is_plain(title) is False


def test_is_plain_is_exactly_the_fold_equals_normalise_identity():
    """The invariant itself, stated against media-core's own two functions.

    If a future media-core changes `fold` or `normalise` independently, this is
    what catches it — the parametrized cases above would still pass while
    `find_album` silently started preferring the wrong edition.
    """
    from media_core.names import fold

    for title in ("In Utero", "In Utero (Deluxe Edition)", "Loveless (Remastered)", "AM", ""):
        assert _is_plain(title) == (fold(title) == normalise(title))


def _record(artist="Nirvana", title="In Utero"):
    return OwnedRecord(artist=artist, title=title, year=1993, release_id=1)


async def test_find_album_accepts_exactly_one_normalised_match(fake_spotify, client_for):
    fake_spotify.add_user("user1")
    fake_spotify.add_album("alb1", "In Utero (Deluxe Edition)", "nirv", "Nirvana")
    fake_spotify.add_album("alb2", "In Utero", "trib", "A Tribute Band")

    match = await find_album(client_for("user1"), _record())

    assert (match.album_id, match.album_name) == ("alb1", "In Utero (Deluxe Edition)")


async def test_find_album_refuses_zero_matches(fake_spotify, client_for):
    fake_spotify.add_user("user1")
    fake_spotify.add_album("alb1", "Nevermind", "nirv", "Nirvana")

    match = await find_album(client_for("user1"), _record())

    assert match.album_id is None and match.record == _record()


async def test_find_album_prefers_the_plain_edition_among_several(fake_spotify, client_for):
    """Spotify lists In Utero four times (plain, Deluxe, Super Deluxe,
    30th Anniversary). Those are one record, not four candidates; the
    plain title is the canonical one and is saved."""
    fake_spotify.add_user("user1")
    fake_spotify.add_album("alb1", "In Utero (Deluxe Edition)", "nirv", "Nirvana")
    fake_spotify.add_album("alb2", "In Utero", "nirv", "Nirvana")
    fake_spotify.add_album("alb3", "In Utero (Super Deluxe Edition)", "nirv", "Nirvana")

    assert (await find_album(client_for("user1"), _record())).album_id == "alb2"


async def test_find_album_refuses_several_editions_with_no_plain_one(fake_spotify, client_for):
    """Two decorated editions and no canonical title: picking one by
    position would be a guess written to a real account."""
    fake_spotify.add_user("user1")
    fake_spotify.add_album("alb1", "In Utero (Remastered)", "nirv", "Nirvana")
    fake_spotify.add_album("alb2", "In Utero (Deluxe Edition)", "nirv", "Nirvana")

    assert (await find_album(client_for("user1"), _record())).album_id is None


async def test_find_album_refuses_two_plain_editions(fake_spotify, client_for):
    fake_spotify.add_user("user1")
    fake_spotify.add_album("alb1", "In Utero", "nirv", "Nirvana")
    fake_spotify.add_album("alb2", "In Utero", "nirv2", "Nirvana")

    assert (await find_album(client_for("user1"), _record())).album_id is None


async def test_saved_status_reports_each_album(fake_spotify, client_for):
    fake_spotify.add_user("user1")
    fake_spotify.saved_albums["user1"] = ["alb1"]

    status = await saved_status(client_for("user1"), ["alb1", "alb2"])

    assert status == {"alb1": True, "alb2": False}


async def test_save_albums_batches_twenty_per_call(fake_spotify, client_for):
    fake_spotify.add_user("user1")
    ids = [f"alb{i}" for i in range(25)]

    saved = await save_albums(client_for("user1"), ids)

    assert saved == 25
    puts = [p for m, p in fake_spotify.requests if m == "PUT" and p == "/v1/me/albums"]
    assert len(puts) == 2
    assert fake_spotify.saved_albums["user1"] == ids


async def test_save_albums_with_nothing_to_save_makes_no_call(fake_spotify, client_for):
    fake_spotify.add_user("user1")
    assert await save_albums(client_for("user1"), []) == 0
    assert not [p for m, p in fake_spotify.requests if m == "PUT"]
