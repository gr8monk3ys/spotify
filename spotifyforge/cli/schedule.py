"""`spotifyforge schedule` commands."""

from __future__ import annotations

import asyncio
from typing import Any

import typer
from rich import box
from rich.panel import Panel
from rich.table import Table

from spotifyforge.cli._shared import (
    _db_user_id,
    _error_panel,
    _run,
    _run_spotify,
    console,
)
from spotifyforge.cli.discover import TimeRange
from spotifyforge.config import settings

# ╔═════════════════════════════════════════════════════════════════════════╗
# ║  SCHEDULE                                                              ║
# ╚═════════════════════════════════════════════════════════════════════════╝
schedule_app = typer.Typer(
    name="schedule",
    help="Manage automated playlist scheduling jobs.",
    no_args_is_help=True,
)


@schedule_app.command("list")
def schedule_list() -> None:
    """Display your scheduled jobs in a table."""
    from sqlmodel import Session, select

    from spotifyforge.db.engine import get_engine
    from spotifyforge.models.models import Playlist, ScheduledJob

    user_id = _db_user_id()
    with Session(get_engine()) as session:
        jobs = list(session.exec(select(ScheduledJob).where(ScheduledJob.user_id == user_id)).all())
        playlist_names = {
            p.id: p.name
            for p in session.exec(select(Playlist).where(Playlist.owner_id == user_id)).all()
        }

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
    table.add_column("ID", style="bold cyan", justify="right")
    table.add_column("Name", style="white")
    table.add_column("Type", style="green")
    table.add_column("Playlist", style="dim")
    table.add_column("Cron", style="yellow")
    table.add_column("Last Run", style="cyan")
    table.add_column("Status", justify="center")

    for job in jobs:
        status = "[green]Enabled[/green]" if job.enabled else "[yellow]Disabled[/yellow]"
        if job.failure_count:
            status = f"[red]Failing ({job.failure_count})[/red]"
        table.add_row(
            str(job.id),
            job.name,
            str(job.job_type),
            playlist_names.get(job.playlist_id, "—") if job.playlist_id else "—",
            job.cron_expression,
            job.last_run_at.strftime("%Y-%m-%d %H:%M") if job.last_run_at else "never",
            status,
        )

    console.print(table)


