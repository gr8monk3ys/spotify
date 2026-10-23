"""The library as one page: genres against decades and against the year
they were liked, then artists and albums.

``export page`` writes a single HTML file that needs nothing else: no
stylesheet, script, font or image is fetched, so it can be opened from
disk, emailed, or dropped onto any host. Everything on it is counted
from the liked library exactly as curation reads it (primary-artist
genres, edition variants collapsed), which is the point of generating it
here rather than screenshotting a stats site.

It is a summary by design. There is no track list and no play history
in the file, only counts: how many songs sit on each genre in each
decade, how many were liked in each year, and which artists and albums
they pile up on.

Every string that reaches the page came from Spotify or MusicBrainz,
which is to say from strangers, and is escaped on the way in.
"""

from __future__ import annotations

from base64 import b64encode
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from functools import cache
from html import escape
from importlib.resources import files
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from spotifyforge.core.curation import CurationTrack

REPOSITORY_URL = "https://github.com/gr8monk3ys/spotify"

# An album needs this many liked songs to be listed: below it the table
# fills with singles that happen to share an album id.
_MIN_ALBUM_SONGS = 3

# A day on which at least this many songs, and this share of the library,
# were liked is an import, not a day of listening. Measured on this library
# on 2026-10-22: 1,588 of 3,041 liked songs carry 14 August 2026, and the
# busiest ordinary day holds 66. Their liked date says when they were moved
# in, not when they were first liked, so the liked-year table leaves them out.
_BULK_DAY_SONGS = 100
_BULK_DAY_SHARE = 0.05

# A liked-year column needs this many songs; a sparser year joins the next
# one. A share of a column (which is how a genre's year is chosen) means
# little over a handful of songs, and the library has near-empty years.
_MIN_YEAR_SONGS = 50

# How many of its busiest genres each liked-year column puts in the table.
_GENRES_PER_YEAR = 2


@dataclass(frozen=True)
class GenreLine:
    name: str
    total: int
    by_decade: tuple[int, ...]
    peak: int  # index into the page's decades

    @property
    def peak_count(self) -> int:
        return self.by_decade[self.peak]


@dataclass(frozen=True)
class YearSpan:
    """One liked-year column: a single year, or a run of sparse years."""

    first: int
    last: int


@dataclass(frozen=True)
class LikedLine:
    name: str
    total: int
    by_year: tuple[int, ...]
    # Index into the page's liked years: the column this genre is the
    # largest share of, not the one with most songs. The library liked
    # far more in some years than others, and raw counts would mark the
    # busiest year on almost every row.
    peak: int


@dataclass(frozen=True)
class ArtistRow:
    name: str
    songs: int
    genres: tuple[str, ...]
    first_year: int | None
    last_year: int | None


@dataclass(frozen=True)
class AlbumRow:
    title: str
    artist: str
    year: int | None
    liked: int
    total: int | None


@dataclass(frozen=True)
class PageData:
    owner: str
    compiled: date
    songs: int
    genres: int
    artists: int
    unclassified: int
    undated: int
    decades: tuple[int, ...]
    all_by_decade: tuple[int, ...]
    lines: tuple[GenreLine, ...]
    top_artists: tuple[ArtistRow, ...]
    top_albums: tuple[AlbumRow, ...]
    # The same genres against the year each song was liked.
    liked_years: tuple[YearSpan, ...] = ()
    all_by_liked: tuple[int, ...] = ()
    liked_lines: tuple[LikedLine, ...] = ()
    bulk_days: tuple[tuple[date, int], ...] = ()  # (day, songs liked that day), left out
    unliked: int = 0  # songs with no liked date at all


def _decade(year: int | None) -> int | None:
    return year // 10 * 10 if year else None


