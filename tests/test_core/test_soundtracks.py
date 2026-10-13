"""Matching favourite films to their soundtrack albums, and writing the playlist."""

from __future__ import annotations

import json

import pytest

from spotifyforge.core.soundtracks import (
    Film,
    FilmsFileError,
    build_playlist_tracks,
    find_soundtrack,
    find_soundtracks,
    is_soundtrack_of,
    read_favourite_films,
    write_playlist,
)

PERSONA = Film("Persona", 1966, 5.0)
COCO = Film("Coco", 2017, 4.5)


# The cases below are the real search results that shaped the rules.
@pytest.mark.parametrize(
    ("album", "year", "film"),
    [
        ("Coco (Original Motion Picture Soundtrack)", 2017, COCO),
        ("Coco (Colonna Sonora Originale)", 2017, COCO),
        ("Aftersun (Original Motion Picture Soundtrack)", 2023, Film("Aftersun", 2022, 4.5)),
        (
            "When Harry Met Sally... Music From The Motion Picture",
            1989,
            Film("When Harry Met Sally...", 1989, 4.5),
        ),
        ("La notte (Colonna sonora originale)", 1961, Film("La Notte", 1961, 4.5)),
        ("Shrek 2 (Original Motion Picture Score)", 2004, Film("Shrek 2", 2004, 4.5)),
        ("NO OTHER CHOICE (Original Soundtrack)", 2026, Film("No Other Choice", 2025, 4.5)),
    ],
)
def test_a_films_own_soundtrack_is_accepted(album, year, film):
    assert is_soundtrack_of(album, year, film)


@pytest.mark.parametrize(
    ("album", "year", "film", "why"),
    [
        ("Persona ORIGINAL SOUNDTRACK", 2026, PERSONA, "sixty years late: a video game"),
        ("PERSONA5 ORIGINAL SOUNDTRACK", 2017, PERSONA, "a different title"),
        (
            "Oppenheimer Soundtrack (LoFi Edition)",
            2023,
            Film("Oppenheimer", 2023, 4.5),
            "an extra word that is not soundtrack boilerplate",
        ),
        (
            "The Texas Chain Saw Massacre (Original Game Soundtrack)",
            2023,
            Film("The Texas Chain Saw Massacre", 1974, 4.5),
            "a game",
        ),
        ("Cocoon (Original Motion Picture Soundtrack)", 1985, COCO, "title is only a prefix"),
        ("Coco", 2017, COCO, "nothing says it is a soundtrack"),
        ("Coco (Deluxe)", 2017, COCO, "nothing says it is a soundtrack"),
        (
            "Sunset Boulevard (Original Motion Picture Score)",
            2002,
            Film("Sunset Boulevard", 1950, 5.0),
            "a reissue decades later is refused with the rest",
        ),
        ("Coco (Original Motion Picture Soundtrack)", None, COCO, "no release year to check"),
        ("Coco (Original Motion Picture Soundtrack)", 2016, COCO, "released before the film"),
    ],
)
def test_everything_else_is_refused(album, year, film, why):
    assert not is_soundtrack_of(album, year, film), why


