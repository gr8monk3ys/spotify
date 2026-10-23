"""The taste page: what gets counted, and that nothing unsafe reaches the HTML."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, date, datetime

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


# -- genres by year liked -------------------------------------------------


def _liked(track_id: str, genre: str, year: int, day: int = 1) -> CurationTrack:
    """A song liked on *day* January of *year*."""
    return replace(
        _track(track_id, artist=f"artist {track_id}", genres=(genre,), album=track_id),
        added_at=datetime(year, 1, day, 12, tzinfo=UTC),
    )


def _liked_library() -> list[CurationTrack]:
    """50 songs liked in 2016 and 50 in 2018 (columns need 50), a sparse 2019."""
    songs = [_liked(f"a{i}", "indie" if i < 30 else "rock", 2016, 1 + i % 28) for i in range(50)]
    songs += [_liked(f"b{i}", "rap" if i < 45 else "indie", 2018, 1 + i % 28) for i in range(50)]
    songs += [_liked(f"c{i}", "jazz", 2019, 1 + i % 28) for i in range(10)]
    return songs


def test_songs_are_counted_by_the_year_they_were_liked():
    data = build_page_data(_liked_library(), "Someone", DAY)

    # 2017 has nothing and 2019 too little, so each joins a neighbour:
    # 2017 the year after it, the 2019 tail the column before it.
    assert [(s.first, s.last) for s in data.liked_years] == [(2016, 2016), (2017, 2019)]
    assert data.all_by_liked == (50, 60)
    by_name = {line.name: line for line in data.liked_lines}
    assert by_name["indie"].by_year == (30, 5)
    assert by_name["rap"].by_year == (0, 45)
    assert by_name["indie"].total == 35


def test_each_year_puts_forward_its_busiest_genres_in_the_order_they_took_hold():
    data = build_page_data(_liked_library(), "Someone", DAY)

    # Two per column: indie and rock from 2016, rap and jazz from 2017-19.
    assert [line.name for line in data.liked_lines] == ["indie", "rock", "rap", "jazz"]
    assert [line.peak for line in data.liked_lines] == [0, 0, 1, 1]


def test_a_genres_year_is_its_largest_share_not_its_largest_count():
    songs = [_liked(f"a{i}", "dub" if i < 30 else "pop", 2016, 1 + i % 28) for i in range(50)]
    songs += [_liked(f"b{i}", "dub" if i < 40 else "pop", 2018, 1 + i % 28) for i in range(200)]

    data = build_page_data(songs, "Someone", DAY)

    dub = next(line for line in data.liked_lines if line.name == "dub")
    # 40 songs in 2017-18 is more than 30 in 2016, but 30 of 50 is the larger share.
    assert dub.by_year == (30, 40)
    assert dub.peak == 0


def test_a_bulk_import_day_is_left_out_of_the_liked_table_and_nowhere_else():
    imported = [_liked(f"x{i}", "rock", 2026, 14) for i in range(150)]
    tracks = _liked_library() + imported

    data = build_page_data(tracks, "Someone", DAY)

    assert data.bulk_days == ((date(2026, 1, 14), 150),)
    assert sum(data.all_by_liked) == 110
    assert data.liked_years[-1].last == 2019
    assert data.songs == 260  # still counted in the decade table and the key
    html = render_page(data)
    assert "It leaves out the 150 songs that arrived in one import on 14 January 2026 ‡." in html
    assert "‡ 150 songs share one liked date, 14 January 2026" in html


def test_an_ordinary_busy_day_is_not_an_import():
    # 100 songs in a day is the floor, whatever share of the library it is.
    tracks = [_liked(f"d{i}", "rock", 2018, 3) for i in range(99)]

    data = build_page_data(tracks, "Someone", DAY)

    assert data.bulk_days == ()
    assert data.all_by_liked == (99,)


def test_the_liked_table_renders_with_its_headline_and_spans():
    html = render_page(build_page_data(_liked_library(), "Someone", DAY))

    assert "From indie in 2016 to jazz in 2017–19." in html
    assert '<span class="full">2017–19</span>' in html
    assert '<span class="short" aria-hidden="true">’17<br>–19</span>' in html
    assert '<tr><th scope="row">indie</th><td class="peak">30</td><td>5</td>' in html


def test_songs_without_a_liked_date_leave_the_liked_table_empty():
    data = build_page_data(_library(), "Someone", DAY)

    assert data.liked_years == ()
    assert data.unliked == len(_library())
    assert "No liked dates to go by yet." in render_page(data)


def test_genres_in_the_liked_table_are_escaped():
    hostile = '<img src=x onerror=alert(1)>"&'
    tracks = [_liked(f"h{i}", hostile, 2016, 1 + i % 28) for i in range(60)]

    html = render_page(build_page_data(tracks, "Someone", DAY))

    assert "<img" not in html
    assert f'<tr><th scope="row">{"&lt;img src=x onerror=alert(1)&gt;&quot;&amp;"}</th>' in html
    # The headline names the genre too.
    assert "Mostly &lt;img src=x onerror=alert(1)&gt;&quot;&amp;." in html
