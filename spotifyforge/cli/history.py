"""`spotifyforge history` commands."""

from __future__ import annotations

from datetime import timedelta

import typer
from rich import box
from rich.markup import escape
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


@history_app.command("playlists")
def history_playlists(
    days: int | None = typer.Option(
        None, "--days", "-d", min=1, help="Only the last N days (default: the whole log)."
    ),
) -> None:
    """Which of your own playlists you actually play.

    Counts the plays in the local log by where they were started from
    and lists your playlists that were played, and how many never were.
    It changes nothing. A play counts for a playlist only when it was
    started from that playlist.
    """
    from spotifyforge.core.clock import utc_now
    from spotifyforge.core.history import MIN_DAYS_TO_JUDGE, load_plays, playlist_usage
    from spotifyforge.core.playlist_manager import PlaylistManager

    plays = load_plays(since=utc_now() - timedelta(days=days) if days else None)
    if not plays:
        console.print(
            Panel(
                "No plays recorded yet.\n"
                "Run [bold]spotifyforge history sync[/bold] on a schedule to start the log.",
                title="Playlist usage",
                border_style="yellow",
                expand=False,
            )
        )
        return

    async def _owned(sp):
        return {p["id"]: p["name"] for p in await PlaylistManager(sp).get_owned_playlists()}

    owned = _run_spotify("Reading your playlists...", "Failed to read playlists", _owned)
    usage = playlist_usage(plays, owned)

    console.print(
        Panel(
            f"Plays logged:            [bold]{usage.plays}[/bold] over "
            f"[bold]{usage.days_covered}[/bold] day(s)\n"
            f"From your own playlists: [bold]{usage.from_own_playlists}[/bold]\n"
            f"From other playlists:    [bold]{usage.from_other_playlists}[/bold] "
            "(followed, or made by Spotify)\n"
            f"From albums:             [bold]{usage.from_albums}[/bold]\n"
            f"From artist pages:       [bold]{usage.from_artists}[/bold]\n"
            f"Elsewhere:               [bold]{usage.from_elsewhere}[/bold] "
            "(liked songs, search, queue)\n"
            f"Your playlists played:   [bold]{len(usage.played)}[/bold] of {len(owned)}",
            title="Playlist usage",
            border_style="cyan",
            expand=False,
        )
    )
    if usage.played:
        table = Table(box=box.SIMPLE, header_style="bold cyan", title="Your playlists, by plays")
        table.add_column("Plays", justify="right")
        table.add_column("Playlist")
        for _pid, name, count in usage.played:
            table.add_row(str(count), escape(name))
        console.print(table)
    if not usage.long_enough_to_judge:
        console.print(
            f"[yellow]The log covers {usage.days_covered} day(s).[/yellow] Under "
            f"{MIN_DAYS_TO_JUDGE}, an unplayed playlist mostly means the log is new, "
            "not that the playlist is unused."
        )


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
    # Titles and artist names are other people's text: escaped, so one
    # containing [link=...] or [red] is printed, not interpreted.
    for name, artists, count in summary.top_tracks:
        tracks.add_row(str(count), escape(name), escape(artists))
    console.print(tracks)

    artists_table = Table(box=box.SIMPLE, header_style="bold cyan", title="Top artists")
    artists_table.add_column("Plays", justify="right")
    artists_table.add_column("Artist", style="green")
    for artist, count in summary.top_artists:
        artists_table.add_row(str(count), escape(artist))
    console.print(artists_table)
    console.print(f"[dim]{history_path()}[/dim]")


def _ranks(top_ranks: tuple[tuple[str, int], ...]) -> str:
    short = {"short_term": "4w", "medium_term": "6m", "long_term": "1y"}
    return " · ".join(f"{short[r]} #{rank}" for r, rank in top_ranks) or "—"


