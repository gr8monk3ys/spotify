"""`spotifyforge library` commands."""

from __future__ import annotations

from pathlib import Path

import typer
from rich import box
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from spotifyforge.cli._shared import _error_panel, _run_spotify, console, err_console
from spotifyforge.config import settings

# ╔═════════════════════════════════════════════════════════════════════════╗
# ║  LIBRARY                                                               ║
# ╚═════════════════════════════════════════════════════════════════════════╝
library_app = typer.Typer(
    name="library",
    help="Bring what other platforms know into the Spotify library.",
    no_args_is_help=True,
)


@library_app.command("save")
def library_save(
    from_discogs: bool = typer.Option(
        False, "--from-discogs", help="Save every record in the Discogs collection."
    ),
    file: Path | None = typer.Option(
        None, "--file", help="The discogs.json to read (default: MUSIC_DIR/discogs.json)."
    ),
    apply: bool = typer.Option(
        False, "--apply", help="Actually save. Without it, only show what would be saved."
    ),
) -> None:
    """Save the records you own on Discogs to your Spotify library.

    Reads [bold]discogs.json[/bold] (written by [bold]discogs export[/bold]),
    resolves each record to a Spotify album by artist and title, and
    saves the ones not already in the library. Dry-run by default; a
    record that matches nothing, or more than one edition, is listed
    and never guessed.
    """
    from spotifyforge.core.library import (
        LibraryFileError,
        find_album,
        plan,
        read_discogs_collection,
        save_albums,
        saved_status,
    )

    if not from_discogs:
        _error_panel("Nothing to read from. Pass [bold]--from-discogs[/bold].")

    source = file or settings.music_dir / "discogs.json"
    try:
        records = read_discogs_collection(source)
    except LibraryFileError as exc:
        err_console.print(Panel(str(exc), title="No Discogs export", border_style="red"))
        raise typer.Exit(code=2) from exc

    async def _resolve(sp):
        matches = [await find_album(sp, r) for r in records]
        ids = [m.album_id for m in matches if m.album_id]
        status = await saved_status(sp, ids) if ids else {}
        return matches, status

    matches, status = _run_spotify(
        f"Matching {len(records)} record(s) on Spotify...", "Failed to match records", _resolve
    )
    groups = plan(matches, status)

    table = Table(title="Discogs collection → Spotify library", box=box.SIMPLE)
    table.add_column("Record")
    table.add_column("Spotify album")
    table.add_column("Action")
    for m in matches:
        label = f"{m.record.artist} — {m.record.title}"
        if m.album_id is None:
            table.add_row(label, "[dim]no match[/dim]", "[yellow]skip[/yellow]")
        elif status.get(m.album_id):
            table.add_row(label, m.album_name or m.album_id, "[dim]already saved[/dim]")
        else:
            table.add_row(label, m.album_name or m.album_id, "[green]would save[/green]")
    console.print(table)

    to_save = groups["to_save"]
    summary = (
        f"{len(to_save)} to save, {len(groups['already'])} already saved, "
        f"{len(groups['unmatched'])} without a match."
    )
    if not apply:
        console.print(summary)
        if to_save:
            console.print("Dry run — re-run with [bold]--apply[/bold] to save them.")
        return

    ids = [m.album_id for m in to_save if m.album_id]
    saved = _run_spotify(
        "Saving albums...", "Failed to save albums", lambda sp: save_albums(sp, ids)
    )
    console.print(
        Panel(
            f"[green]Saved {saved} album(s)[/green] to your library.\n{summary}",
            title="Library updated",
            border_style="green",
            expand=False,
        )
    )


@library_app.command("soundtracks")
def library_soundtracks(
    min_rating: float = typer.Option(
        4.5, "--min-rating", min=0.5, max=5.0, help="Only films rated at least this."
    ),
    per_film: int = typer.Option(2, "--per-film", min=1, max=10, help="Tracks taken per film."),
    file: Path | None = typer.Option(
        None, "--file", help="The letterboxd.json to read (default: MOVIES_DIR/letterboxd.json)."
    ),
    name: str = typer.Option(
        None, "--name", help="Playlist name (default: 'Soundtracks: films I rated N+')."
    ),
    apply: bool = typer.Option(
        False, "--apply", help="Write the playlist. Without it, only show the matches."
    ),
) -> None:
    """Build a playlist from the soundtracks of your highest-rated films.

    Reads [bold]letterboxd.json[/bold] and looks each favourite up on
    Spotify. Only an album named for the film, marked as its soundtrack
    or score, and released with it is accepted; a film without one is
    listed and never guessed. Dry-run by default. With
    [bold]--apply[/bold] the opening tracks of each soundtrack go into
    one private playlist, which a later run updates in place.
    """
    from spotifyforge.core.soundtracks import (
        FilmsFileError,
        build_playlist_tracks,
        find_soundtracks,
        read_favourite_films,
        write_playlist,
    )

    source = file or settings.movies_dir / "letterboxd.json"
    try:
        films = read_favourite_films(source, min_rating)
    except FilmsFileError as exc:
        err_console.print(Panel(str(exc), title="No Letterboxd export", border_style="red"))
        raise typer.Exit(code=2) from exc
    if not films:
        console.print(f"No films rated {min_rating:g} or higher in {source}.")
        return

    matches = _run_spotify(
        f"Looking up soundtracks for {len(films)} film(s)...",
        "Failed to look up soundtracks",
        lambda sp: find_soundtracks(sp, films),
    )
    matched = [m for m in matches if m.album_id]

    # Film and album titles are other people's text: escaped, never interpreted.
    table = Table(title="Favourite films → soundtracks", box=box.SIMPLE)
    table.add_column("Film")
    table.add_column("Rated", justify="right")
    table.add_column("Soundtrack")
    for m in matches:
        film = escape(f"{m.film.title} ({m.film.year})" if m.film.year else m.film.title)
        if m.album_id is None:
            table.add_row(film, f"{m.film.rating:g}", "[dim]no match[/dim]")
        else:
            table.add_row(film, f"{m.film.rating:g}", escape(f"{m.album_name} — {m.artist}"))
    console.print(table)

    summary = f"{len(matched)} of {len(films)} films have a soundtrack that could be matched."
    if not apply:
        console.print(summary)
        if matched:
            console.print("Dry run — re-run with [bold]--apply[/bold] to write the playlist.")
        return
    if not matched:
        console.print(summary)
        return

    title = name or f"Soundtracks: films I rated {min_rating:g}+"
    description = (
        f"The opening tracks from the soundtracks of {len(matched)} films "
        f"rated {min_rating:g} or higher on Letterboxd."
    )

    async def _write(sp):
        uris = await build_playlist_tracks(sp, matched, per_film)
        return await write_playlist(sp, title, description, uris), len(uris)

    created, count = _run_spotify("Writing the playlist...", "Failed to write the playlist", _write)
    console.print(
        Panel(
            f"[green]{'Created' if created else 'Updated'}[/green] "
            f"[bold]{escape(title)}[/bold] with {count} track(s).\n{summary}",
            title="Soundtracks",
            border_style="green",
            expand=False,
        )
    )