def build_page_data(
    tracks: Sequence[CurationTrack],
    owner: str,
    compiled: date,
    genre_count: int = 14,
    artist_count: int = 12,
    album_count: int = 12,
) -> PageData:
    """Count *tracks* (already deduplicated, genres attached) into the page's tables."""
    present = sorted({d for t in tracks if (d := _decade(t.release_year)) is not None})
    decades = tuple(range(present[0], present[-1] + 10, 10)) if present else ()
    column = {decade: index for index, decade in enumerate(decades)}

    genre_totals: Counter[str] = Counter()
    genre_decades: dict[str, list[int]] = defaultdict(lambda: [0] * len(decades))
    everything = [0] * len(decades)
    for track in tracks:
        decade = _decade(track.release_year)
        if decade is not None:
            everything[column[decade]] += 1
        for genre in track.genres:
            genre_totals[genre] += 1
            if decade is not None:
                genre_decades[genre][column[decade]] += 1

    lines = []
    for name, total in sorted(genre_totals.items(), key=lambda item: (-item[1], item[0])):
        counts = tuple(genre_decades[name])
        if not any(counts):
            continue  # a genre whose every song is undated has no row to draw
        lines.append(GenreLine(name, total, counts, counts.index(max(counts))))
        if len(lines) == genre_count:
            break

    by_artist: dict[str, list[CurationTrack]] = defaultdict(list)
    for track in tracks:
        # Spotify occasionally credits an artist id with no name attached.
        if track.artist_ids and track.artist_names:
            by_artist[track.artist_ids[0]].append(track)
    artists = []
    for songs in by_artist.values():
        years = [t.release_year for t in songs if t.release_year]
        artists.append(
            ArtistRow(
                name=songs[0].artist_names[0],
                songs=len(songs),
                genres=tuple(dict.fromkeys(g for t in songs for g in t.genres))[:3],
                first_year=min(years) if years else None,
                last_year=max(years) if years else None,
            )
        )
    artists.sort(key=lambda row: (-row.songs, row.name.casefold()))

    by_album: dict[str, list[CurationTrack]] = defaultdict(list)
    for track in tracks:
        if track.album_id:
            by_album[track.album_id].append(track)
    albums = [
        AlbumRow(
            title=songs[0].album_name,
            artist=songs[0].artist_names[0] if songs[0].artist_names else "",
            year=songs[0].release_year,
            liked=len(songs),
            total=songs[0].album_total_tracks,
        )
        for songs in by_album.values()
        if len(songs) >= _MIN_ALBUM_SONGS
    ]
    albums.sort(key=lambda row: (-row.liked, row.title.casefold()))

    spans, all_by_liked, liked_lines, bulk_days = _by_year_liked(tracks)

    return PageData(
        owner=owner,
        compiled=compiled,
        songs=len(tracks),
        genres=len(genre_totals),
        artists=len(by_artist),
        unclassified=sum(1 for t in tracks if not t.genres),
        undated=sum(1 for t in tracks if not t.release_year),
        decades=decades,
        all_by_decade=tuple(everything),
        lines=tuple(lines),
        top_artists=tuple(artists[:artist_count]),
        top_albums=tuple(albums[:album_count]),
        liked_years=spans,
        all_by_liked=all_by_liked,
        liked_lines=liked_lines,
        bulk_days=bulk_days,
        unliked=sum(1 for t in tracks if t.added_at is None),
    )


def _year_spans(counts: Counter[int]) -> tuple[YearSpan, ...]:
    """Contiguous columns from the first year to the last, each holding at
    least ``_MIN_YEAR_SONGS`` songs: a sparse year joins the years after it,
    and a sparse tail joins the column before it."""
    if not counts:
        return ()
    spans: list[YearSpan] = []
    start, held = min(counts), 0
    for year in range(min(counts), max(counts) + 1):
        held += counts[year]
        if held >= _MIN_YEAR_SONGS:
            spans.append(YearSpan(start, year))
            start, held = year + 1, 0
    if start <= max(counts):
        if spans:
            spans[-1] = YearSpan(spans[-1].first, max(counts))
        else:
            spans.append(YearSpan(start, max(counts)))
    return tuple(spans)


