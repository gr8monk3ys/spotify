"""`spotifyforge playlist` commands."""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

import typer
from rich import box
from rich.panel import Panel
from rich.table import Table

from spotifyforge.cli._shared import (
    _db_user_id,
    _error_panel,
    _run_spotify,
    console,
)

# ╔═════════════════════════════════════════════════════════════════════════╗
# ║  PLAYLIST                                                              ║
# ╚═════════════════════════════════════════════════════════════════════════╝
playlist_app = typer.Typer(
    name="playlist",
    help="Manage and curate your Spotify playlists.",
    no_args_is_help=True,
)


@playlist_app.command("list")
def playlist_list() -> None:
    """Show all your playlists in a table."""
    from spotifyforge.core.playlist_manager import PlaylistManager

    playlists = _run_spotify(
        "Fetching playlists...",
        "Failed to fetch playlists",
        lambda sp: PlaylistManager(sp).get_user_playlists(),
    )

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
    table.add_column("ID", style="dim")

    for idx, pl in enumerate(playlists, start=1):
        visibility = "[green]Public[/green]" if pl.get("public") else "[yellow]Private[/yellow]"
        table.add_row(
            str(idx),
            pl.get("name", "—"),
            str(pl.get("track_count", 0)),
            visibility,
            pl.get("id", "—"),
        )

    console.print(table)


@playlist_app.command("show")
def playlist_show(
    playlist_id: str = typer.Argument(..., help="Spotify playlist ID to inspect."),
) -> None:
    """Display playlist details and its tracks."""
    from spotifyforge.core.playlist_manager import PlaylistManager

    details = _run_spotify(
        "Loading playlist details...",
        "Failed to fetch playlist",
        lambda sp: PlaylistManager(sp).get_playlist_details(playlist_id),
    )

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
    from spotifyforge.core.playlist_manager import PlaylistManager

    owner_id = _db_user_id()

    playlist = _run_spotify(
        "Creating playlist...",
        "Failed to create playlist",
        lambda sp: PlaylistManager(sp).create_playlist(
            name=name, owner_id=owner_id, description=description, public=public
        ),
    )

    console.print(
        Panel(
            f"[green]Playlist created![/green]\n\n"
            f"  Name:        [bold]{playlist.name}[/bold]\n"
            f"  Spotify ID:  {playlist.spotify_id}\n"
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
    from spotifyforge.core.playlist_manager import PlaylistManager

    owner_id = _db_user_id()
    playlist = _run_spotify(
        "Syncing playlist...",
        "Sync failed",
        lambda sp: PlaylistManager(sp).sync_playlist(playlist_id, owner_id=owner_id),
    )

    console.print(
        Panel(
            f"[green]Playlist synced to local cache.[/green]\n\n"
            f"  Playlist: [bold]{playlist.name}[/bold]\n"
            f"  Tracks synced: {playlist.track_count}\n"
            f"  Last synced: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}",
            title="Sync Complete",
            border_style="green",
            expand=False,
        )
    )


@playlist_app.command("deduplicate")
def playlist_deduplicate(
    playlist_id: str = typer.Argument(..., help="Spotify playlist ID to deduplicate."),
) -> None:
    """Find and remove duplicate tracks from a playlist (keeps one copy of each)."""
    from spotifyforge.core.playlist_manager import PlaylistManager

    removed = _run_spotify(
        "Scanning for duplicates...",
        "Deduplication failed",
        lambda sp: PlaylistManager(sp).deduplicate(playlist_id),
    )

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
                f"  Removed [bold]{removed}[/bold] duplicate occurrence(s).",
                title="Deduplication",
                border_style="green",
                expand=False,
            )
        )


class ExportFormat(StrEnum):
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
    output: Path | None = typer.Option(  # noqa: UP007
        None,
        "--output",
        "-o",
        help="Output file path. Defaults to stdout.",
    ),
) -> None:
    """Export playlist tracks to CSV or JSON."""
    from spotifyforge.core.playlist_manager import PlaylistManager

    details = _run_spotify(
        "Fetching playlist for export...",
        "Export failed",
        lambda sp: PlaylistManager(sp).get_playlist_details(playlist_id),
    )

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
        console.print(f"[green]Exported {len(tracks)} tracks to[/green] [bold]{output}[/bold]")
    else:
        console.print(export_data)
