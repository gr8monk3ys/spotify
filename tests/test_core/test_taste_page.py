"""The taste page: what gets counted, and that nothing unsafe reaches the HTML."""

from __future__ import annotations

import re
from datetime import date

from spotifyforge.core.curation import CurationTrack
from spotifyforge.core.taste_page import build_page_data, render_page

DAY = date(2026, 10, 12)


def _track(
    track_id: str,
    *,
    artist: str = "Artist",
    genres: tuple[str, ...] = (),
    year: int | None = 2015,
    album: str = "alb",
    album_total: int | None = 10,
) -> CurationTrack:
    return CurationTrack(
        id=track_id,
        uri=f"spotify:track:{track_id}",
        name=track_id,
        artist_ids=(artist.lower(),),
        artist_names=(artist,),
        release_year=year,
        popularity=50,
        genres=genres,
        album_id=album,
        album_name=album.title(),
        album_total_tracks=album_total,
    )


def _library() -> list[CurationTrack]:
    return [
        _track("a", artist="Old Band", genres=("psych",), year=1967, album="one"),
        _track("b", artist="Old Band", genres=("psych",), year=1968, album="one"),
        _track("c", artist="Old Band", genres=("psych",), year=1969, album="one"),
        _track("d", artist="New Act", genres=("psych", "indie"), year=2014, album="two"),
        _track("e", artist="New Act", genres=("indie",), year=2015, album="two"),
        _track("f", artist="Nobody", genres=(), year=1985, album="three"),
        _track("g", artist="Dateless", genres=("indie",), year=None, album="four"),
    ]


def test_counts_genres_against_a_contiguous_run_of_decades():
    data = build_page_data(_library(), "Someone", DAY)

    # 1960s through 2010s with nothing skipped, though the 70s, 90s and 00s are empty.
    assert data.decades == (1960, 1970, 1980, 1990, 2000, 2010)
    psych, indie = data.lines
    assert (psych.name, psych.total, psych.by_decade) == ("psych", 4, (3, 0, 0, 0, 0, 1))
    assert data.decades[psych.peak] == 1960
    assert (indie.name, indie.total, indie.by_decade) == ("indie", 3, (0, 0, 0, 0, 0, 2))
    assert data.decades[indie.peak] == 2010


def test_totals_count_every_song_once_whatever_its_genres():
    data = build_page_data(_library(), "Someone", DAY)

    assert data.songs == 7
    assert data.genres == 2
    assert data.unclassified == 1
    assert data.undated == 1
    # Song "d" rides two lines but is one song in the 2010s column.
    assert data.all_by_decade == (3, 0, 1, 0, 0, 2)


def test_artists_and_albums_are_ranked_by_songs_liked():
    data = build_page_data(_library(), "Someone", DAY)

    top = data.top_artists[0]
    assert (top.name, top.songs, top.genres) == ("Old Band", 3, ("psych",))
    assert (top.first_year, top.last_year) == (1967, 1969)
    # Only an album with at least three liked songs is listed.
    assert [(a.title, a.liked, a.total) for a in data.top_albums] == [("One", 3, 10)]


def test_an_empty_library_still_renders():
    html = render_page(build_page_data([], "Someone", DAY))

    assert "<title>Someone’s library" in html
    assert "Nothing dated yet." in html
    assert "Songs, editions merged</dt><dd>0</dd>" in html


def test_an_artist_id_without_a_name_does_not_break_the_page():
    nameless = CurationTrack(
        id="x",
        uri="spotify:track:x",
        name="x",
        artist_ids=("ghost",),
        artist_names=(),
        release_year=2001,
        popularity=1,
    )

    data = build_page_data([nameless], "Someone", DAY)

    assert data.songs == 1
    assert data.top_artists == ()


def test_the_page_is_one_file_that_fetches_nothing():
    html = render_page(build_page_data(_library(), "Someone", DAY))

    assert html.startswith("<!doctype html>")
    assert "<script" not in html
    assert "@import" not in html
    # The only url() values are the three embedded fonts.
    urls = re.findall(r"url\(([^)]{0,24})", html)
    assert len(urls) == 3
    assert all(u.startswith("data:font/woff2;base64,") for u in urls)
    assert not re.search(r"<(link|img|iframe|source)\b", html)
    # The one outbound reference is a link the reader has to click.
    assert re.findall(r'(?:href|src)="([^"]+)"', html) == ["https://github.com/gr8monk3ys/spotify"]


def test_names_from_spotify_are_escaped_everywhere_they_appear():
    hostile = '<img src=x onerror=alert(1)>"&'
    tracks = [_track(f"t{i}", artist=hostile, genres=(hostile,), album="evil") for i in range(3)]
    for index, track in enumerate(tracks):
        tracks[index] = CurationTrack(**{**track.__dict__, "album_name": hostile})

    html = render_page(build_page_data(tracks, hostile, DAY))

    assert "<img" not in html
    assert "onerror=alert(1)>" not in html
    assert html.count("&lt;img src=x onerror=alert(1)&gt;") >= 5  # title, h1, line, artist, album


def test_the_solid_cell_marks_each_lines_busiest_decade():
    html = render_page(build_page_data(_library(), "Someone", DAY))

    assert html.count('class="peak"') == 2
    assert '<th scope="row">psych</th><td class="peak">3</td>' in html


def test_light_dark_and_print_are_all_styled():
    html = render_page(build_page_data(_library(), "Someone", DAY))

    assert "prefers-color-scheme:dark" in html
    assert "@media print" in html
    assert "prefers-reduced-motion:reduce" in html


def test_the_headline_is_derived_from_the_table_it_sits_over():
    html = render_page(build_page_data(_library(), "Someone", DAY))

    # Busiest decade overall is the 1960s (3 songs); indie peaks in the 2010s.
    assert "Mostly the 1960s, with a detour through the 2010s." in html


def test_the_fonts_ship_inside_the_package():
    from importlib.resources import files

    fonts = files("spotifyforge") / "assets" / "fonts"
    for name in (
        "fraunces-latin.woff2",
        "instrument-sans-latin.woff2",
        "ibm-plex-mono-500-latin.woff2",
    ):
        assert (fonts / name).read_bytes()[:4] == b"wOF2"
