"""`spotifyforge discover` commands."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

import typer
from rich import box
from rich.panel import Panel
from rich.table import Table

from spotifyforge.cli._shared import (
    _db_user_id,
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

    owner_id = _db_user_id()

    async def _build(sp):
        tracks = await DiscoveryEngine(sp).build_genre_playlist(genre=genre_name, limit=limit)
        playlist = await PlaylistManager(sp).create_playlist_with_tracks(
            name=name or f"SpotifyForge: {genre_name.title()}",
            owner_id=owner_id,
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

    owner_id = _db_user_id()

    async def _build(sp):
        tracks = await DiscoveryEngine(sp).build_time_capsule(time_range=time_range.value)
        playlist = await PlaylistManager(sp).create_playlist_with_tracks(
            name=name or f"SpotifyForge: Time Capsule ({time_range.value})",
            owner_id=owner_id,
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