def _by_year_liked(
    tracks: Sequence[CurationTrack],
) -> tuple[
    tuple[YearSpan, ...], tuple[int, ...], tuple[LikedLine, ...], tuple[tuple[date, int], ...]
]:
    """Genres against the year their songs were liked, imports set aside.

    The rows are not the decade table's. Each liked-year column puts
    forward its own busiest genres, and the rows are ordered by the
    column each genre is the largest share of, so reading down the table
    is reading forward in time: what the library took up, and when.
    """
    days = Counter(t.added_at.date() for t in tracks if t.added_at is not None)
    floor = max(_BULK_DAY_SONGS, _BULK_DAY_SHARE * len(tracks))
    bulk = {day: count for day, count in days.items() if count >= floor}
    liked = [
        (t.added_at.year, t.genres)
        for t in tracks
        if t.added_at is not None and t.added_at.date() not in bulk
    ]

    spans = _year_spans(Counter(year for year, _ in liked))
    column = {
        year: index for index, span in enumerate(spans) for year in range(span.first, span.last + 1)
    }
    everything = [0] * len(spans)
    by_genre: dict[str, list[int]] = defaultdict(lambda: [0] * len(spans))
    for year, genres in liked:
        everything[column[year]] += 1
        for genre in genres:
            by_genre[genre][column[year]] += 1

    picked: dict[str, None] = {}
    for index in range(len(spans)):
        busiest = sorted(by_genre, key=lambda name: (-by_genre[name][index], name))
        picked.update(dict.fromkeys(n for n in busiest[:_GENRES_PER_YEAR] if by_genre[n][index]))

    lines = []
    for name in picked:
        counts = tuple(by_genre[name])
        shares = [count / everything[i] for i, count in enumerate(counts)]
        lines.append(LikedLine(name, sum(counts), counts, shares.index(max(shares))))
    lines.sort(key=lambda line: (line.peak, -line.total, line.name))
    return spans, tuple(everything), tuple(lines), tuple(sorted(bulk.items()))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
#
# The look is lscaturchio.xyz's own system, "The Field Notebook": warm
# paper, one green ink, a serif that speaks, a sans that reads and a
# monospace that files. Its rules are followed here rather than restated:
# green marks emphasis and never area, every rule is a 1px sand hairline,
# every count and date is a mono wall label, and nothing animates in.


def _n(value: int) -> str:
    return f"{value:,}"


@cache
def _font_faces() -> str:
    """The three faces as data URIs, so the page still fetches nothing.

    These are the latin subsets the site itself serves. Text outside
    them (a Japanese title, an Arabic artist name) falls back to the
    reader's system fonts, which is where those scripts read best anyway.
    """
    fonts = files("spotifyforge") / "assets" / "fonts"
    faces = (
        ("Fraunces", "100 900", "fraunces-latin.woff2"),
        ("Instrument Sans", "400 700", "instrument-sans-latin.woff2"),
        ("IBM Plex Mono", "500", "ibm-plex-mono-500-latin.woff2"),
    )
    return "".join(
        f'@font-face{{font-family:"{family}";font-weight:{weight};font-display:swap;'
        f"src:url(data:font/woff2;base64,"
        f"{b64encode((fonts / name).read_bytes()).decode('ascii')}) format('woff2')}}"
        for family, weight, name in faces
    )


def _label(decade: int) -> str:
    """ "1960s" in full; the stylesheet swaps to the short form on narrow screens."""
    return (
        f'<span class="full">{decade}s</span>'
        f'<span class="short" aria-hidden="true">{decade % 100:02d}s</span>'
    )


def _cell(count: int, *, peak: bool = False) -> str:
    if not count:
        return '<td class="none"><span aria-hidden="true">–</span><span class="sr">0</span></td>'
    if peak:
        return f'<td class="peak">{_n(count)}</td>'
    return f"<td>{_n(count)}</td>"


def _genre_table(data: PageData) -> str:
    head = "".join(f'<th scope="col">{_label(d)}</th>' for d in data.decades)
    rows = "".join(
        f'<tr><th scope="row">{escape(line.name)}</th>'
        + "".join(
            _cell(count, peak=index == line.peak) for index, count in enumerate(line.by_decade)
        )
        + f'<td class="total">{_n(line.total)}</td></tr>'
        for line in data.lines
    )
    foot = "".join(_cell(count) for count in data.all_by_decade)
    return (
        "<table>"
        f'<thead><tr><th scope="col">Genre</th>{head}<th scope="col">Songs</th></tr></thead>'
        f"<tbody>{rows}</tbody>"
        f'<tfoot><tr><th scope="row">Every song, any genre</th>{foot}'
        f'<td class="total">{_n(data.songs)}</td></tr></tfoot>'
        "</table>"
    )


def _span_text(span: YearSpan) -> str:
    return str(span.first) if span.first == span.last else f"{span.first}–{span.last % 100:02d}"


