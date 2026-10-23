"""`spotifyforge discover` commands."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

import typer
from rich import box
from rich.panel import Panel
from rich.table import Table

from spotifyforge.cli._shared import (
    _run_spotify,
    console,
)

# ╔═════════════════════════════════════════════════════════════════════════╗
# ║  DISCOVER                                                              ║
# ╚═════════════════════════════════════════════════════════════════════════╝
discover_app = typer.Typer(
    name="discover",
    help="Discover new music through intelligent analysis.",
    no_args_is_help=True,
)


class TimeRange(StrEnum):
    """Spotify time range for personalization endpoints."""

    short_term = "short_term"
    medium_term = "medium_term"
    long_term = "long_term"


_RANGE_LABELS = {
    "short_term": "Last 4 Weeks",
    "medium_term": "Last 6 Months",
    "long_term": "All Time",
}


def _artist_names(track: Any) -> str:
    return ", ".join(a.name for a in track.artists) if track.artists else "Unknown"


@discover_app.command("top-tracks")
def discover_top_tracks(
    time_range: TimeRange = typer.Option(
        TimeRange.medium_term,
        "--time-range",
        "-t",
        help="Time range: short_term (~4 weeks), medium_term (~6 months), long_term (years).",
        case_sensitive=False,
    ),
    limit: int = typer.Option(
        20,
        "--limit",
        "-l",
        min=1,
        max=50,
        help="Number of top tracks to display (1-50).",
    ),
) -> None:
    """Show your top tracks on Spotify."""
    from spotifyforge.core.discovery import DiscoveryEngine

    tracks = _run_spotify(
        "Fetching your top tracks...",
        "Failed to fetch top tracks",
        lambda sp: DiscoveryEngine(sp).get_user_top_tracks(
            time_range=time_range.value, limit=limit
        ),
    )

    if not tracks:
        console.print("[yellow]No top tracks found for the selected time range.[/yellow]")
        return

    table = Table(
        title=f"Your Top Tracks — {_RANGE_LABELS.get(time_range.value, time_range.value)}",
        box=box.ROUNDED,
        header_style="bold magenta",
    )
    table.add_column("#", style="dim", justify="right")
    table.add_column("Title", style="bold white", no_wrap=True, max_width=50)
    table.add_column("Artist", style="green", no_wrap=True, max_width=35)
    table.add_column("Album", style="dim", no_wrap=True, max_width=35)
    table.add_column("Popularity", justify="right", style="cyan")

    for idx, track in enumerate(tracks, start=1):
        table.add_row(
            str(idx),
            track.name or "—",
            _artist_names(track),
            track.album.name if track.album else "—",
            f"{track.popularity or 0}/100",
        )

    console.print(table)


@discover_app.command("deep-cuts")
def discover_deep_cuts(
    artist_id: str = typer.Argument(..., help="Spotify artist ID."),
    threshold: int = typer.Option(
        30,
        "--threshold",
        "-t",
        min=0,
        max=100,
        help="Tracks with popularity strictly below this value qualify (0-100).",
    ),
) -> None:
    """Find an artist's lesser-known tracks (deep cuts)."""
    from spotifyforge.core.discovery import DiscoveryEngine

    tracks = _run_spotify(
        f"Searching for deep cuts (popularity < {threshold})...",
        "Failed to find deep cuts",
        lambda sp: DiscoveryEngine(sp).find_deep_cuts(
            artist_id=artist_id, popularity_threshold=threshold
        ),
    )

    if not tracks:
        console.print(
            f"[yellow]No deep cuts found for artist [bold]{artist_id}[/bold] "
            f"with popularity below {threshold}.[/yellow]"
        )
        return

    console.print(
        Panel(
            f"Found [bold]{len(tracks)}[/bold] deep cuts (popularity < {threshold})",
            border_style="cyan",
            expand=False,
        )
    )

    table = Table(box=box.SIMPLE_HEAVY, header_style="bold cyan")
    table.add_column("#", style="dim", justify="right")
    table.add_column("Track", style="white", no_wrap=True, max_width=50)
    table.add_column("Album", style="dim", no_wrap=True, max_width=35)
    table.add_column("Popularity", justify="right", style="yellow")
    table.add_column("Duration", justify="right", style="cyan")

    for idx, track in enumerate(tracks, start=1):
        duration_ms = track.duration_ms or 0
        minutes, seconds = divmod(duration_ms // 1000, 60)
        table.add_row(
            str(idx),
            track.name or "—",
            track.album.name if track.album else "—",
            str(track.popularity or 0),
            f"{minutes}:{seconds:02d}",
        )

    console.print(table)


@discover_app.command("genre")
def discover_genre(
    genre_name: str = typer.Argument(..., help="Genre name (e.g. 'indie-rock', 'trip-hop')."),
    limit: int = typer.Option(
        25,
        "--limit",
        "-l",
        min=1,
        max=50,
        help="Number of tracks to include in the genre playlist (1-50).",
    ),
    name: str | None = typer.Option(
        None, "--name", "-n", help="Playlist name (auto-generated if omitted)."
    ),
) -> None:
    """Create a playlist populated with tracks from a genre search."""
    from spotifyforge.core.discovery import DiscoveryEngine
    from spotifyforge.core.playlist_manager import PlaylistManager

    async def _build(sp):
        tracks = await DiscoveryEngine(sp).build_genre_playlist(genre=genre_name, limit=limit)
        playlist = await PlaylistManager(sp).create_playlist_with_tracks(
            name=name or f"SpotifyForge: {genre_name.title()}",
            tracks=tracks,
            description=f"Genre playlist: {genre_name}",
        )
        return playlist, tracks

    playlist, tracks = _run_spotify(
        f"Building genre playlist for '{genre_name}'...",
        "Failed to build genre playlist",
        _build,
    )

    console.print(
        Panel(
            f"[green]Genre playlist created![/green]\n\n"
            f"  Name:       [bold]{playlist.name}[/bold]\n"
            f"  Spotify ID: {playlist.spotify_id}\n"
            f"  Tracks:     {len(tracks)}",
            title=f"Genre: {genre_name}",
            border_style="green",
            expand=False,
        )
    )

    if tracks:
        table = Table(box=box.SIMPLE, header_style="bold cyan")
        table.add_column("#", style="dim", justify="right")
        table.add_column("Title", style="white", no_wrap=True, max_width=50)
        table.add_column("Artist", style="green", no_wrap=True, max_width=35)
        for idx, track in enumerate(tracks, start=1):
            table.add_row(str(idx), track.name or "—", _artist_names(track))
        console.print(table)


@discover_app.command("time-capsule")
def discover_time_capsule(
    time_range: TimeRange = typer.Option(
        TimeRange.long_term,
        "--time-range",
        "-t",
        help="Time range for the capsule: short_term, medium_term, long_term.",
        case_sensitive=False,
    ),
    name: str | None = typer.Option(
        None, "--name", "-n", help="Playlist name (auto-generated if omitted)."
    ),
) -> None:
    """Create a time-capsule playlist from your listening history."""
    from spotifyforge.core.discovery import DiscoveryEngine
    from spotifyforge.core.playlist_manager import PlaylistManager

    async def _build(sp):
        tracks = await DiscoveryEngine(sp).build_time_capsule(time_range=time_range.value)
        playlist = await PlaylistManager(sp).create_playlist_with_tracks(
            name=name or f"SpotifyForge: Time Capsule ({time_range.value})",
            tracks=tracks,
            description=f"Time capsule playlist ({time_range.value})",
            public=False,
        )
        return playlist, len(tracks)

    playlist, track_count = _run_spotify(
        "Building your time capsule...", "Failed to create time capsule", _build
    )

    console.print(
        Panel(
            f"[green]Time capsule created![/green]\n\n"
            f"  Name:       [bold]{playlist.name}[/bold]\n"
            f"  Spotify ID: {playlist.spotify_id}\n"
            f"  Tracks:     {track_count}\n"
            f"  Time range: {_RANGE_LABELS.get(time_range.value, time_range.value)}",
            title="Time Capsule",
            border_style="magenta",
            expand=False,
        )
    )


@discover_app.command("shows")
def discover_shows(
    days: int = typer.Option(
        90, "--days", "-d", min=1, max=365, help="How far ahead to look, in days."
    ),
    artists: int = typer.Option(
        50,
        "--artists",
        "-a",
        min=1,
        max=150,
        help="How many of your most-played artists to check (two requests each).",
    ),
    near: str | None = typer.Option(
        None,
        "--near",
        help='Home for this run: "lat,long" or a city. Default: SPOTIFYFORGE_HOME_LOCATION.',
    ),
    radius: int | None = typer.Option(
        None,
        "--radius",
        "-r",
        min=1,
        max=500,
        help="Miles around a lat,long home. Default: SPOTIFYFORGE_HOME_RADIUS_MILES.",
    ),
) -> None:
    """Upcoming shows near home by the artists you play most (via Ticketmaster).

    Your artists are Spotify's top artists over all three time ranges,
    plus anything the local listening log shows you playing. Read-only:
    nothing is written to Spotify or kept on disk.
    """
    from rich.markup import escape
    from rich.style import Style
    from rich.text import Text

    from spotifyforge.cli._shared import _error_panel
    from spotifyforge.config import settings
    from spotifyforge.core.shows import HOW_TO_GET_A_KEY, Home, upcoming_shows

    if not settings.ticketmaster_api_key:
        _error_panel(HOW_TO_GET_A_KEY, title="Ticketmaster key needed")
    try:
        home = Home.parse(near or settings.home_location, radius or settings.home_radius_miles)
    except ValueError as exc:
        _error_panel(escape(str(exc)), title="Home location needed")

    report = _run_spotify(
        f"Looking for shows {home.describe()}...",
        "Failed to look up shows",
        lambda sp: upcoming_shows(
            sp, home, days, settings.ticketmaster_api_key, artist_limit=artists
        ),
    )

    by_name = sum(1 for a in report.matched if a.confidence == "name")
    console.print(
        Panel(
            f"[bold]{len(report.shows)}[/bold] show(s) {escape(home.describe())} "
            f"in the next {days} days.\n"
            f"Checked {report.artists_checked} of your artists: "
            f"{len(report.matched)} found on Ticketmaster"
            + (f" ({by_name} by name only)" if by_name else "")
            + f", {len(report.ambiguous)} ambiguous, "
            f"{len(report.not_on_ticketmaster)} not found. "
            f"[dim]{report.requests} requests.[/dim]",
            title="Shows",
            border_style="cyan",
            expand=False,
        )
    )

    if report.shows:
        table = Table(box=box.SIMPLE_HEAVY, header_style="bold cyan")
        table.add_column("Date", style="cyan", no_wrap=True)
        table.add_column("Artist", style="green", max_width=30)
        table.add_column("Event", style="white", max_width=45)
        table.add_column("Venue", style="dim", max_width=40)
        table.add_column("Miles", justify="right", style="yellow")
        for show in report.shows:
            # Text cells are never parsed as markup, and the core has already
            # stripped control characters, so an event or venue name cannot
            # inject styling or a link of its own. show.url is only ever an
            # https Ticketmaster/Live Nation page (core.shows.safe_event_url).
            event = Text(show.name, style=Style(link=show.url) if show.url else "")
            if show.status and show.status != "onsale":
                event.append(f" ({show.status})", style="yellow")
            where = ", ".join(p for p in (show.venue, show.city, show.region) if p)
            artist = Text(", ".join(show.artists))
            if show.confidence == "name":
                artist.append(" *", style="yellow")
            table.add_row(
                Text(" ".join(p for p in (show.local_date, (show.local_time or "")[:5]) if p)),
                artist,
                event,
                Text(where or "—"),
                f"{show.distance_miles:.0f}" if show.distance_miles is not None else "—",
            )
        console.print(table)
        if any(s.confidence == "name" for s in report.shows):
            console.print(
                "[yellow]*[/yellow] [dim]Matched by name only: Ticketmaster does not link "
                "this act to the Spotify artist, so check it is the same one.[/dim]"
            )

    if report.ambiguous:
        listed = ", ".join(f"{escape(name)} ({count})" for name, count in report.ambiguous)
        console.print(f"[dim]Skipped, as several Ticketmaster acts share the name: {listed}.[/dim]")
    if report.truncated:
        console.print(
            "[dim]More shows exist than were listed for: "
            f"{escape(', '.join(report.truncated))}.[/dim]"
        )
