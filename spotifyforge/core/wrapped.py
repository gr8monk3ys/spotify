"""A year in plays, as one page: the listening log counted the way Wrapped counts.

``export wrapped`` writes a single HTML file from the local log that
``history sync`` keeps. Spotify's own Wrapped arrives once a year, covers
what Spotify decides it covers, and cannot be asked how it counted. This
page is drawn from a log on disk, so every figure on it can be checked,
and it can be made on any day.

That log is the page's whole evidence, and it is young: it starts the
day the hourly sync was first scheduled. So the page says where it
starts and ends before it says anything else, and flags every gap a sync
has recorded (:func:`spotifyforge.core.history.load_gaps`). The section
on songs played hard and then dropped stays empty, and says why, until
the log reaches a month past its first one.

Unlike the taste page, this one holds play history: what was played,
when, and from where. It is written for its owner, so the command only
writes a local file and the default name is ignored by git.

It shares the taste page's design system, stylesheet and embedded fonts
(:mod:`spotifyforge.core.taste_page`), and like it fetches nothing.
Every string from Spotify is escaped on the way in.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from html import escape

from spotifyforge.core.curation import song_key
from spotifyforge.core.history import Gap, Play, PlaylistUsage, playlist_usage
from spotifyforge.core.taste_page import (
    _CSS,
    REPOSITORY_URL,
    _cell,
    _column_guides,
    _font_faces,
    _n,
    _sub,
)

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

# A song needs this many plays in one calendar month to count as played hard.
HEAVY_PLAYS = 5
# ...and at most this share of that month's plays in every month since.
DROPPED_SHARE = 0.2
# How long the log must run past the end of a month before a song that
# went quiet after it can be called dropped rather than merely recent.
SETTLE_DAYS = 28


@dataclass(frozen=True)
class Row:
    name: str
    detail: str  # the artist, for songs and albums
    plays: int
    minutes: int
    songs: int = 0  # distinct songs, for artists


@dataclass(frozen=True)
class Dropped:
    name: str
    artist: str
    month: str  # "October 2026"
    peak: int
    since: int  # plays in every later month together


@dataclass(frozen=True)
class WrappedData:
    owner: str
    compiled: date
    year: int | None
    timezone: str
    plays: int
    minutes: int
    songs: int
    artists: int
    first: datetime | None  # local time
    last: datetime | None
    gaps: tuple[tuple[datetime, datetime], ...]  # (after, before), local time
    top_songs: tuple[Row, ...]
    top_artists: tuple[Row, ...]
    top_albums: tuple[Row, ...]
    grid: tuple[tuple[int, ...], ...]  # 24 hours down, Monday to Sunday across
    usage: PlaylistUsage
    dropped: tuple[Dropped, ...]
    dropped_from: date | None  # the day the log is long enough to name any

    @property
    def days(self) -> int:
        if self.first is None or self.last is None:
            return 0
        return (self.last.date() - self.first.date()).days + 1


def _month_end(day: date) -> date:
    following = date(day.year + day.month // 12, day.month % 12 + 1, 1)
    return following - timedelta(days=1)


def _song(play: Play) -> Hashable:
    return song_key(play.name, play.artists[0]) if play.artists else play.track_id


def _top(
    groups: Mapping[Hashable, list[Play]],
    top: int,
    name: str,
    detail: str | None = None,
) -> tuple[Row, ...]:
    """Rank *groups* by plays; *name* and *detail* pick the label from the first play."""

    def label(play: Play, field: str | None) -> str:
        if field is None:
            return ""
        if field == "artist":
            return ", ".join(play.artists)
        if field == "first_artist":
            return play.artists[0] if play.artists else ""
        return str(getattr(play, field))

    rows = [
        Row(
            name=label(group[0], name),
            detail=label(group[0], detail),
            plays=len(group),
            minutes=round(sum(p.duration_ms for p in group) / 60_000),
            songs=len({_song(p) for p in group}),
        )
        for group in groups.values()
    ]
    rows.sort(key=lambda row: (-row.plays, -row.minutes, row.name.casefold()))
    return tuple(rows[:top])


def _dropped(plays: Sequence[tuple[Play, datetime]], last: date) -> tuple[Dropped, ...]:
    by_song: dict[Hashable, Counter[tuple[int, int]]] = defaultdict(Counter)
    label: dict[Hashable, Play] = {}
    for play, local in plays:
        song = _song(play)
        by_song[song][(local.year, local.month)] += 1
        label.setdefault(song, play)

    out = []
    for song, months in by_song.items():
        (year, month), peak = max(months.items(), key=lambda item: (item[1], item[0]))
        if peak < HEAVY_PLAYS:
            continue
        if (last - _month_end(date(year, month, 1))).days < SETTLE_DAYS:
            continue  # the busy month is too recent to know whether it stopped
        since = sum(n for m, n in months.items() if m > (year, month))
        if since > peak * DROPPED_SHARE:
            continue
        play = label[song]
        out.append(
            Dropped(
                name=play.name,
                artist=", ".join(play.artists),
                month=f"{date(year, month, 1):%B %Y}",
                peak=peak,
                since=since,
            )
        )
    out.sort(key=lambda d: (-d.peak, d.since, d.name.casefold()))
    return tuple(out)


def build_wrapped(
    plays: Sequence[Play],
    owner: str,
    compiled: date,
    owned: Mapping[str, str],
    gaps: Sequence[Gap] = (),
    year: int | None = None,
    tz: tzinfo | None = None,
    top: int = 10,
) -> WrappedData:
    """Count *plays* (oldest first) for the page; *owned* maps playlist id to name.

    Hours and weekdays are read in *tz*, the machine's own zone when it
    is ``None``, because "when you listen" means the listener's clock.
    With *year*, only plays in that calendar year (in *tz*) are counted.
    """
    local = [(p, p.at.astimezone(tz)) for p in plays]
    if year is not None:
        local = [(p, at) for p, at in local if at.year == year]
    kept = [p for p, _ in local]

    by_song: dict[Hashable, list[Play]] = defaultdict(list)
    by_artist: dict[Hashable, list[Play]] = defaultdict(list)
    by_album: dict[Hashable, list[Play]] = defaultdict(list)
    for play in kept:
        by_song[_song(play)].append(play)
        if play.artists:
            by_artist[play.artist_ids[0] if play.artist_ids else play.artists[0]].append(play)
        if play.album_id or play.album:
            by_album[play.album_id or play.album].append(play)

    grid = [[0] * 7 for _ in range(24)]
    for _, at in local:
        grid[at.hour][at.weekday()] += 1

    first = local[0][1] if local else None
    last = local[-1][1] if local else None
    spans = (
        (datetime.fromisoformat(gap.after).astimezone(tz), datetime.fromisoformat(gap.before))
        for gap in gaps
    )
    window_gaps = tuple(
        (after, before.astimezone(tz))
        for after, before in spans
        if year is None or year in (after.year, before.astimezone(tz).year)
    )
    zone = (first or datetime.now().astimezone(tz)).tzname() or "UTC"

    return WrappedData(
        owner=owner,
        compiled=compiled,
        year=year,
        timezone=zone,
        plays=len(kept),
        minutes=round(sum(p.duration_ms for p in kept) / 60_000),
        songs=len(by_song),
        artists=len(by_artist),
        first=first,
        last=last,
        gaps=window_gaps,
        top_songs=_top(by_song, top, "name", "artist"),
        top_artists=_top(by_artist, top, "first_artist"),
        top_albums=_top(by_album, top, "album", "first_artist"),
        grid=tuple(tuple(row) for row in grid),
        usage=playlist_usage(kept, owned),
        dropped=_dropped(local, last.date()) if last else (),
        dropped_from=_month_end(first.date()) + timedelta(days=SETTLE_DAYS) if first else None,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
#
# The taste page's stylesheet, fonts and table helpers, unchanged: the
# same paper, the same one green pen, the same mono wall labels. What is
# this page's own is only what it counts.


def _day(moment: datetime | date) -> str:
    return f"{moment.day} {moment:%B %Y}"


def _hours(minutes: int) -> str:
    return f"{minutes / 60:,.1f}"


def _song_table(rows: Sequence[Row]) -> str:
    body = "".join(
        f'<tr><th scope="row">{escape(row.name)}{_sub(row.detail)}</th>'
        f'<td class="text">{escape(row.detail)}</td>'
        f"<td>{_n(row.minutes)}</td>"
        f'<td class="total">{_n(row.plays)}</td></tr>'
        for row in rows
    )
    return (
        "<table>"
        '<thead><tr><th scope="col">Song</th><th scope="col" class="text">Artist</th>'
        '<th scope="col">Minutes</th><th scope="col">Plays</th></tr></thead>'
        f"<tbody>{body}</tbody></table>"
    )


def _artist_table(rows: Sequence[Row]) -> str:
    body = "".join(
        f'<tr><th scope="row">{escape(row.name)}</th>'
        f"<td>{_n(row.songs)}</td>"
        f"<td>{_hours(row.minutes)}</td>"
        f'<td class="total">{_n(row.plays)}</td></tr>'
        for row in rows
    )
    return (
        "<table>"
        '<thead><tr><th scope="col">Artist</th><th scope="col">Songs</th>'
        '<th scope="col">Hours</th><th scope="col">Plays</th></tr></thead>'
        f"<tbody>{body}</tbody></table>"
    )


def _album_table(rows: Sequence[Row]) -> str:
    body = "".join(
        f'<tr><th scope="row">{escape(row.name)}{_sub(row.detail)}</th>'
        f'<td class="text">{escape(row.detail)}</td>'
        f"<td>{_n(row.songs)}</td>"
        f'<td class="total">{_n(row.plays)}</td></tr>'
        for row in rows
    )
    return (
        "<table>"
        '<thead><tr><th scope="col">Album</th><th scope="col" class="text">Artist</th>'
        '<th scope="col">Songs</th><th scope="col">Plays</th></tr></thead>'
        f"<tbody>{body}</tbody></table>"
    )


def _weekday(name: str) -> str:
    """ "Monday" in full; the stylesheet swaps to "Mo" on narrow screens."""
    return f'<span class="full">{name[:3]}</span><span class="short" aria-hidden="true">{name[:2]}</span>'


def _clock_table(data: WrappedData) -> str:
    peak = max((max(row) for row in data.grid), default=0)
    head = "".join(f'<th scope="col">{_weekday(day)}</th>' for day in WEEKDAYS)
    body = "".join(
        f'<tr><th scope="row">{hour:02d}:00</th>'
        + "".join(_cell(count, peak=bool(peak) and count == peak) for count in row)
        + f'<td class="total">{_n(sum(row))}</td></tr>'
        for hour, row in enumerate(data.grid)
    )
    foot = "".join(_cell(sum(row[day] for row in data.grid)) for day in range(7))
    return (
        "<table>"
        f'<thead><tr><th scope="col">Hour</th>{head}<th scope="col">Plays</th></tr></thead>'
        f"<tbody>{body}</tbody>"
        f'<tfoot><tr><th scope="row">Every hour</th>{foot}'
        f'<td class="total">{_n(data.plays)}</td></tr></tfoot>'
        "</table>"
    )


def _share(part: int, whole: int) -> str:
    return f"{round(100 * part / whole)}%" if whole else "—"


def _source_table(usage: PlaylistUsage) -> str:
    sources = (
        ("My own playlists", usage.from_own_playlists),
        ("Other playlists, followed or made by Spotify", usage.from_other_playlists),
        ("Albums", usage.from_albums),
        ("Artist pages", usage.from_artists),
        ("Elsewhere: liked songs, search, the queue", usage.from_elsewhere),
    )
    body = "".join(
        f'<tr><th scope="row">{label}</th><td>{_share(count, usage.plays)}</td>'
        f'<td class="total">{_n(count)}</td></tr>'
        for label, count in sources
    )
    return (
        "<table>"
        '<thead><tr><th scope="col">Started from</th><th scope="col">Share</th>'
        '<th scope="col">Plays</th></tr></thead>'
        f"<tbody>{body}</tbody></table>"
    )


def _playlist_table(usage: PlaylistUsage, top: int = 8) -> str:
    body = "".join(
        f'<tr><th scope="row">{escape(name)}</th><td class="total">{_n(count)}</td></tr>'
        for _pid, name, count in usage.played[:top]
    )
    return (
        "<table>"
        '<thead><tr><th scope="col">My playlist</th><th scope="col">Plays</th></tr></thead>'
        f"<tbody>{body}</tbody></table>"
    )


def _dropped_table(rows: Sequence[Dropped]) -> str:
    body = "".join(
        f'<tr><th scope="row">{escape(row.name)}{_sub(row.artist)}</th>'
        f'<td class="text">{escape(row.artist)}</td>'
        f"<td>{escape(row.month)}</td>"
        f"<td>{_n(row.peak)}</td>"
        f'<td class="total">{_n(row.since)}</td></tr>'
        for row in rows
    )
    return (
        "<table>"
        '<thead><tr><th scope="col">Song</th><th scope="col" class="text">Artist</th>'
        '<th scope="col">Busiest</th><th scope="col">Plays</th>'
        '<th scope="col">Since</th></tr></thead>'
        f"<tbody>{body}</tbody></table>"
    )


def _coverage(data: WrappedData) -> tuple[tuple[str, ...], str]:
    """The span the log covers and its recorded gaps, as wall-label parts and as a sentence."""
    if data.first is None or data.last is None:
        return ("Nothing logged yet",), "The log has no plays yet."
    span = f"Logged {_day(data.first)} – {_day(data.last)}"
    days = "1 day" if data.days == 1 else f"{_n(data.days)} days"
    if not data.gaps:
        return (
            (span, days, "No gaps recorded"),
            f"The log runs from {_day(data.first)} to {_day(data.last)}, {days}. "
            "No sync has recorded a possible gap in that time.",
        )
    listed = "; ".join(
        f"between {after:%d %b %H:%M} and {before:%d %b %H:%M}" for after, before in data.gaps
    )
    count = "1 possible gap" if len(data.gaps) == 1 else f"{len(data.gaps)} possible gaps"
    return (
        (span, days, count),
        f"The log runs from {_day(data.first)} to {_day(data.last)}, {days}, with {count}: "
        f"{listed}. Plays in a gap may be missing.",
    )


def _when_headline(data: WrappedData) -> str:
    if not data.plays:
        return "Nothing logged yet."
    # The marked cell, not the busiest day and the busiest hour taken
    # apart: those two can name an hour that day had no plays in.
    count, hour, day = max(
        (count, -hour, -day) for hour, row in enumerate(data.grid) for day, count in enumerate(row)
    )
    return f"Busiest on {WEEKDAYS[-day]}s at {-hour:02d}:00, with {_n(count)} plays."


def _where_headline(usage: PlaylistUsage) -> str:
    if not usage.plays:
        return "Nothing logged yet."
    return f"{_share(usage.from_own_playlists, usage.plays)} started from a playlist of my own."


_EXTRA_CSS = """
.coverage{margin:1.25rem 0 0}
.pair{display:grid;grid-template-columns:minmax(0,1.4fr) minmax(0,1fr);gap:2.5rem clamp(2rem,6vw,5rem)}
@media (max-width:52rem){.pair{grid-template-columns:1fr}}
"""


def render_wrapped(data: WrappedData) -> str:
    """The whole page as one HTML document."""
    owner = escape(data.owner)
    compiled = _day(data.compiled)
    label, sentence = _coverage(data)
    label_html = "".join(f"<span>{escape(part)}</span>" for part in label)
    subject = (
        f"in {data.year}"
        if data.year
        else (f"since {_day(data.first)}" if data.first else "from the log")
    )

    key = (
        ("Plays", _n(data.plays)),
        ("Hours †", _hours(data.minutes)),
        ("Songs", _n(data.songs)),
        ("Artists", _n(data.artists)),
        ("Days logged", _n(data.days)),
    )
    key_html = "".join(
        f'<div><dt class="label">{name}</dt><dd>{value}</dd></div>' for name, value in key
    )
    top_song = (
        f"“{escape(data.top_songs[0].name)}”, most of all."
        if data.top_songs
        else "Nothing logged yet."
    )

    if data.dropped:
        dropped = (
            '<p class="lead">At least 5 plays in one month, then at most a fifth of that in '
            "every month since.</p>"
            f'<div class="grid" role="region" aria-labelledby="t6" tabindex="0">'
            f"{_dropped_table(data.dropped)}</div>"
        )
    elif data.dropped_from and data.dropped_from > (data.last.date() if data.last else date.min):
        dropped = (
            '<p class="lead">Not yet. A song can only be called dropped once the log has run '
            f"{SETTLE_DAYS} days past the end of the month it was busiest in, and this log "
            f"is not that long. It can first name one on {_day(data.dropped_from)}.</p>"
        )
    else:
        dropped = '<p class="lead">None. No song had 5 plays in a month and then fell away.</p>'

    playlists = (
        '<div><p class="lead">My own playlists, by plays started from them.</p>'
        f'<div class="grid" role="region" aria-label="My playlists by plays" tabindex="0">'
        f"{_playlist_table(data.usage)}</div></div>"
        if data.usage.played
        else '<div><p class="lead">No play was started from a playlist of my own.</p></div>'
    )

    notes = [
        sentence,
        "A play is one entry in Spotify’s recently played list, which the log reads every "
        "hour. Spotify keeps only the last fifty, so a gap is recorded when a sync finds all "
        "fifty new: older plays may have been lost before they were read.",
        "† Hours add up the full length of every song played, whether or not it was played "
        "to the end, so they are an upper bound.",
        f"Hours of the day are on the clock of the machine that wrote the page ({escape(data.timezone)}).",
        "Editions of a song (remasters, reissues) are counted as one song.",
        "This page holds play history. It was written to a file on my own machine and "
        "published nowhere.",
    ]
    notes_html = "".join(f"<li><span>{note}</span></li>" for note in notes)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="robots" content="noindex">
<title>{owner}’s listening, {subject}</title>
<style>{_font_faces()}{_CSS}{_EXTRA_CSS}{_column_guides(7)}</style>
</head>
<body>
<main>
<header class="masthead">
<div>
<p class="label"><span>Listening log</span><span>Spotify</span><span>Compiled {compiled}</span></p>
<h1>{owner}’s listening, <em>{subject}</em></h1>
<p class="thesis">Every play the hourly log has recorded, counted: what I played, when, and where I started it.</p>
<p class="label coverage">{label_html}</p>
</div>
<dl class="key">{key_html}</dl>
</header>
<section aria-labelledby="t1">
<p class="label"><span>01</span><span>Songs</span></p>
<h2 id="t1">{top_song}</h2>
<p class="lead">The {len(data.top_songs)} songs played most, of {_n(data.songs)} played at all.</p>
<div class="grid" role="region" aria-labelledby="t1" tabindex="0">{_song_table(data.top_songs)}</div>
</section>
<section aria-labelledby="t2">
<p class="label"><span>02</span><span>Artists</span></p>
<h2 id="t2">The ones on repeat.</h2>
<p class="lead">By plays, with how many of their songs those plays covered.</p>
<div class="grid" role="region" aria-labelledby="t2" tabindex="0">{_artist_table(data.top_artists)}</div>
</section>
<section aria-labelledby="t3">
<p class="label"><span>03</span><span>Albums</span></p>
<h2 id="t3">Albums played from most.</h2>
<p class="lead">Plays of any song on the album, however the play was started.</p>
<div class="grid" role="region" aria-labelledby="t3" tabindex="0">{_album_table(data.top_albums)}</div>
</section>
<section aria-labelledby="t4">
<p class="label"><span>04</span><span>When</span></p>
<h2 id="t4">{_when_headline(data)}</h2>
<p class="lead">Plays by the hour they started and the day of the week. The busiest hour is marked <span class="peak-key">like this</span>.</p>
<div class="grid" role="region" aria-labelledby="t4" tabindex="0">{_clock_table(data)}</div>
</section>
<section aria-labelledby="t5">
<p class="label"><span>05</span><span>Where</span></p>
<h2 id="t5">{_where_headline(data.usage)}</h2>
<div class="pair">
<div><p class="lead">Where each play was started, as Spotify reports it.</p>
<div class="grid" role="region" aria-label="Plays by where they started" tabindex="0">{_source_table(data.usage)}</div></div>
{playlists}
</div>
</section>
<section aria-labelledby="t6">
<p class="label"><span>06</span><span>Dropped</span></p>
<h2 id="t6">Played hard, then dropped.</h2>
{dropped}
</section>
<section class="notes" aria-labelledby="notes">
<p class="label"><span>07</span><span>Method</span></p>
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