def _year_label(span: YearSpan) -> str:
    """ "2018" or "2023–26" in full; "’18", or "’23" over "–26", on narrow screens.

    A run of years is stacked on phones because its column is the widest
    head in a table that has to fit 390px.
    """
    short = f"’{span.first % 100:02d}" + (
        "" if span.first == span.last else f"<br>–{span.last % 100:02d}"
    )
    return (
        f'<span class="full">{_span_text(span)}</span>'
        f'<span class="short" aria-hidden="true">{short}</span>'
    )


def _liked_table(data: PageData) -> str:
    head = "".join(f'<th scope="col">{_year_label(s)}</th>' for s in data.liked_years)
    rows = "".join(
        f'<tr><th scope="row">{escape(line.name)}</th>'
        + "".join(_cell(count, peak=index == line.peak) for index, count in enumerate(line.by_year))
        + f'<td class="total">{_n(line.total)}</td></tr>'
        for line in data.liked_lines
    )
    foot = "".join(_cell(count) for count in data.all_by_liked)
    return (
        '<table class="liked">'
        f'<thead><tr><th scope="col">Genre</th>{head}<th scope="col">Songs</th></tr></thead>'
        f"<tbody>{rows}</tbody>"
        f'<tfoot><tr><th scope="row">Every song, any genre</th>{foot}'
        f'<td class="total">{_n(sum(data.all_by_liked))}</td></tr></tfoot>'
        "</table>"
    )


def _years(first: int | None, last: int | None) -> str:
    if first is None or last is None:
        return "—"
    return str(first) if first == last else f"{first}–{last}"


def _sub(text: str) -> str:
    """The second column again, as a line under the name.

    Narrow screens hide the text column and show this instead, so the
    counts stay on screen without scrolling. Hidden from assistive
    technology, which reads the real column.
    """
    return f'<span class="sub" aria-hidden="true">{escape(text)}</span>'


def _artist_table(data: PageData) -> str:
    rows = "".join(
        f'<tr><th scope="row">{escape(row.name)}'
        f"{_sub(', '.join(row.genres) or 'unclassified †')}</th>"
        f'<td class="text">{escape(", ".join(row.genres)) or "unclassified †"}</td>'
        f"<td>{_years(row.first_year, row.last_year)}</td>"
        f'<td class="total">{_n(row.songs)}</td></tr>'
        for row in data.top_artists
    )
    return (
        "<table>"
        '<thead><tr><th scope="col">Artist</th><th scope="col" class="text">Files under</th>'
        '<th scope="col">Released</th><th scope="col">Songs</th></tr></thead>'
        f"<tbody>{rows}</tbody></table>"
    )


def _album_table(data: PageData) -> str:
    rows = "".join(
        f'<tr><th scope="row">{escape(row.title)}{_sub(row.artist)}</th>'
        f'<td class="text">{escape(row.artist)}</td>'
        f"<td>{row.year or '—'}</td>"
        f'<td class="total">{_n(row.liked)}'
        + (f'<span class="of"> of {_n(row.total)}</span>' if row.total else "")
        + "</td></tr>"
        for row in data.top_albums
    )
    return (
        "<table>"
        '<thead><tr><th scope="col">Album</th><th scope="col" class="text">Artist</th>'
        '<th scope="col">Year</th><th scope="col">Liked</th></tr></thead>'
        f"<tbody>{rows}</tbody></table>"
    )


def _column_guides(columns: int) -> str:
    """The reading guide: pointing at a cell tints its whole column too.

    CSS has no "this cell's column" selector, so one rule per column is
    written out for however many decades this library spans.
    """
    return "".join(
        f".grid table:has(tbody td:nth-child({n}):hover) "
        f":is(tbody td, tfoot td):nth-child({n}){{background-color:var(--tinted)}}"
        for n in range(2, columns + 3)
    )


