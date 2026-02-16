"""SpotifyForge CLI — the main user-facing interface.

Built with Typer + Rich.  Every sub-command group is its own ``typer.Typer``
instance, added to the root ``app`` via ``app.add_typer()``.

Entry-point (registered in ``pyproject.toml``):
    spotifyforge = "spotifyforge.cli.app:app"
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import sys
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

import spotifyforge
from spotifyforge.config import Settings, settings

# ---------------------------------------------------------------------------
# Console singleton
# ---------------------------------------------------------------------------
console = Console()
err_console = Console(stderr=True)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _error_panel(message: str, *, title: str = "Error") -> None:
    """Display a Rich error panel on *stderr* and exit with code 1."""
    err_console.print(Panel(message, title=title, border_style="red", expand=False))
    raise typer.Exit(code=1)


def _run(coro):
    """Convenience wrapper around ``asyncio.run`` for async core methods."""
    return asyncio.run(coro)


def _version_callback(value: bool) -> None:
    """Print the version string and exit."""
    if value:
        console.print(
            f"[bold]SpotifyForge[/bold] version [cyan]{spotifyforge.__version__}[/cyan]"
        )
        raise typer.Exit()


# ---------------------------------------------------------------------------
# Root application
# ---------------------------------------------------------------------------
app = typer.Typer(
    name="spotifyforge",
    help="SpotifyForge — the all-in-one platform for serious Spotify playlist curators.",
    rich_markup_mode="rich",
    no_args_is_help=True,
)


@app.callback()
def main(
    version: Optional[bool] = typer.Option(  # noqa: UP007
        None,
        "--version",
        "-V",
        help="Show the application version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """SpotifyForge CLI — curate, discover, and schedule Spotify playlists."""


# ╔═════════════════════════════════════════════════════════════════════════╗
# ║  AUTH                                                                  ║
# ╚═════════════════════════════════════════════════════════════════════════╝
auth_app = typer.Typer(
    name="auth",
    help="Manage Spotify authentication (OAuth 2.0 PKCE).",
    no_args_is_help=True,
)
app.add_typer(auth_app)


@auth_app.command("login")
def auth_login() -> None:
    """Open the browser for Spotify OAuth and store the access token."""
    try:
        from spotifyforge.auth.oauth import SpotifyAuth
    except Exception as exc:
        _error_panel(f"Failed to import auth module: {exc}")

    auth = SpotifyAuth()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task("Opening browser for Spotify login...", total=None)
        try:
            _run(auth.login())
        except Exception as exc:
            _error_panel(f"Login failed: {exc}", title="Authentication Error")

    console.print(
        Panel(
            "[green]Successfully authenticated with Spotify![/green]\n"
            "Your access token has been stored securely.",
            title="Login Successful",
            border_style="green",
            expand=False,
        )
    )


@auth_app.command("status")
def auth_status() -> None:
    """Display the current authentication status."""
    try:
        from spotifyforge.auth.oauth import SpotifyAuth
    except Exception as exc:
        _error_panel(f"Failed to import auth module: {exc}")

    auth = SpotifyAuth()

    try:
        status = _run(auth.status())
    except Exception as exc:
        _error_panel(f"Could not retrieve auth status: {exc}")

    if not status.get("logged_in"):
        console.print(
            Panel(
                "[yellow]Not logged in.[/yellow]\n"
                "Run [bold]spotifyforge auth login[/bold] to authenticate.",
                title="Auth Status",
                border_style="yellow",
                expand=False,
            )
        )
        return

    table = Table(title="Auth Status", box=box.ROUNDED, show_lines=True)
    table.add_column("Property", style="bold cyan")
    table.add_column("Value", style="white")

    table.add_row("User", status.get("display_name", "N/A"))
    table.add_row("Email", status.get("email", "N/A"))
    table.add_row("User ID", status.get("user_id", "N/A"))
    table.add_row("Token Expiry", status.get("token_expiry", "N/A"))
    table.add_row(
        "Status",
        "[green]Active[/green]" if status.get("token_valid") else "[red]Expired[/red]",
    )

    console.print(table)


@auth_app.command("logout")
def auth_logout() -> None:
    """Remove stored Spotify tokens."""
    try:
        from spotifyforge.auth.oauth import SpotifyAuth
    except Exception as exc:
        _error_panel(f"Failed to import auth module: {exc}")

    auth = SpotifyAuth()

    try:
        _run(auth.logout())
    except Exception as exc:
        _error_panel(f"Logout failed: {exc}")

    console.print(
        Panel(
            "[green]Logged out successfully.[/green]\nAll stored tokens have been removed.",
            title="Logout",
            border_style="green",
            expand=False,
        )
    )


# ╔═════════════════════════════════════════════════════════════════════════╗
# ║  PLAYLIST                                                              ║
# ╚═════════════════════════════════════════════════════════════════════════╝
playlist_app = typer.Typer(
    name="playlist",
    help="Manage and curate your Spotify playlists.",
    no_args_is_help=True,
)
app.add_typer(playlist_app)


@playlist_app.command("list")
def playlist_list() -> None:
    """Show all user playlists in a Rich table."""
    try:
        from spotifyforge.core.playlist_manager import PlaylistManager
    except Exception as exc:
        _error_panel(f"Failed to import playlist manager: {exc}")

    manager = PlaylistManager()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task("Fetching playlists...", total=None)
        try:
            playlists = _run(manager.get_user_playlists())
        except Exception as exc:
            _error_panel(f"Failed to fetch playlists: {exc}")

    if not playlists:
        console.print("[yellow]No playlists found.[/yellow]")
        return

    table = Table(
        title="Your Playlists",
        box=box.ROUNDED,
        show_lines=True,
        header_style="bold magenta",
    )
    table.add_column("#", style="dim", justify="right")
    table.add_column("Name", style="bold white", no_wrap=True)
    table.add_column("Tracks", justify="right", style="cyan")
    table.add_column("Visibility", justify="center")
    table.add_column("Followers", justify="right", style="green")
    table.add_column("ID", style="dim")

    for idx, pl in enumerate(playlists, start=1):
        visibility = (
            "[green]Public[/green]" if pl.get("public") else "[yellow]Private[/yellow]"
        )
        table.add_row(
            str(idx),
            pl.get("name", "—"),
            str(pl.get("track_count", 0)),
            visibility,
            str(pl.get("followers", 0)),
            pl.get("id", "—"),
        )

    console.print(table)


@playlist_app.command("show")
def playlist_show(
    playlist_id: str = typer.Argument(..., help="Spotify playlist ID to inspect."),
) -> None:
    """Display playlist details and its tracks."""
    try:
        from spotifyforge.core.playlist_manager import PlaylistManager
    except Exception as exc:
        _error_panel(f"Failed to import playlist manager: {exc}")

    manager = PlaylistManager()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task("Loading playlist details...", total=None)
        try:
            details = _run(manager.get_playlist_details(playlist_id))
        except Exception as exc:
            _error_panel(f"Failed to fetch playlist: {exc}")

    # -- Header panel --
    meta = details.get("meta", {})
    visibility = "Public" if meta.get("public") else "Private"
    header_text = (
        f"[bold]{meta.get('name', 'Unknown')}[/bold]\n"
        f"{meta.get('description', '')}\n\n"
        f"Owner: {meta.get('owner', 'N/A')}  |  "
        f"Tracks: {meta.get('track_count', 0)}  |  "
        f"Followers: {meta.get('followers', 0)}  |  "
        f"Visibility: {visibility}"
    )
    console.print(Panel(header_text, title="Playlist Details", border_style="cyan", expand=False))

    # -- Tracks table --
    tracks = details.get("tracks", [])
    if not tracks:
        console.print("[yellow]Playlist has no tracks.[/yellow]")
        return

    table = Table(box=box.SIMPLE_HEAVY, show_lines=False, header_style="bold cyan")
    table.add_column("#", style="dim", justify="right")
    table.add_column("Title", style="white", no_wrap=True, max_width=50)
    table.add_column("Artist", style="green", no_wrap=True, max_width=35)
    table.add_column("Album", style="dim", no_wrap=True, max_width=35)
    table.add_column("Duration", justify="right", style="cyan")

    for idx, track in enumerate(tracks, start=1):
        duration_ms = track.get("duration_ms", 0)
        minutes, seconds = divmod(duration_ms // 1000, 60)
        table.add_row(
            str(idx),
            track.get("name", "—"),
            track.get("artist", "—"),
            track.get("album", "—"),
            f"{minutes}:{seconds:02d}",
        )

    console.print(table)


@playlist_app.command("create")
def playlist_create(
    name: str = typer.Argument(..., help="Name for the new playlist."),
    description: str = typer.Option("", "--description", "-d", help="Playlist description."),
    public: bool = typer.Option(
        True,
        "--public/--private",
        help="Whether the playlist should be public (default) or private.",
    ),
) -> None:
    """Create a new Spotify playlist."""
    try:
        from spotifyforge.core.playlist_manager import PlaylistManager
    except Exception as exc:
        _error_panel(f"Failed to import playlist manager: {exc}")

    manager = PlaylistManager()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task("Creating playlist...", total=None)
        try:
            result = _run(manager.create_playlist(name=name, description=description, public=public))
        except Exception as exc:
            _error_panel(f"Failed to create playlist: {exc}")

    console.print(
        Panel(
            f"[green]Playlist created![/green]\n\n"
            f"  Name:        [bold]{result.get('name', name)}[/bold]\n"
            f"  ID:          {result.get('id', 'N/A')}\n"
            f"  Visibility:  {'Public' if public else 'Private'}\n"
            f"  Description: {description or '(none)'}",
            title="New Playlist",
            border_style="green",
            expand=False,
        )
    )


@playlist_app.command("sync")
def playlist_sync(
    playlist_id: str = typer.Argument(..., help="Spotify playlist ID to sync."),
) -> None:
    """Sync a playlist to the local cache database."""
    try:
        from spotifyforge.core.playlist_manager import PlaylistManager
    except Exception as exc:
        _error_panel(f"Failed to import playlist manager: {exc}")

    manager = PlaylistManager()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Syncing playlist...", total=None)
        try:
            result = _run(manager.sync_playlist(playlist_id))
        except Exception as exc:
            _error_panel(f"Sync failed: {exc}")
        progress.update(task, completed=True)

    tracks_synced = result.get("tracks_synced", 0)
    console.print(
        Panel(
            f"[green]Playlist synced to local cache.[/green]\n\n"
            f"  Playlist: [bold]{result.get('name', playlist_id)}[/bold]\n"
            f"  Tracks synced: {tracks_synced}\n"
            f"  Last synced: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
            title="Sync Complete",
            border_style="green",
            expand=False,
        )
    )


@playlist_app.command("deduplicate")
def playlist_deduplicate(
    playlist_id: str = typer.Argument(..., help="Spotify playlist ID to deduplicate."),
) -> None:
    """Find and remove duplicate tracks from a playlist."""
    try:
        from spotifyforge.core.playlist_manager import PlaylistManager
    except Exception as exc:
        _error_panel(f"Failed to import playlist manager: {exc}")

    manager = PlaylistManager()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task("Scanning for duplicates...", total=None)
        try:
            result = _run(manager.deduplicate_playlist(playlist_id))
        except Exception as exc:
            _error_panel(f"Deduplication failed: {exc}")

    removed = result.get("removed", 0)
    if removed == 0:
        console.print(
            Panel(
                "[green]No duplicates found![/green] Your playlist is already clean.",
                title="Deduplication",
                border_style="green",
                expand=False,
            )
        )
    else:
        console.print(
            Panel(
                f"[green]Deduplication complete.[/green]\n\n"
                f"  Removed [bold]{removed}[/bold] duplicate track(s).\n"
                f"  Remaining tracks: {result.get('remaining', 'N/A')}",
                title="Deduplication",
                border_style="green",
                expand=False,
            )
        )


class ExportFormat(str, Enum):
    """Supported playlist export formats."""
    csv = "csv"
    json = "json"


@playlist_app.command("export")
def playlist_export(
    playlist_id: str = typer.Argument(..., help="Spotify playlist ID to export."),
    format: ExportFormat = typer.Option(
        ExportFormat.json,
        "--format",
        "-f",
        help="Export format: csv or json.",
        case_sensitive=False,
    ),
    output: Optional[Path] = typer.Option(  # noqa: UP007
        None,
        "--output",
        "-o",
        help="Output file path. Defaults to stdout.",
    ),
) -> None:
    """Export playlist tracks to CSV or JSON."""
    try:
        from spotifyforge.core.playlist_manager import PlaylistManager
    except Exception as exc:
        _error_panel(f"Failed to import playlist manager: {exc}")

    manager = PlaylistManager()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task("Fetching playlist for export...", total=None)
        try:
            details = _run(manager.get_playlist_details(playlist_id))
        except Exception as exc:
            _error_panel(f"Export failed: {exc}")

    tracks = details.get("tracks", [])
    if not tracks:
        _error_panel("Playlist has no tracks to export.")

    if format == ExportFormat.json:
        export_data = json.dumps(tracks, indent=2, ensure_ascii=False)
    else:
        buf = io.StringIO()
        fieldnames = ["name", "artist", "album", "duration_ms", "uri"]
        writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(tracks)
        export_data = buf.getvalue()

    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(export_data, encoding="utf-8")
        console.print(
            f"[green]Exported {len(tracks)} tracks to[/green] [bold]{output}[/bold]"
        )
    else:
        console.print(export_data)


# ╔═════════════════════════════════════════════════════════════════════════╗
# ║  DISCOVER                                                              ║
# ╚═════════════════════════════════════════════════════════════════════════╝
discover_app = typer.Typer(
    name="discover",
    help="Discover new music through intelligent analysis.",
    no_args_is_help=True,
)
app.add_typer(discover_app)


class TimeRange(str, Enum):
    """Spotify time range for personalization endpoints."""
    short_term = "short_term"
    medium_term = "medium_term"
    long_term = "long_term"


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
    try:
        from spotifyforge.core.discovery import Discovery
    except Exception as exc:
        _error_panel(f"Failed to import discovery module: {exc}")

    discovery = Discovery()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task("Fetching your top tracks...", total=None)
        try:
            tracks = _run(discovery.get_top_tracks(time_range=time_range.value, limit=limit))
        except Exception as exc:
            _error_panel(f"Failed to fetch top tracks: {exc}")

    if not tracks:
        console.print("[yellow]No top tracks found for the selected time range.[/yellow]")
        return

    range_labels = {
        "short_term": "Last 4 Weeks",
        "medium_term": "Last 6 Months",
        "long_term": "All Time",
    }

    table = Table(
        title=f"Your Top Tracks — {range_labels.get(time_range.value, time_range.value)}",
        box=box.ROUNDED,
        header_style="bold magenta",
    )
    table.add_column("#", style="dim", justify="right")
    table.add_column("Title", style="bold white", no_wrap=True, max_width=50)
    table.add_column("Artist", style="green", no_wrap=True, max_width=35)
    table.add_column("Album", style="dim", no_wrap=True, max_width=35)
    table.add_column("Popularity", justify="right", style="cyan")

    for idx, track in enumerate(tracks, start=1):
        popularity = track.get("popularity", 0)
        pop_display = f"{popularity}/100"
        table.add_row(
            str(idx),
            track.get("name", "—"),
            track.get("artist", "—"),
            track.get("album", "—"),
            pop_display,
        )

    console.print(table)


@discover_app.command("deep-cuts")
def discover_deep_cuts(
    artist: str = typer.Argument(
        ..., help="Artist name or Spotify artist ID."
    ),
    threshold: int = typer.Option(
        30,
        "--threshold",
        "-t",
        min=0,
        max=100,
        help="Maximum popularity score to qualify as a deep cut (0-100).",
    ),
) -> None:
    """Find an artist's lesser-known tracks (deep cuts)."""
    try:
        from spotifyforge.core.discovery import Discovery
    except Exception as exc:
        _error_panel(f"Failed to import discovery module: {exc}")

    discovery = Discovery()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task(f"Searching for deep cuts (popularity < {threshold})...", total=None)
        try:
            result = _run(discovery.find_deep_cuts(artist=artist, threshold=threshold))
        except Exception as exc:
            _error_panel(f"Failed to find deep cuts: {exc}")

    tracks = result.get("tracks", [])
    artist_name = result.get("artist_name", artist)

    if not tracks:
        console.print(
            f"[yellow]No deep cuts found for [bold]{artist_name}[/bold] "
            f"with popularity below {threshold}.[/yellow]"
        )
        return

    console.print(
        Panel(
            f"Found [bold]{len(tracks)}[/bold] deep cuts for "
            f"[bold cyan]{artist_name}[/bold cyan] "
            f"(popularity < {threshold})",
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
        duration_ms = track.get("duration_ms", 0)
        minutes, seconds = divmod(duration_ms // 1000, 60)
        table.add_row(
            str(idx),
            track.get("name", "—"),
            track.get("album", "—"),
            str(track.get("popularity", 0)),
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
        max=100,
        help="Number of tracks to include in the genre playlist (1-100).",
    ),
) -> None:
    """Build a playlist from a specific genre."""
    try:
        from spotifyforge.core.discovery import Discovery
    except Exception as exc:
        _error_panel(f"Failed to import discovery module: {exc}")

    discovery = Discovery()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task(f"Building genre playlist for '{genre_name}'...", total=None)
        try:
            result = _run(discovery.build_genre_playlist(genre=genre_name, limit=limit))
        except Exception as exc:
            _error_panel(f"Failed to build genre playlist: {exc}")

    playlist = result.get("playlist", {})
    tracks = result.get("tracks", [])

    console.print(
        Panel(
            f"[green]Genre playlist created![/green]\n\n"
            f"  Name:   [bold]{playlist.get('name', genre_name)}[/bold]\n"
            f"  ID:     {playlist.get('id', 'N/A')}\n"
            f"  Tracks: {len(tracks)}",
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
            table.add_row(str(idx), track.get("name", "—"), track.get("artist", "—"))

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
) -> None:
    """Create a time-capsule playlist from your listening history."""
    try:
        from spotifyforge.core.discovery import Discovery
    except Exception as exc:
        _error_panel(f"Failed to import discovery module: {exc}")

    discovery = Discovery()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task("Building your time capsule...", total=None)
        try:
            result = _run(discovery.create_time_capsule(time_range=time_range.value))
        except Exception as exc:
            _error_panel(f"Failed to create time capsule: {exc}")

    playlist = result.get("playlist", {})
    track_count = result.get("track_count", 0)

    range_labels = {
        "short_term": "Last 4 Weeks",
        "medium_term": "Last 6 Months",
        "long_term": "All Time",
    }

    console.print(
        Panel(
            f"[green]Time capsule created![/green]\n\n"
            f"  Name:       [bold]{playlist.get('name', 'Time Capsule')}[/bold]\n"
            f"  ID:         {playlist.get('id', 'N/A')}\n"
            f"  Tracks:     {track_count}\n"
            f"  Time range: {range_labels.get(time_range.value, time_range.value)}\n"
            f"  Created:    {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
            title="Time Capsule",
            border_style="magenta",
            expand=False,
        )
    )


# ╔═════════════════════════════════════════════════════════════════════════╗
# ║  SCHEDULE                                                              ║
# ╚═════════════════════════════════════════════════════════════════════════╝
schedule_app = typer.Typer(
    name="schedule",
    help="Manage automated playlist scheduling jobs.",
    no_args_is_help=True,
)
app.add_typer(schedule_app)


@schedule_app.command("list")
def schedule_list() -> None:
    """Display all scheduled jobs in a table."""
    try:
        from spotifyforge.core.scheduler import Scheduler
    except Exception as exc:
        _error_panel(f"Failed to import scheduler module: {exc}")

    scheduler = Scheduler()

    try:
        jobs = _run(scheduler.list_jobs())
    except Exception as exc:
        _error_panel(f"Failed to list scheduled jobs: {exc}")

    if not jobs:
        console.print(
            Panel(
                "[yellow]No scheduled jobs.[/yellow]\n"
                "Use [bold]spotifyforge schedule add[/bold] to create one.",
                title="Scheduled Jobs",
                border_style="yellow",
                expand=False,
            )
        )
        return

    table = Table(
        title="Scheduled Jobs",
        box=box.ROUNDED,
        show_lines=True,
        header_style="bold magenta",
    )
    table.add_column("ID", style="bold cyan")
    table.add_column("Name", style="white")
    table.add_column("Type", style="green")
    table.add_column("Playlist", style="dim")
    table.add_column("Cron", style="yellow")
    table.add_column("Next Run", style="cyan")
    table.add_column("Status", justify="center")

    for job in jobs:
        status = job.get("status", "unknown")
        if status == "active":
            status_display = "[green]Active[/green]"
        elif status == "paused":
            status_display = "[yellow]Paused[/yellow]"
        else:
            status_display = f"[dim]{status}[/dim]"

        table.add_row(
            job.get("id", "—"),
            job.get("name", "—"),
            job.get("type", "—"),
            job.get("playlist_id", "—"),
            job.get("cron", "—"),
            job.get("next_run", "—"),
            status_display,
        )

    console.print(table)


@schedule_app.command("add")
def schedule_add(
    name: str = typer.Option(..., "--name", "-n", help="Human-friendly name for the job."),
    type: str = typer.Option(
        ...,
        "--type",
        "-t",
        help="Job type (e.g. 'sync', 'deduplicate', 'discover', 'time-capsule').",
    ),
    playlist: str = typer.Option(
        ..., "--playlist", "-p", help="Target Spotify playlist ID."
    ),
    cron: str = typer.Option(
        ...,
        "--cron",
        "-c",
        help="Cron expression for scheduling (e.g. '0 8 * * 1' for Mondays at 8 AM).",
    ),
) -> None:
    """Add a new scheduled job."""
    try:
        from spotifyforge.core.scheduler import Scheduler
    except Exception as exc:
        _error_panel(f"Failed to import scheduler module: {exc}")

    scheduler = Scheduler()

    try:
        result = _run(
            scheduler.add_job(name=name, job_type=type, playlist_id=playlist, cron=cron)
        )
    except Exception as exc:
        _error_panel(f"Failed to add scheduled job: {exc}")

    console.print(
        Panel(
            f"[green]Job scheduled successfully![/green]\n\n"
            f"  Job ID:   [bold]{result.get('id', 'N/A')}[/bold]\n"
            f"  Name:     {name}\n"
            f"  Type:     {type}\n"
            f"  Playlist: {playlist}\n"
            f"  Cron:     {cron}\n"
            f"  Next run: {result.get('next_run', 'N/A')}",
            title="Job Added",
            border_style="green",
            expand=False,
        )
    )


@schedule_app.command("remove")
def schedule_remove(
    job_id: str = typer.Argument(..., help="ID of the scheduled job to remove."),
) -> None:
    """Remove a scheduled job."""
    try:
        from spotifyforge.core.scheduler import Scheduler
    except Exception as exc:
        _error_panel(f"Failed to import scheduler module: {exc}")

    scheduler = Scheduler()

    try:
        _run(scheduler.remove_job(job_id))
    except Exception as exc:
        _error_panel(f"Failed to remove job: {exc}")

    console.print(f"[green]Job [bold]{job_id}[/bold] removed successfully.[/green]")


@schedule_app.command("run")
def schedule_run() -> None:
    """Start the scheduler daemon (foreground process)."""
    try:
        from spotifyforge.core.scheduler import Scheduler
    except Exception as exc:
        _error_panel(f"Failed to import scheduler module: {exc}")

    if not settings.scheduler_enabled:
        _error_panel(
            "Scheduler is disabled in configuration.\n"
            "Set SPOTIFYFORGE_SCHEDULER_ENABLED=true or update your config.",
            title="Scheduler Disabled",
        )

    scheduler = Scheduler()

    console.print(
        Panel(
            "[bold cyan]SpotifyForge Scheduler[/bold cyan]\n\n"
            "The scheduler daemon is running in the foreground.\n"
            "Press [bold]Ctrl+C[/bold] to stop.",
            border_style="cyan",
            expand=False,
        )
    )

    try:
        _run(scheduler.start())
    except KeyboardInterrupt:
        console.print("\n[yellow]Scheduler stopped by user.[/yellow]")
    except Exception as exc:
        _error_panel(f"Scheduler error: {exc}")


# ╔═════════════════════════════════════════════════════════════════════════╗
# ║  CONFIG                                                                ║
# ╚═════════════════════════════════════════════════════════════════════════╝
config_app = typer.Typer(
    name="config",
    help="View and modify SpotifyForge configuration.",
    no_args_is_help=True,
)
app.add_typer(config_app)


@config_app.command("show")
def config_show() -> None:
    """Display the current SpotifyForge configuration."""
    table = Table(
        title="SpotifyForge Configuration",
        box=box.ROUNDED,
        show_lines=True,
        header_style="bold magenta",
    )
    table.add_column("Key", style="bold cyan")
    table.add_column("Value", style="white")
    table.add_column("Source", style="dim")

    # Iterate over all fields in Settings and display their current values.
    # Mask secrets for safety.
    secret_fields = {"spotify_client_id", "spotify_client_secret"}

    for field_name, field_info in Settings.model_fields.items():
        value = getattr(settings, field_name)
        display_value = str(value)

        if field_name in secret_fields and value:
            # Mask all but the last 4 characters.
            display_value = "****" + display_value[-4:] if len(display_value) > 4 else "****"

        # Determine source — environment variable or default.
        env_key = f"SPOTIFYFORGE_{field_name.upper()}"
        import os
        source = "env" if os.environ.get(env_key) else "default"

        table.add_row(field_name, display_value, source)

    console.print(table)


@config_app.command("set")
def config_set(
    key: str = typer.Argument(..., help="Configuration key (e.g. 'spotify_client_id')."),
    value: str = typer.Argument(..., help="New value for the configuration key."),
) -> None:
    """Set a configuration value in the .env file.

    This writes or updates the key in the project-level ``.env`` file so
    the value persists across sessions.
    """
    valid_keys = set(Settings.model_fields.keys())
    if key not in valid_keys:
        _error_panel(
            f"Unknown configuration key: [bold]{key}[/bold]\n\n"
            f"Valid keys: {', '.join(sorted(valid_keys))}",
            title="Invalid Key",
        )

    env_key = f"SPOTIFYFORGE_{key.upper()}"
    env_path = Path(".env")

    # Read existing .env content.
    lines: list[str] = []
    found = False
    if env_path.exists():
        raw = env_path.read_text(encoding="utf-8")
        for line in raw.splitlines():
            stripped = line.strip()
            if stripped.startswith(f"{env_key}=") or stripped.startswith(f"{env_key} ="):
                lines.append(f"{env_key}={value}")
                found = True
            else:
                lines.append(line)

    if not found:
        lines.append(f"{env_key}={value}")

    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    console.print(
        f"[green]Configuration updated:[/green] "
        f"[bold]{key}[/bold] = [cyan]{value}[/cyan]  "
        f"(written to .env as {env_key})"
    )


# ---------------------------------------------------------------------------
# Module guard — allow ``python -m spotifyforge.cli.app``
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app()