def _letterboxd(tmp_path, films) -> object:
    path = tmp_path / "letterboxd.json"
    path.write_text(
        json.dumps(
            {
                "schema": "letterboxd/1",
                "generated_at": "2026-08-30T00:00:00+00:00",
                "username": "someone",
                "films": films,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_read_favourites_keeps_the_highly_rated_best_first(tmp_path):
    path = _letterboxd(
        tmp_path,
        [
            {"title": "Fine", "year": 2001, "rating": 3.5},
            {"title": "Great", "year": 2002, "rating": 4.5},
            {"title": "Best", "year": 2003, "rating": 5.0},
            {"title": "Also Great", "year": 2004, "rating": 4.5},
            {"title": "Unrated", "year": 2005, "rating": None},
        ],
    )

    films = read_favourite_films(path, min_rating=4.5)

    assert [f.title for f in films] == ["Best", "Great", "Also Great"]


def test_a_missing_or_foreign_file_is_named(tmp_path):
    with pytest.raises(FilmsFileError, match="letterboxd export"):
        read_favourite_films(tmp_path / "nope.json")

    other = tmp_path / "other.json"
    other.write_text(json.dumps({"schema": "discogs/1", "films": []}), encoding="utf-8")
    with pytest.raises(FilmsFileError):
        read_favourite_films(other)


async def test_find_prefers_the_fullest_album_of_the_same_name(fake_spotify, client_for):
    fake = fake_spotify
    fake.add_user("user1")
    fake.add_album(
        "single", "Mulholland Drive (Original Motion Picture Soundtrack)", release_date="2001-10-09"
    )
    fake.add_album(
        "full", "Mulholland Drive (Original Motion Picture Soundtrack)", release_date="2001-10-09"
    )
    fake.albums["single"]["total_tracks"] = 1
    fake.albums["full"]["total_tracks"] = 17
    fake.add_album("game", "Mulholland Drive (Original Game Soundtrack)", release_date="2001-01-01")

    match = await find_soundtrack(client_for(), Film("Mulholland Drive", 2001, 5.0))

    assert match.album_id == "full"
    assert match.artist == "Artist One"


async def test_a_film_with_no_defensible_album_is_left_unmatched(fake_spotify, client_for):
    fake = fake_spotify
    fake.add_user("user1")
    fake.add_album("game", "Persona ORIGINAL SOUNDTRACK", release_date="2026-01-01")

    matches = await find_soundtracks(client_for(), [PERSONA])

    assert [m.album_id for m in matches] == [None]
    assert matches[0].film is PERSONA


async def test_playlist_takes_the_opening_tracks_of_each_soundtrack(fake_spotify, client_for):
    fake = fake_spotify
    fake.add_user("user1")
    for album, tracks in (("a", ["a1", "a2", "a3"]), ("b", ["b1", "b2"])):
        fake.add_album(album, f"{album} (Original Soundtrack)")
        for track_id in tracks:
            fake.add_track(track_id, album_id=album)
        fake.album_tracks[album] = tracks
    sp = client_for()
    matches = await find_soundtracks(sp, [])
    assert matches == []

    from spotifyforge.core.soundtracks import SoundtrackMatch

    picked = [
        SoundtrackMatch(Film("A", 2000, 5.0), "a", "a", "x"),
        SoundtrackMatch(Film("None", 2000, 5.0)),
        SoundtrackMatch(Film("B", 2000, 4.5), "b", "b", "x"),
    ]
    uris = await build_playlist_tracks(sp, picked, per_film=2)

    assert uris == ["spotify:track:a1", "spotify:track:a2", "spotify:track:b1", "spotify:track:b2"]


async def test_write_creates_a_private_playlist_then_updates_it_in_place(fake_spotify, client_for):
    fake = fake_spotify
    fake.add_user("user1")
    for track_id in ("t1", "t2", "t3"):
        fake.add_track(track_id)
    sp = client_for()

    created = await write_playlist(sp, "Soundtracks", "desc", ["spotify:track:t1"])
    assert created is True
    (pid,) = [p for p, meta in fake.playlists.items() if meta["name"] == "Soundtracks"]
    assert fake.playlists[pid]["public"] is False
    assert fake.playlist_tracks[pid] == ["t1"]

    again = await write_playlist(
        sp, "Soundtracks", "desc", ["spotify:track:t2", "spotify:track:t3"]
    )
    assert again is False
    assert fake.playlist_tracks[pid] == ["t2", "t3"]
    assert len([p for p in fake.playlists.values() if p["name"] == "Soundtracks"]) == 1


async def test_write_never_touches_someone_elses_playlist_of_the_same_name(
    fake_spotify, client_for
):
    fake = fake_spotify
    fake.add_user("user1")
    fake.add_user("other")
    fake.add_track("t1")
    fake.add_track("theirs")
    fake.add_playlist("followed", owner="other", name="Soundtracks", track_ids=["theirs"])

    created = await write_playlist(client_for(), "Soundtracks", "desc", ["spotify:track:t1"])

    assert created is True
    assert fake.playlist_tracks["followed"] == ["theirs"]