_CSS = """
:root{--paper:#f9f8f5;--tinted:#eeece7;--ink:#1a1f23;--muted:#606976;--hairline:#e2dbd5;
--pen:#184e35;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--paper:#111317;--tinted:#25272d;--ink:#fafafa;
--muted:#abb0ba;--hairline:#292c32;--pen:#42a979;color-scheme:dark}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%;scrollbar-color:var(--hairline) transparent}
body{margin:0;background:var(--paper);color:var(--ink);
font:1rem/1.65 "Instrument Sans",ui-sans-serif,system-ui,sans-serif}
::selection{background:var(--pen);color:var(--paper)}
:focus-visible{outline:2px solid var(--pen);outline-offset:3px;border-radius:6px}
a{color:inherit;text-decoration-color:var(--ink);text-decoration-thickness:1px;
text-underline-offset:.22em;transition:color .2s,text-decoration-color .2s}
a:hover{color:var(--pen);text-decoration-color:var(--pen)}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%)}
.label,thead th,td,tfoot th{font-family:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,monospace;
font-weight:500;font-variant-numeric:tabular-nums}
.label,thead th{font-size:.72rem;line-height:1.4;letter-spacing:.16em;text-transform:uppercase;
color:var(--muted)}
.label{margin:0;display:flex;flex-wrap:wrap;gap:0 .6em}
.label span+span::before{content:"·";margin-right:.6em}
main{max-width:72rem;margin:0 auto;padding:clamp(3rem,9vw,7rem) clamp(1.25rem,5vw,3rem) 4rem}
h1,h2{font-family:Fraunces,ui-serif,Georgia,serif;margin:0;text-wrap:balance}
h1{font-size:clamp(2.4rem,5.2vw,4.9rem);font-weight:720;line-height:1.02;letter-spacing:-.035em;
margin-top:1.25rem}
h1 em{font-style:normal;color:var(--pen)}
.masthead{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:2.5rem clamp(2rem,6vw,5rem);
align-items:end;padding-bottom:clamp(2.5rem,6vw,4rem);border-bottom:1px solid var(--hairline)}
.thesis{margin:1.25rem 0 0;max-width:34rem;font-size:clamp(1.0625rem,1.6vw,1.25rem);
line-height:1.5;color:var(--muted)}
.key{margin:0;border-top:1px solid var(--hairline)}
.key div{display:flex;justify-content:space-between;align-items:baseline;gap:1rem;
padding:.6rem 0;border-bottom:1px solid var(--hairline)}
.key dt{margin:0}
.key dd{margin:0;font:500 .9375rem/1.4 "IBM Plex Mono",ui-monospace,monospace;
font-variant-numeric:tabular-nums}
section{padding-top:clamp(4rem,8vw,6rem)}
h2{font-size:clamp(1.55rem,2.4vw,2.25rem);font-weight:650;line-height:1.18;letter-spacing:-.026em;
margin-top:.75rem}
.lead{margin:.75rem 0 1.75rem;max-width:36rem;color:var(--muted)}
.grid{overflow-x:auto;scrollbar-color:var(--hairline) transparent}
table{border-collapse:collapse;width:100%}
th,td{padding:.7rem .75rem;text-align:right;white-space:nowrap;border-bottom:1px solid var(--hairline);
transition:background-color .15s}
thead th{padding-bottom:.6rem;font-weight:500}
td{font-size:.875rem}
th:first-child{text-align:left;padding-left:0;position:sticky;left:0;background:var(--paper);
white-space:normal;min-width:11rem}
tbody th{font-weight:500}
td.text,th.text{text-align:left;white-space:normal;min-width:12rem}
td.text{font:400 .9375rem/1.5 "Instrument Sans",ui-sans-serif,system-ui,sans-serif;color:var(--muted)}
td.none{color:var(--muted);opacity:.55}
td.peak,.peak-key{color:var(--pen);text-decoration:underline 2px;text-underline-offset:.35em}
td.total{color:var(--ink);padding-right:0}
.of{color:var(--muted)}
tbody tr:hover :is(th,td){background-color:var(--tinted)}
tfoot :is(th,td){border-bottom:0;color:var(--ink)}
tfoot th{font-size:.72rem;letter-spacing:.16em;text-transform:uppercase;color:var(--muted)}
.short,.sub{display:none}
.sub{font-size:.875rem;line-height:1.45;color:var(--muted);font-weight:400}
.notes ol{margin:1.5rem 0 0;padding:0;list-style:none;counter-reset:n;max-width:38rem}
.notes li{counter-increment:n;display:grid;grid-template-columns:2.25rem 1fr;
padding:.9rem 0;border-top:1px solid var(--hairline)}
.notes li::before{content:counter(n,decimal-leading-zero);
font:500 .72rem/2.3 "IBM Plex Mono",ui-monospace,monospace;letter-spacing:.16em;color:var(--muted)}
footer{margin-top:clamp(4rem,8vw,6rem);padding-top:1.5rem;border-top:1px solid var(--hairline);
display:grid;grid-template-columns:1.1fr 2fr;gap:1.5rem 3rem}
footer p{margin:0;max-width:36rem;color:var(--muted);font-size:.9375rem}
@media (max-width:52rem){.masthead{grid-template-columns:1fr;align-items:start}
footer{grid-template-columns:1fr}
th,td{padding:.6rem .2rem}td{font-size:.6875rem}thead th,tfoot th{font-size:.6875rem;letter-spacing:.02em}
.full{display:none}.short{display:inline}th:first-child{min-width:5rem;font-size:.8125rem;line-height:1.35}
td.text,th.text{display:none}.sub{display:block}
.liked :is(td,th+th){padding-left:.1rem;padding-right:.1rem}.liked td.total{padding-right:0}}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
@media print{:root{--paper:#fff;--tinted:#fff;--ink:#000;--muted:#444;--hairline:#bbb;--pen:#000}
.grid{overflow:visible}section{break-inside:avoid}}
"""


