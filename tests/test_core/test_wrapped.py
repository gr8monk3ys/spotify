"""The Wrapped page: what the log counts into, and that nothing unsafe reaches the HTML."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta

from spotifyforge.core.history import Gap, Play
from spotifyforge.core.wrapped import build_wrapped, render_wrapped

DAY = date(2026, 10, 22)
# A Monday at noon, UTC.
START = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


def _play(
    minutes: float,
    track: str = "t1",
    *,
    name: str | None = None,
    artist: str = "Alpha",
    album: str = "First Album",
    context: str | None = None,
    start: datetime = START,
) -> Play:
    return Play(
        played_at=(start + timedelta(minutes=minutes)).isoformat(),
        track_id=track,
        name=name or f"Song {track}",
        artists=(artist,),
        artist_ids=(artist.lower(),),
        album=album,
        album_id=album.lower().replace(" ", "-"),
        duration_ms=180_000,
        context=context,
    )


def _log() -> list[Play]:
    return [
        _play(0, "t1", context="spotify:playlist:mine"),
        _play(5, "t1", context="spotify:playlist:mine"),
        _play(10, "t2", artist="Beta", album="Second", context="spotify:album:second"),
        # The next day, an hour later on the clock.
        _play(24 * 60 + 60, "t1", context="spotify:playlist:theirs"),
        _play(24 * 60 + 65, "t3", name="Song t1 - 2011 Remaster"),
    ]


def _build(plays=None, **kwargs):
    return build_wrapped(
        _log() if plays is None else plays,
        kwargs.pop("owner", "Someone"),
        DAY,
        kwargs.pop("owned", {"mine": "Late Set"}),
        tz=UTC,
        **kwargs,
    )


def test_counts_plays_songs_and_minutes():
    data = _build()

    assert (data.plays, data.minutes, data.artists) == (5, 15, 2)
    # The remaster is the same song as the album cut.
    assert data.songs == 2
    song = data.top_songs[0]
    assert (song.name, song.detail, song.plays) == ("Song t1", "Alpha", 4)
    assert [(a.name, a.plays, a.songs) for a in data.top_artists] == [
        ("Alpha", 4, 1),
        ("Beta", 1, 1),
    ]
    assert data.days == 2


def test_the_clock_grid_reads_hours_and_weekdays_in_the_given_zone():
    data = _build()

    assert data.grid[12][0] == 3  # Monday 12:00
    assert data.grid[13][1] == 2  # Tuesday 13:00
    assert sum(map(sum, data.grid)) == 5


def test_the_when_headline_names_the_marked_cell():
    # Monday is the busiest day and 13:00 the busiest hour, but Monday has
    # nothing at 13:00: the headline must name a cell that exists.
    monday = START.replace(hour=9)
    plays = [_play(m, start=monday) for m in (0, 5)]
    plays += [_play(m, start=monday.replace(hour=20)) for m in (0, 5)]
    tuesday = START.replace(hour=13) + timedelta(days=1)
    wednesday = tuesday + timedelta(days=1)
    plays += [_play(m, start=tuesday) for m in (0,)] + [_play(m, start=wednesday) for m in (0, 5)]
    plays.append(_play(10, start=tuesday))
    plays.sort(key=lambda p: p.played_at)

    html = render_wrapped(_build(plays))

    assert "Busiest on Mondays at 09:00, with 2 plays." in html


def test_where_plays_start_splits_own_playlists_from_the_rest():
    usage = _build().usage

    assert (usage.from_own_playlists, usage.from_other_playlists, usage.from_albums) == (2, 1, 1)
    assert usage.from_elsewhere == 1
    assert usage.played == [("mine", "Late Set", 2)]


def test_a_year_keeps_only_its_own_plays():
    plays = [_play(0, "old", start=datetime(2025, 12, 31, 12, tzinfo=UTC)), *_log()]

    data = _build(plays, year=2026)

    assert data.plays == 5
    assert "in 2026" in render_wrapped(data)


def test_a_song_played_hard_then_dropped_is_named_once_the_log_has_settled():
    september = datetime(2026, 9, 1, 12, tzinfo=UTC)
    plays = [_play(i * 60, "hit", name="Hit", start=september) for i in range(6)]
    plays += [_play(i * 60, "steady", name="Steady", start=september) for i in range(6)]
    later = datetime(2026, 10, 29, 12, tzinfo=UTC)
    plays += [_play(i * 60, "steady", name="Steady", start=later) for i in range(6)]
    plays.sort(key=lambda p: p.played_at)

    data = _build(plays)

    assert [(d.name, d.month, d.peak, d.since) for d in data.dropped] == [
        ("Hit", "September 2026", 6, 0)
    ]


def test_until_the_log_is_long_enough_the_dropped_section_says_when_it_will_be():
    data = _build()
    html = render_wrapped(data)

    assert data.dropped == ()
    assert data.dropped_from == date(2026, 11, 28)
    assert "It can first name one on 28 November 2026." in html


def test_the_page_states_its_coverage_and_its_gaps():
    html = render_wrapped(_build())
    assert "since 12 October 2026" in html
    assert "<span>Logged 12 October 2026 – 13 October 2026</span><span>2 days</span>" in html
    assert "No gaps recorded" in html

    gap = Gap(after="2026-10-12T12:10:00+00:00", before="2026-10-13T09:00:00+00:00")
    html = render_wrapped(_build(gaps=[gap]))
    assert "1 possible gap" in html
    assert "between 12 Oct 12:10 and 13 Oct 09:00" in html


def test_an_empty_log_still_renders():
    html = render_wrapped(_build([]))

    assert "Nothing logged yet" in html
    assert html.rstrip().endswith("</html>")


def test_the_page_is_one_file_that_fetches_nothing():
    html = render_wrapped(_build())

    assert html.startswith("<!doctype html>")
    assert "<script" not in html
    assert "@import" not in html
    urls = re.findall(r"url\(([^)]{0,24})", html)
    assert len(urls) == 3
    assert all(u.startswith("data:font/woff2;base64,") for u in urls)
    assert not re.search(r"<(link|img|iframe|source|object|embed)\b", html)
    assert re.findall(r'(?:href|src)="([^"]+)"', html) == ["https://github.com/gr8monk3ys/spotify"]


def test_strings_from_spotify_are_escaped_everywhere_they_appear():
    hostile = '<img src=x onerror=alert(1)>"&'
    plays = [
        _play(i, "evil", name=hostile, artist=hostile, album=hostile, context="spotify:playlist:p")
        for i in range(6)
    ]

    html = render_wrapped(_build(plays, owner=hostile, owned={"p": hostile}))

    assert "<img" not in html
    assert "onerror=alert(1)>" not in html
    # title, h1, top song headline, song row and its sub-line, artist cells,
    # album row and its sub-line, the playlist row.
    assert html.count("&lt;img src=x onerror=alert(1)&gt;") >= 10
