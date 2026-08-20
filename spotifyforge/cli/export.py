"""`spotifyforge export` commands."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.panel import Panel

from spotifyforge.cli._shared import _run_spotify, console
from spotifyforge.cli.curate import _MAX_TRACKS

# ╔═════════════════════════════════════════════════════════════════════════╗
# ║  EXPORT                                                                ║
# ╚═════════════════════════════════════════════════════════════════════════╝
export_app = typer.Typer(
    name="export",
    help="Hand this library's knowledge to other music platforms.",
    no_args_is_help=True,
)


@export_app.command("library")
def export_library(
    out: Path | None = typer.Option(
        None, "--out", "-o", help="Where to write (default: ~/.spotifyforge/music-library.json)."
    ),
    max_tracks: int | None = _MAX_TRACKS,
) -> None:
    """Write the liked library as a file other platform repos can read.

    Rolls your liked songs up to album level — the unit Discogs and
    RateYourMusic are keyed by — with ISRCs, genres, and how much of
    each album you actually like. Unheard tracks pinned by
    [bold]curate expand[/bold] are kept in a separate section so no
    consumer mistakes them for music you have heard.
    """
    from datetime import UTC, datetime

    from spotifyforge.core.audio_features import load_cached_features
    from spotifyforge.core.curation import CurationEngine
    from spotifyforge.core.expansion import load_expansions
    from spotifyforge.core.export import build_library_export, write_export

    async def _export(sp):
        engine = CurationEngine(sp)
        tracks = await engine.enrich_genres(await engine.fetch_liked(max_tracks))
        me = await sp.current_user()
        return build_library_export(
            tracks,
            load_cached_features(),
            load_expansions(),
            me.id,
            datetime.now(UTC).isoformat(),
        )

    document = _run_spotify("Reading your library...", "Failed to export library", _export)
    target = write_export(document, out)

    albums = document["albums"]
    with_affinity = [a for a in albums if a["affinity"] is not None]
    complete = sum(1 for a in with_affinity if a["affinity"] >= 0.8)
    console.print(
        Panel(
            f"[green]{len(albums)} album(s)[/green] from "
            f"{sum(a['liked_track_count'] for a in albums)} liked track(s).\n"
            f"{complete} album(s) at least 80% liked — the records you actually own in spirit.\n"
            f"{len(document['discoveries'])} discovered niche(s) kept separate (unheard).",
            title="Library exported",
            border_style="green",
            expand=False,
        )
    )
    console.print(f"[dim]{target}[/dim]")