@history_app.command("likes")
def history_likes(
    min_plays: int = typer.Option(
        3, "--min-plays", min=1, help="Plays in the log that make a song a candidate to like."
    ),
    show: int = typer.Option(20, "--show", "-n", min=0, help="Rows to print from each list."),
    playlist: bool = typer.Option(
        False,
        "--playlist",
        help="Also write the liked-but-not-heard list to a new private playlist.",
    ),
    limit: int = typer.Option(
        100,
        "--limit",
        min=1,
        help="Most songs --playlist writes, oldest likes first.",
    ),
) -> None:
    """Songs you play but have not liked, and liked songs you no longer hear.

    Compares your liked songs with the local log and Spotify's three top
    lists (about 4 weeks, 6 months and a year), read to their full depth.
    "Not heard lately" means in none of those: it is not proof a song was
    never played. Changes nothing unless you pass [bold]--playlist[/bold],
    which creates one new private playlist of the first [bold]--limit[/bold]
    songs, oldest likes first. Dates are in this machine's time zone.
    """
    from spotifyforge.core.clock import utc_now
    from spotifyforge.core.history import load_plays
    from spotifyforge.core.liked_played import (
        RANGE_SPANS,
        TOP_HEAD,
        liked_vs_played,
        playlist_name,
        write_playlist,
    )

    plays = load_plays()
    report = _run_spotify(
        "Reading your liked songs and top lists...",
        "Failed to compare liked and played",
        lambda sp: liked_vs_played(sp, plays, min_plays),
    )

    if report.log_since and report.log_until:
        log = f"[bold]{report.log_plays}[/bold] plays, {report.log_since} to {report.log_until}"
    else:
        log = "[yellow]empty[/yellow] (run [bold]history sync[/bold] on a timer)"
    tops = " · ".join(f"{n:,} ({RANGE_SPANS[r]})" for r, n in report.top_lists)
    console.print(
        Panel(
            f"Liked songs:        [bold]{report.liked:,}[/bold]\n"
            f"Log:                {log}\n"
            f"Top lists read:     {tops}\n"
            f"Played, not liked:  [bold]{len(report.played_not_liked)}[/bold] "
            f"({report.min_plays}+ plays in the log, or a top {TOP_HEAD} rank)\n"
            f"Liked, not heard:   [bold]{len(report.liked_not_heard):,}[/bold] "
            f"({report.not_heard_wording})",
            title="Liked vs played",
            border_style="cyan",
            expand=False,
        )
    )

    # Titles and artist names are other people's text: escaped throughout.
    if report.played_not_liked and show:
        table = Table(box=box.SIMPLE, header_style="bold cyan", title="Played, not liked")
        table.add_column("Plays", justify="right")
        table.add_column("Top rank")
        table.add_column("Track", max_width=45)
        table.add_column("Artist", style="green", max_width=30)
        for row in report.played_not_liked[:show]:
            table.add_row(
                str(row.plays), _ranks(row.top_ranks), escape(row.name), escape(row.artists)
            )
        console.print(table)

    if report.liked_not_heard and show:
        table = Table(
            box=box.SIMPLE,
            header_style="bold cyan",
            title=f"Liked, {report.not_heard_wording} (liked longest ago first)",
        )
        table.add_column("Track", max_width=45)
        table.add_column("Artist", style="green", max_width=30)
        table.add_column("Album", style="dim", max_width=30)
        for item in report.liked_not_heard[:show]:
            table.add_row(escape(item.name), escape(item.artists), escape(item.album))
        console.print(table)
        if len(report.liked_not_heard) > show:
            console.print(f"[dim]…and {len(report.liked_not_heard) - show:,} more.[/dim]")

    if not playlist:
        return
    if not report.liked_not_heard:
        console.print(
            "[yellow]Every liked song is in a top list or the log; no playlist made.[/yellow]"
        )
        return
    today = utc_now().astimezone().date()  # the same clock as the dates above
    size = report.playlist_size(limit)
    created = _run_spotify(
        f"Creating '{playlist_name(today)}'...",
        "Failed to create the playlist",
        lambda sp: write_playlist(sp, report, today, limit),
    )
    capped = (
        f"[dim]The oldest {size:,} of {len(report.liked_not_heard):,} likes; "
        "raise --limit for more.[/dim]\n"
        if size < len(report.liked_not_heard)
        else ""
    )
    console.print(
        Panel(
            f"[green]Created[/green] [bold]{escape(created.name)}[/bold] (private) with "
            f"{size:,} song(s).\n{capped}"
            f"https://open.spotify.com/playlist/{created.spotify_id}",
            title="Playlist",
            border_style="green",
            expand=False,
        )
    )
