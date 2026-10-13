"""`spotifyforge history` commands."""

from __future__ import annotations

from datetime import timedelta

import typer
from rich import box
from rich.panel import Panel
from rich.table import Table

from spotifyforge.cli._shared import _run_spotify, console

# ╔═════════════════════════════════════════════════════════════════════════╗
# ║  HISTORY                                                               ║
# ╚═════════════════════════════════════════════════════════════════════════╝
history_app = typer.Typer(
    name="history",
    help="Keep a listening history Spotify itself does not keep for you.",
    no_args_is_help=True,
)


@history_app.command("sync")
def history_sync() -> None:
    """Append your recent plays to the local listening history.

    Spotify only remembers the last 50 plays, so run this about hourly
    (cron or Task Scheduler). It is safe to run as often as you like:
    plays already recorded are skipped.
    """
    from spotifyforge.core.history import sync_history

    result = _run_spotify(
        "Reading recent plays...",
        "Failed to sync listening history",
        lambda sp: sync_history(sp),
    )

    body = (
        f"Added [bold]{result.added}[/bold] play(s); "
        f"the history now holds [bold]{result.total}[/bold]."
    )
    if result.possible_gap:
        body += (
            "\n[yellow]All 50 plays Spotify remembered were new, so some older "
            "ones may have been missed.[/yellow] Run this more often."
        )
    console.print(Panel(body, title="Listening history", border_style="green", expand=False))
    console.print(f"[dim]{result.path}[/dim]")


@history_app.command("show")
def history_show(
    days: int = typer.Option(30, "--days", "-d", min=1, help="How far back to look."),
    top: int = typer.Option(10, "--top", "-t", min=1, help="How many tracks and artists to list."),
) -> None:
    """Summarise the local listening history (no Spotify request)."""
    from spotifyforge.core.clock import utc_now
    from spotifyforge.core.history import history_path, load_plays, summarise

    plays = load_plays(since=utc_now() - timedelta(days=days))
    if not plays:
        console.print(
            Panel(
                f"No plays recorded in the last {days} day(s).\n"
                "Run [bold]spotifyforge history sync[/bold] to start the log.",
                title="Listening history",
                border_style="yellow",
                expand=False,
            )
        )
        return

    summary = summarise(plays, top=top)
    console.print(
        Panel(
            f"Plays:            [bold]{summary.plays}[/bold]\n"
            f"Listening time:   [bold]{summary.minutes // 60}h {summary.minutes % 60}m[/bold]\n"
            f"Distinct tracks:  [bold]{summary.distinct_tracks}[/bold]\n"
            f"From:             {summary.first}\n"
            f"To:               {summary.last}",
            title=f"Last {days} day(s)",
            border_style="cyan",
            expand=False,
        )
    )

    tracks = Table(box=box.SIMPLE, header_style="bold cyan", title="Top tracks")
    tracks.add_column("Plays", justify="right")
    tracks.add_column("Track", no_wrap=True, max_width=45)
    tracks.add_column("Artist", style="green", no_wrap=True, max_width=30)
    for name, artists, count in summary.top_tracks:
        tracks.add_row(str(count), name, artists)
    console.print(tracks)

    artists_table = Table(box=box.SIMPLE, header_style="bold cyan", title="Top artists")
    artists_table.add_column("Plays", justify="right")
    artists_table.add_column("Artist", style="green")
    for artist, count in summary.top_artists:
        artists_table.add_row(str(count), artist)
    console.print(artists_table)
    console.print(f"[dim]{history_path()}[/dim]")