def _headline(data: PageData) -> str:
    """One sentence the first table bears out: the busiest decade, and the
    furthest-back decade any of the listed genres peaks in."""
    if not data.decades:
        return "Nothing dated yet."
    busiest = data.decades[data.all_by_decade.index(max(data.all_by_decade))]
    detours = sorted({data.decades[line.peak] for line in data.lines} - {busiest})
    if not detours:
        return f"Mostly the {busiest}s."
    return f"Mostly the {busiest}s, with a detour through the {detours[0]}s."


def _liked_headline(data: PageData) -> str:
    """The first and last genres down the year-liked table, by the year each took hold."""
    if not data.liked_lines:
        return "No liked dates to go by yet."
    first, last = data.liked_lines[0], data.liked_lines[-1]
    if first.peak == last.peak:
        return f"Mostly {escape(first.name)}."
    return (
        f"From {escape(first.name)} in {_span_text(data.liked_years[first.peak])} "
        f"to {escape(last.name)} in {_span_text(data.liked_years[last.peak])}."
    )


def _day(day: date) -> str:
    return f"{day.day} {day:%B %Y}"


def _bulk_note(data: PageData) -> str:
    """Why songs liked on an import day are missing from the year-liked table."""
    if not data.bulk_days:
        return ""
    songs = sum(count for _, count in data.bulk_days)
    if len(data.bulk_days) == 1:
        when = f"one liked date, {_day(data.bulk_days[0][0])}"
    else:
        when = "the liked dates " + ", ".join(_day(day) for day, _ in data.bulk_days)
    return (
        f"‡ {_n(songs)} songs share {when}: they arrived in one import, so the date "
        "says when they were moved in, not when I came to like them. They are left "
        "out of this table and counted everywhere else."
    )


def _bulk_lead(data: PageData) -> str:
    if not data.bulk_days:
        return ""
    songs = _n(sum(count for _, count in data.bulk_days))
    if len(data.bulk_days) == 1:
        return f" It leaves out the {songs} songs that arrived in one import on {_day(data.bulk_days[0][0])} ‡."
    return (
        f" It leaves out the {songs} songs that arrived in imports on {len(data.bulk_days)} days ‡."
    )


def _split_title(owner: str) -> str:
    """The owner's name in ink, the subject in the pen, as the site's masthead sets a name."""
    return f"{escape(owner)}’s library, <em>by genre and decade</em>"


