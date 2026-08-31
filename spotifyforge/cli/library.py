"""`spotifyforge library` commands."""

from __future__ import annotations

from pathlib import Path

import typer
from rich import box
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