@schedule_app.command("add")
def schedule_add(
    name: str = typer.Option(..., "--name", "-n", help="Human-friendly name for the job."),
    job_type: str = typer.Option(
        ...,
        "--type",
        "-t",
        help="Job type: sync, archive, deduplicate, genre_refresh, or time_capsule.",
    ),
    cron: str = typer.Option(
        ...,
        "--cron",
        "-c",
        help="Cron expression (5 fields, e.g. '0 8 * * 1' for Mondays at 8 AM).",
    ),
    playlist: str | None = typer.Option(
        None,
        "--playlist",
        "-p",
        help="Target Spotify playlist ID (required for all types except time_capsule).",
    ),
    genre: str | None = typer.Option(
        None, "--genre", "-g", help="Genre seed (required for genre_refresh jobs)."
    ),
    source_playlist: str | None = typer.Option(
        None,
        "--source-playlist",
        help="Source Spotify playlist ID (required for archive jobs).",
    ),
    time_range: TimeRange = typer.Option(
        TimeRange.short_term,
        "--time-range",
        help="Time range for time_capsule jobs.",
        case_sensitive=False,
    ),
) -> None:
    """Add a new scheduled job (stored in the database; run by the server or 'schedule run')."""
    from sqlmodel import Session, select

    from spotifyforge.core.playlist_manager import PlaylistManager
    from spotifyforge.core.scheduler import validate_cron
    from spotifyforge.db.engine import get_engine
    from spotifyforge.models.models import JobType, Playlist, ScheduledJob

    try:
        jt = JobType(job_type)
    except ValueError:
        _error_panel(
            f"Unknown job type [bold]{job_type}[/bold].\n"
            f"Valid types: {', '.join(t.value for t in JobType)}",
            title="Invalid Job Type",
        )

    if validate_cron(cron) is None:
        _error_panel(
            f"Invalid cron expression: [bold]{cron}[/bold]\n"
            "Expected 5 fields: 'minute hour day month day_of_week'.",
            title="Invalid Cron Expression",
        )

    needs_playlist = jt is not JobType.time_capsule
    if needs_playlist and not playlist:
        _error_panel(f"Job type '{jt.value}' requires --playlist.")
    if jt is JobType.genre_refresh and not genre:
        _error_panel("genre_refresh jobs require --genre.")
    if jt is JobType.archive and not source_playlist:
        _error_panel("archive jobs require --source-playlist.")

    user_id = _db_user_id()

    # Resolve (or auto-sync) the local playlist row for the FK.
    playlist_pk: int | None = None
    if playlist:
        with Session(get_engine()) as session:
            row = session.exec(
                select(Playlist).where(
                    Playlist.spotify_id == playlist, Playlist.owner_id == user_id
                )
            ).first()
        if row is None:
            console.print(f"[dim]Playlist {playlist} not in local cache — syncing it first.[/dim]")
            row = _run_spotify(
                f"Syncing playlist {playlist}...",
                f"Could not sync playlist {playlist}",
                lambda sp: PlaylistManager(sp).sync_playlist(playlist, owner_id=user_id),
            )
        playlist_pk = row.id

    config: dict[str, Any] = {}
    if genre:
        config["genre"] = genre
    if source_playlist:
        config["source_playlist_id"] = source_playlist
    if jt is JobType.time_capsule:
        config["time_range"] = time_range.value

    with Session(get_engine()) as session:
        job = ScheduledJob(
            user_id=user_id,
            name=name,
            job_type=jt,
            playlist_id=playlist_pk,
            config=config or None,
            cron_expression=cron,
            enabled=True,
        )
        session.add(job)
        session.commit()
        session.refresh(job)

    console.print(
        Panel(
            f"[green]Job scheduled successfully![/green]\n\n"
            f"  Job ID:   [bold]{job.id}[/bold]\n"
            f"  Name:     {name}\n"
            f"  Type:     {jt.value}\n"
            f"  Playlist: {playlist or '—'}\n"
            f"  Cron:     {cron}\n\n"
            "It runs whenever the API server or [bold]spotifyforge schedule run[/bold] is up.",
            title="Job Added",
            border_style="green",
            expand=False,
        )
    )


@schedule_app.command("remove")
def schedule_remove(
    job_id: int = typer.Argument(..., help="ID of the scheduled job to remove."),
) -> None:
    """Remove a scheduled job."""
    from sqlmodel import Session, select

    from spotifyforge.db.engine import get_engine
    from spotifyforge.models.models import ScheduledJob

    user_id = _db_user_id()
    with Session(get_engine()) as session:
        job = session.exec(
            select(ScheduledJob).where(ScheduledJob.id == job_id, ScheduledJob.user_id == user_id)
        ).first()
        if job is None:
            _error_panel(f"Scheduled job {job_id} not found.")
        session.delete(job)
        session.commit()

    console.print(f"[green]Job [bold]{job_id}[/bold] removed successfully.[/green]")


@schedule_app.command("run")
def schedule_run() -> None:
    """Start the scheduler daemon (foreground process)."""
    from spotifyforge.core.scheduler import get_scheduler_service
    from spotifyforge.db.engine import init_db

    if not settings.scheduler_enabled:
        _error_panel(
            "Scheduler is disabled in configuration.\n"
            "Set SPOTIFYFORGE_SCHEDULER_ENABLED=true or update your config.",
            title="Scheduler Disabled",
        )

    init_db()

    console.print(
        Panel(
            "[bold cyan]SpotifyForge Scheduler[/bold cyan]\n\n"
            "The scheduler daemon is running in the foreground.\n"
            "Press [bold]Ctrl+C[/bold] to stop.",
            border_style="cyan",
            expand=False,
        )
    )

    async def _daemon():
        service = get_scheduler_service()
        service.start()
        count = await service.load_jobs_from_db()
        console.print(f"[dim]Loaded {count} enabled job(s).[/dim]")
        try:
            await asyncio.Event().wait()  # run until interrupted
        finally:
            service.stop(wait=False)

    try:
        _run(_daemon())
    except KeyboardInterrupt:
        console.print("\n[yellow]Scheduler stopped by user.[/yellow]")