def render_page(data: PageData) -> str:
    """The whole page as one HTML document."""
    owner = escape(data.owner)
    compiled = f"{data.compiled.day} {data.compiled:%B %Y}"
    span = f"{data.decades[0]}s–{data.decades[-1]}s" if data.decades else "—"

    key = (
        ("Songs, editions merged", _n(data.songs)),
        ("Genres", _n(data.genres)),
        ("Artists", _n(data.artists)),
        ("Decades", span),
        ("Unclassified †", _n(data.unclassified)),
    )
    key_html = "".join(
        f'<div><dt class="label">{escape(label)}</dt><dd>{value}</dd></div>' for label, value in key
    )

    notes = [
        "Spotify gives genres to artists, not to songs. Each song is filed under its main "
        "artist’s genres, so a guest verse never moves a song into another genre.",
        "A song can sit in more than one genre, so the rows of the first table overlap. "
        "They are not a breakdown of the total beneath them, and they leave out every "
        "genre below the busiest few.",
        "Remasters, reissues and deluxe editions of the same recording are counted once.",
        (
            "† 1 song has no genre: nobody I trust has classified its main artist. "
            "It is in every total and in no genre’s row."
            if data.unclassified == 1
            else f"† {_n(data.unclassified)} songs have no genre: nobody I trust has "
            "classified their main artist. They are in every total and in no genre’s row."
        ),
        'A figure marked <span class="peak-key">like this</span> is that genre’s busiest decade.',
        "In the year-liked table each year puts forward its two busiest genres, and a "
        "marked figure is the year that genre made up the largest share of what I liked. "
        "Some years I liked far more than others, so the year with the most songs would "
        "mark the same busy year on almost every row.",
        "A song liked again in another version (a remaster, a deluxe edition) counts from "
        "the first time it was liked.",
    ]
    if any(span.first != span.last for span in data.liked_years):
        notes.append(
            f"A year with fewer than {_MIN_YEAR_SONGS} liked songs shares a column with "
            "the years after it, so a column can span several years."
        )
    if data.bulk_days:
        notes.append(_bulk_note(data))
    if data.undated:
        notes.append(
            "1 song carries no release date and sits in no decade."
            if data.undated == 1
            else f"{_n(data.undated)} songs carry no release date and sit in no decade."
        )
    notes_html = "".join(f"<li><span>{note}</span></li>" for note in notes)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>{owner}’s library, by genre and decade</title>
<style>{_font_faces()}{_CSS}{_column_guides(max(len(data.decades), len(data.liked_years)))}</style>
</head>
<body>
<main>
<header class="masthead">
<div>
<p class="label"><span>Liked songs</span><span>Spotify</span><span>Compiled {compiled}</span></p>
<h1>{_split_title(data.owner)}</h1>
<p class="thesis">Every song I have liked, counted by the genre of its main artist and the decade it came out. Read along a row to see when a genre happened for me.</p>
</div>
<dl class="key">{key_html}</dl>
</header>
<section aria-labelledby="t1">
<p class="label"><span>01</span><span>Genres by decade</span></p>
<h2 id="t1">{_headline(data)}</h2>
<p class="lead">The {len(data.lines)} genres with the most songs, by the decade those songs came out.</p>
<div class="grid" role="region" aria-labelledby="t1" tabindex="0">{_genre_table(data)}</div>
</section>
<section aria-labelledby="t4">
<p class="label"><span>02</span><span>Genres by year liked</span></p>
<h2 id="t4">{_liked_headline(data)}</h2>
<p class="lead">The same library against the year I liked each song, not the year it came out. Each year’s two busiest genres have a row, in the order they took hold.{_bulk_lead(data)}</p>
<div class="grid" role="region" aria-labelledby="t4" tabindex="0">{_liked_table(data)}</div>
</section>
<section aria-labelledby="t2">
<p class="label"><span>03</span><span>Artists</span></p>
<h2 id="t2">The ones I keep coming back to.</h2>
<p class="lead">By songs liked, with the years those songs span.</p>
<div class="grid" role="region" aria-labelledby="t2" tabindex="0">{_artist_table(data)}</div>
</section>
<section aria-labelledby="t3">
<p class="label"><span>04</span><span>Albums</span></p>
<h2 id="t3">Albums I liked most of.</h2>
<p class="lead">Songs liked, against how many songs the album has.</p>
<div class="grid" role="region" aria-labelledby="t3" tabindex="0">{_album_table(data)}</div>
</section>
<section class="notes" aria-labelledby="notes">
<p class="label"><span>05</span><span>Method</span></p>
<h2 id="notes">How this was counted.</h2>
<ol>{notes_html}</ol>
</section>
<footer>
<p class="label"><span>Colophon</span></p>
<p>Counted and typeset by <a href="{REPOSITORY_URL}">spotifyforge</a>, a tool I wrote to sort my own library. Nothing on this page is fetched when you open it.</p>
</footer>
</main>
</body>
</html>
"""
