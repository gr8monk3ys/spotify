"""`spotifyforge export` commands."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.panel import Panel

from spotifyforge.cli._shared import _current_spotify_user_id, _run_spotify, console
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
        None,
        "--out",
        "-o",
        help="Where to write (default: music-library.json in MUSIC_DIR, ~/.music).",
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
    from spotifyforge.core.audio_features import load_cached_features
    from spotifyforge.core.clock import utc_now
    from spotifyforge.core.curation import CurationEngine
    from spotifyforge.core.expansion import load_expansions
    from spotifyforge.core.export import build_library_export, write_export

    # Known locally — asking Spotify who we are just to stamp provenance
    # would be a round trip for a string already on disk.
    user_id = _current_spotify_user_id()

    async def _export(sp):
        engine = CurationEngine(sp)
        tracks = await engine.enrich_genres(await engine.fetch_liked(max_tracks))
        return build_library_export(
            tracks,
            load_cached_features(),
            load_expansions(),
            user_id,
            utc_now().isoformat(),
        )

    document = _run_spotify("Reading your library...", "Failed to export library", _export)
    target = write_export(document, out)

    albums = document["albums"]
    complete = sum(1 for a in albums if (a["affinity"] or 0) >= 0.8)
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


@export_app.command("page")
def export_page(
    out: Path = typer.Option(Path("taste.html"), "--out", "-o", help="Where to write the page."),
    name: str | None = typer.Option(
        None, "--name", help="Whose library it is (default: your Spotify display name)."
    ),
    max_tracks: int | None = _MAX_TRACKS,
) -> None:
    """Write your library as one self-contained HTML page.

    A timetable of your genres against the decades the songs came out,
    plus the artists and albums you have liked most of. It is a summary:
    counts only, no track list and no play history. The file fetches
    nothing when opened, so it works from disk, by email, or on any
    host. This command only writes the file; it publishes nothing.
    """
    from spotifyforge.core.clock import utc_now
    from spotifyforge.core.curation import CurationEngine, dedupe_versions
    from spotifyforge.core.taste_page import build_page_data, render_page

    async def _read(sp):
        engine = CurationEngine(sp)
        tracks = dedupe_versions(await engine.enrich_genres(await engine.fetch_liked(max_tracks)))
        me = await sp.current_user()
        return tracks, me.display_name or me.id

    tracks, display_name = _run_spotify(
        "Reading your liked songs...", "Failed to read library", _read
    )
    data = build_page_data(tracks, name or display_name, utc_now().date())
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_page(data), encoding="utf-8", newline="\n")

    console.print(
        Panel(
            f"[green]Wrote the page[/green] for {data.songs} songs across "
            f"{data.genres} genres ({len(data.lines)} shown).\n"
            "Nothing was published; the file is yours to open or host.",
            title="Taste page",
            border_style="green",
            expand=False,
        )
    )
    console.print(f"[dim]{out.resolve()}[/dim]")


@export_app.command("wrapped")
def export_wrapped(
    out: Path = typer.Option(Path("wrapped.html"), "--out", "-o", help="Where to write the page."),
    year: int | None = typer.Option(
        None, "--year", "-y", min=2000, help="Only plays in this calendar year."
    ),
    name: str | None = typer.Option(
        None, "--name", help="Whose listening it is (default: your Spotify display name)."
    ),
) -> None:
    """Write your own Wrapped from the local listening log, as one HTML page.

    Top songs, artists and albums, the hours and weekdays you listen,
    where plays start, and songs played hard then dropped, all counted
    from the log [bold]history sync[/bold] keeps. The page says when the
    log starts and flags any gap a sync recorded. It holds your play
    history, so it is written to a local file only and published nowhere.
    Spotify is asked only for your name and which playlists are yours.
    """
    from spotifyforge.core.clock import utc_now
    from spotifyforge.core.history import load_gaps, load_plays
    from spotifyforge.core.playlist_manager import PlaylistManager
    from spotifyforge.core.wrapped import build_wrapped, render_wrapped

    plays = load_plays()

    async def _read(sp):
        owned = {p["id"]: p["name"] for p in await PlaylistManager(sp).get_owned_playlists()}
        me = await sp.current_user()
        return owned, me.display_name or me.id

    owned, display_name = _run_spotify(
        "Reading your profile and playlists...", "Failed to read your playlists", _read
    )
    data = build_wrapped(
        plays, name or display_name, utc_now().date(), owned, load_gaps(), year=year
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_wrapped(data), encoding="utf-8", newline="\n")

    since = f"{data.first:%Y-%m-%d}" if data.first else "—"
    console.print(
        Panel(
            f"[green]Wrote the page[/green] from {data.plays} play(s) logged since {since} "
            f"({data.days} day(s), {len(data.gaps)} recorded gap(s)).\n"
            "It holds your play history: nothing was published, keep the file local.",
            title="Wrapped",
            border_style="green",
            expand=False,
        )
    )
    console.print(f"[dim]{out.resolve()}[/dim]")
