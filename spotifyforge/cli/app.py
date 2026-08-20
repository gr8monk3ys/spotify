"""SpotifyForge CLI — the main user-facing interface.

Built with Typer + Rich. Every sub-command group is its own ``typer.Typer``
instance, added to the root ``app`` via ``app.add_typer()``.

Command bodies are thin async wrappers over the same core services the web
API uses (:class:`PlaylistManager`, :class:`DiscoveryEngine`,
:class:`SchedulerService`), authenticated through the OS keyring.

Entry-point (registered in ``pyproject.toml``):
    spotifyforge = "spotifyforge.cli.app:app"
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import webbrowser
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, NoReturn

import tekore
import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
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


def _error_panel(message: str, *, title: str = "Error") -> NoReturn:
    """Display a Rich error panel on *stderr* and exit with code 1."""
    err_console.print(Panel(message, title=title, border_style="red", expand=False))
    raise typer.Exit(code=1)


def _run(coro):
    """Convenience wrapper around ``asyncio.run`` for async core methods."""
    return asyncio.run(coro)


def _version_callback(value: bool) -> None:
    """Print the version string and exit."""
    if value:
        console.print(f"[bold]SpotifyForge[/bold] version [cyan]{spotifyforge.__version__}[/cyan]")
        raise typer.Exit()


def _current_user_file() -> Path:
    return settings.db_path.parent / "current_user"


def _current_spotify_user_id() -> str:
    """Return the Spotify user id of the logged-in CLI user, or exit."""
    path = _current_user_file()
    if not path.exists():
        _error_panel(
            "Not logged in. Run [bold]spotifyforge auth login[/bold] first.",
            title="Authentication Required",
        )
    return path.read_text(encoding="utf-8").strip()


def _make_auth():
    """Build a keyring-backed :class:`SpotifyAuth`, or exit with guidance."""
    from spotifyforge.auth.oauth import AuthenticationError, KeyringTokenStore, SpotifyAuth

    try:
        return SpotifyAuth(token_store=KeyringTokenStore())
    except AuthenticationError as exc:
        _error_panel(str(exc), title="Configuration Error")


def _load_token(auth: Any, spotify_user_id: str) -> tekore.Token:
    """Load (and refresh if expiring) the stored token for a user.

    A refreshed token is persisted to both the keyring and the local DB
    row — scheduled jobs authenticate from the DB, so it must not go stale.
    """
    token = auth.token_store.load_token(spotify_user_id)
    if token.is_expiring:
        if not token.refresh_token:
            raise RuntimeError("Stored token expired with no refresh token; log in again.")
        token = auth.credentials.refresh_user_token(token.refresh_token)
        auth.token_store.save_token(spotify_user_id, token)
        _persist_tokens_to_db(spotify_user_id, token)
    return token


def _persist_tokens_to_db(spotify_user_id: str, token: tekore.Token) -> None:
    """Best-effort sync of refreshed tokens onto the local User row."""
    from sqlmodel import Session, select

    from spotifyforge.core.clients import apply_user_tokens
    from spotifyforge.db.engine import get_engine, init_db
    from spotifyforge.models.models import User

    init_db()
    with Session(get_engine()) as session:
        user = session.exec(select(User).where(User.spotify_id == spotify_user_id)).first()
        if user is not None:
            apply_user_tokens(user, token.access_token, token.refresh_token, token.expires_at)
            session.add(user)
            session.commit()


def _spotify_client() -> tekore.Spotify:
    """Return an authenticated async Spotify client for the CLI user."""
    from spotifyforge.core.clients import build_spotify

    auth = _make_auth()
    spotify_user_id = _current_spotify_user_id()
    try:
        token = _load_token(auth, spotify_user_id)
    except Exception as exc:
        _error_panel(
            f"Could not load stored credentials: {exc}\n"
            "Run [bold]spotifyforge auth login[/bold] to re-authenticate.",
            title="Authentication Error",
        )
    return build_spotify(token.access_token)


def _run_spotify(status_msg: str, error_msg: str, coro_fn):
    """Run an async Spotify operation with the standard CLI scaffolding.

    Builds the authenticated client, shows a status spinner, always closes
    the client, and converts any failure into an error panel (exit 1).
    ``coro_fn`` receives the client and returns the result.
    """
    sp = _spotify_client()

    async def _impl():
        try:
            return await coro_fn(sp)
        finally:
            await sp.close()

    with console.status(status_msg):
        try:
            return _run(_impl())
        except Exception as exc:
            # Some exceptions (httpx.ReadTimeout among them) stringify to
            # "", which produced an error panel that named no error.
            detail = str(exc) or type(exc).__name__
            _error_panel(f"{error_msg}: {detail}")


def _db_user_id() -> int:
    """Return the local DB user id for the logged-in CLI user, or exit.

    The row is created at login. If it is missing but the keyring still
    holds a usable token (e.g. the database file was deleted), it is
    rebuilt from that token rather than forcing a full re-authorization.
    """
    from sqlmodel import Session, select

    from spotifyforge.db.engine import get_engine, init_db
    from spotifyforge.models.models import User

    init_db()
    spotify_user_id = _current_spotify_user_id()
    with Session(get_engine()) as session:
        user = session.exec(select(User).where(User.spotify_id == spotify_user_id)).first()
        if user is not None and user.id is not None:
            return user.id

    return _rebuild_db_user(spotify_user_id)


def _rebuild_db_user(spotify_user_id: str) -> int:
    """Recreate the local User row from the keyring token, or exit."""
    from spotifyforge.auth.oauth import get_spotify_user

    auth = _make_auth()
    try:
        token = _load_token(auth, spotify_user_id)
    except Exception:
        _error_panel(
            "Local user record not found and no stored credentials to rebuild it.\n"
            "Run [bold]spotifyforge auth login[/bold] to authenticate.",
            title="Authentication Required",
        )

    with console.status("Rebuilding local user record..."):
        try:
            profile = _run(get_spotify_user(token.access_token))
        except Exception as exc:
            _error_panel(f"Could not rebuild local user record: {exc}")

    user_id = _upsert_db_user(profile["id"], profile, token)
    console.print(
        "[yellow]Local database was rebuilt from your stored credentials.[/yellow] "
        "Playlists and schedules it held were not restored."
    )
    return user_id


def _upsert_db_user(spotify_id: str, profile: dict[str, Any], token: tekore.Token) -> int:
    """Create or update the local User row (with encrypted tokens).

    Tokens are persisted so scheduled jobs (which authenticate from the
    database) can run for CLI-authenticated users too.
    """
    from sqlmodel import Session, select

    from spotifyforge.core.clients import apply_user_profile, apply_user_tokens
    from spotifyforge.db.engine import get_engine, init_db
    from spotifyforge.models.models import User

    init_db()
    with Session(get_engine()) as session:
        user = session.exec(select(User).where(User.spotify_id == spotify_id)).first()
        if user is None:
            user = User(spotify_id=spotify_id)
        apply_user_profile(user, profile)
        apply_user_tokens(user, token.access_token, token.refresh_token, token.expires_at)
        session.add(user)
        session.commit()
        session.refresh(user)
        assert user.id is not None
        return user.id


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
    version: bool | None = typer.Option(  # noqa: UP007
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
    help="Manage Spotify authentication (OAuth 2.0 authorization-code flow).",
    no_args_is_help=True,
)
app.add_typer(auth_app)


@auth_app.command("login")
def auth_login(
    no_browser: bool = typer.Option(
        False, "--no-browser", help="Print the URL instead of opening a browser."
    ),
) -> None:
    """Log in to Spotify: opens the authorization page, then asks for the redirect URL."""
    auth = _make_auth()
    auth_url, state = auth.begin_login()

    console.print("\nOpen this URL and authorize SpotifyForge:\n")
    console.print(f"  [link]{auth_url}[/link]\n")
    if not no_browser:
        webbrowser.open(auth_url)

    console.print(
        "After authorizing, your browser is sent to the redirect URI "
        "(the page itself may not load — that's fine)."
    )
    redirect_url = typer.prompt("Paste the full redirect URL here")

    try:
        profile = auth.complete_login(redirect_url, expected_state=state)
        token = auth.token_store.load_token(profile["user_id"])
        _upsert_db_user(profile["user_id"], profile, token)
    except Exception as exc:
        _error_panel(f"Login failed: {exc}", title="Authentication Error")

    _current_user_file().parent.mkdir(parents=True, exist_ok=True)
    _current_user_file().write_text(profile["user_id"], encoding="utf-8")

    console.print(
        Panel(
            f"[green]Successfully authenticated as "
            f"[bold]{profile.get('display_name') or profile['user_id']}[/bold]![/green]\n"
            "Your tokens are stored in the OS keyring.",
            title="Login Successful",
            border_style="green",
            expand=False,
        )
    )


@auth_app.command("status")
def auth_status() -> None:
    """Display the current authentication status."""
    path = _current_user_file()
    if not path.exists():
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

    spotify_user_id = path.read_text(encoding="utf-8").strip()
    auth = _make_auth()
    try:
        token = auth.token_store.load_token(spotify_user_id)
    except Exception:
        console.print(
            Panel(
                f"[yellow]Stored login for [bold]{spotify_user_id}[/bold] has no usable "
                "token.[/yellow]\nRun [bold]spotifyforge auth login[/bold] again.",
                title="Auth Status",
                border_style="yellow",
                expand=False,
            )
        )
        return

    expires_at = datetime.fromtimestamp(token.expires_at, tz=UTC)
    table = Table(title="Auth Status", box=box.ROUNDED, show_lines=True)
    table.add_column("Property", style="bold cyan")
    table.add_column("Value", style="white")
    table.add_row("User ID", spotify_user_id)
    table.add_row("Token Expiry", expires_at.strftime("%Y-%m-%d %H:%M:%S UTC"))
    table.add_row(
        "Status",
        "[yellow]Expiring (auto-refreshes on use)[/yellow]"
        if token.is_expiring
        else "[green]Active[/green]",
    )
    console.print(table)


@auth_app.command("logout")
def auth_logout() -> None:
    """Remove stored Spotify tokens."""
    path = _current_user_file()
    if not path.exists():
        console.print("[yellow]Not logged in — nothing to do.[/yellow]")
        return

    spotify_user_id = path.read_text(encoding="utf-8").strip()
    auth = _make_auth()
    try:
        auth.token_store.delete_token(spotify_user_id)
    except Exception:
        pass  # token already gone from the keyring
    path.unlink(missing_ok=True)

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


# ╔═════════════════════════════════════════════════════════════════════════╗
# ║  DISCOVER                                                              ║
# ╚═════════════════════════════════════════════════════════════════════════╝
discover_app = typer.Typer(
    name="discover",
    help="Discover new music through intelligent analysis.",
    no_args_is_help=True,
)
app.add_typer(discover_app)


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


# ╔═════════════════════════════════════════════════════════════════════════╗
# ║  CURATE                                                                ║
# ╚═════════════════════════════════════════════════════════════════════════╝
curate_app = typer.Typer(
    name="curate",
    help="Forge a catalogue of niche playlists from your liked songs.",
    no_args_is_help=True,
)
app.add_typer(curate_app)

# Shared option definitions so `plan`, `forge` and `reflow` cannot drift
# apart — a plan that previewed different clusters than the forge creates
# would make the preview worthless.
_MIN_SIZE = typer.Option(
    12, "--min-size", min=2, help="Smallest genre cluster that becomes a playlist."
)
_MAX_SIZE = typer.Option(
    60, "--max-size", min=10, help="Clusters larger than this are split by decade."
)
_MAX_TRACKS = typer.Option(
    None, "--max-tracks", min=1, help="Only scan the first N liked songs (quick preview)."
)
_EXCLUSIVE = typer.Option(
    False,
    "--exclusive",
    help="Put each track in only its rarest genre (fewer, sharper playlists).",
)
_HARMONIC = typer.Option(
    False,
    "--harmonic",
    help="Sequence by musical key and BPM (needs 'curate features' first).",
)


def _features_or_warn(harmonic: bool):
    """Load cached tempo/key data, explaining if there is none yet."""
    if not harmonic:
        return None
    from spotifyforge.core.audio_features import load_cached_features

    features = load_cached_features()
    if not features:
        _error_panel(
            "No tempo/key data cached yet.\n"
            "Run [bold]spotifyforge curate features --deep[/bold] first.",
            title="Nothing to sequence by",
        )
    keyed = sum(1 for f in features.values() if f.has_key)
    console.print(
        f"[dim]Harmonic ordering: {len(features)} tracks analysed, {keyed} with a key.[/dim]"
    )
    return features


async def _plan(sp, opts, features=None):
    """Plan the catalogue with the pinned expansions folded in.

    Every curate command plans through here: a command that forgot the
    pins would hand reflow a plan that strips pinned tracks off live
    playlists, so the folding is not optional at the call sites.
    """
    from spotifyforge.core.curation import plan_catalogue
    from spotifyforge.core.expansion import load_expansions

    return await plan_catalogue(sp, opts, features, load_expansions())


def _curation_options(min_size, max_size, max_tracks, exclusive):
    from spotifyforge.core.curation import CurationOptions

    return CurationOptions(
        min_size=min_size, max_size=max_size, max_tracks=max_tracks, exclusive=exclusive
    )


def _specs_table(specs) -> Table:
    table = Table(box=box.SIMPLE, header_style="bold cyan")
    table.add_column("#", style="dim", justify="right")
    table.add_column("Playlist", style="white", no_wrap=True, max_width=45)
    table.add_column("Genre", style="green", no_wrap=True, max_width=30)
    table.add_column("Era", style="magenta")
    table.add_column("Tracks", justify="right")
    for idx, spec in enumerate(specs, start=1):
        table.add_row(
            str(idx),
            spec.title,
            spec.genre_label,
            f"{spec.decade}s" if spec.decade else "\u2014",
            str(len(spec.tracks)),
        )
    return table


@curate_app.command("plan")
def curate_plan(
    min_size: int = _MIN_SIZE,
    max_size: int = _MAX_SIZE,
    max_tracks: int | None = _MAX_TRACKS,
    exclusive: bool = _EXCLUSIVE,
    harmonic: bool = _HARMONIC,
) -> None:
    """Preview the playlist catalogue your liked songs would produce (no writes)."""

    opts = _curation_options(min_size, max_size, max_tracks, exclusive)
    features = _features_or_warn(harmonic)
    plan = _run_spotify(
        "Scanning your liked songs...",
        "Failed to plan curation",
        lambda sp: _plan(sp, opts, features),
    )

    console.print(
        Panel(
            f"Liked songs scanned:   [bold]{plan.liked_count}[/bold]\n"
            f"Unique songs:          [bold]{plan.unique_count}[/bold] "
            f"({plan.collapsed_count} duplicate versions collapsed)\n"
            f"Playlists planned:     [bold]{len(plan.specs)}[/bold]\n"
            f"Songs placed:          [bold]{plan.placed_count}[/bold] of {plan.unique_count} "
            f"({plan.unique_count - plan.placed_count} in genres too small to fill a playlist)\n"
            f"Playlist entries:      [bold]{plan.entry_count}[/bold] "
            "(a song can belong to more than one genre)\n"
            f"Sequenced by key+BPM:  [bold]{plan.harmonic_count}[/bold] of {len(plan.specs)} "
            "(the rest had too little key data)",
            title="Curation Plan",
            border_style="cyan",
            expand=False,
        )
    )
    if plan.specs:
        console.print(_specs_table(plan.specs))
        console.print(
            "\nRun [bold]spotifyforge curate forge --limit N[/bold] to create them "
            "(already-created titles are skipped, so repeated runs continue the catalogue)."
        )


@curate_app.command("forge")
def curate_forge(
    limit: int = typer.Option(
        5, "--limit", "-l", min=1, help="Maximum playlists to create this run."
    ),
    min_size: int = _MIN_SIZE,
    max_size: int = _MAX_SIZE,
    max_tracks: int | None = _MAX_TRACKS,
    exclusive: bool = _EXCLUSIVE,
    harmonic: bool = _HARMONIC,
    private: bool = typer.Option(
        False, "--private", help="Create the playlists as private instead of public."
    ),
) -> None:
    """Create the next batch of planned playlists on Spotify (resumable)."""
    from spotifyforge.core.curation import forge_next
    from spotifyforge.core.playlist_manager import PlaylistManager

    owner_id = _db_user_id()
    opts = _curation_options(min_size, max_size, max_tracks, exclusive)
    features = _features_or_warn(harmonic)

    async def _forge(sp):
        plan = await _plan(sp, opts, features)
        created, pending = await forge_next(
            PlaylistManager(sp), owner_id, plan.specs, limit, public=not private
        )
        return created, pending, len(plan.specs)

    created, pending, total = _run_spotify(
        "Forging playlists from your liked songs...",
        "Failed to forge playlists",
        _forge,
    )

    if not created:
        console.print(
            Panel(
                f"All {total} planned playlists already exist \u2014 nothing to create.",
                title="Catalogue complete",
                border_style="green",
                expand=False,
            )
        )
        return

    console.print(
        Panel(
            f"[green]Created {len(created)} playlist(s).[/green]\n"
            f"Remaining in plan: [bold]{pending - len(created)}[/bold] of {total} \u2014 "
            "run the same command again to continue.",
            title="Forge",
            border_style="green",
            expand=False,
        )
    )
    console.print(_specs_table([spec for spec, _ in created]))


@curate_app.command("curators")
def curate_curators(
    limit: int = typer.Option(25, "--limit", "-l", min=1, help="How many curators to list."),
    genres: int = typer.Option(12, "--genres", min=1, help="How many of your genres to search."),
    max_tracks: int | None = _MAX_TRACKS,
) -> None:
    """Find curators whose playlists overlap your liked songs.

    Read-only: it lists people worth following, it does not follow
    anyone. Mass-following strangers to collect follow-backs is the
    artificial-engagement pattern Spotify's rules prohibit, and it risks
    the account it is meant to grow.
    """
    from spotifyforge.core.curation import CurationEngine
    from spotifyforge.core.curators import find_curators, top_genres

    async def _find(sp):
        engine = CurationEngine(sp)
        tracks = await engine.enrich_genres(await engine.fetch_liked(max_tracks=max_tracks))
        me = await sp.current_user()
        seeds = top_genres(tracks, count=genres)
        liked_ids = {t.id for t in tracks}
        return await find_curators(sp, seeds, liked_ids, me.id, limit=limit), seeds, len(tracks)

    curators, seeds, scanned = _run_spotify(
        "Searching for curators who share your taste...",
        "Failed to find curators",
        _find,
    )

    console.print(
        Panel(
            f"Liked songs scanned: [bold]{scanned}[/bold]\n"
            f"Genres searched:     {', '.join(seeds[:6])}"
            + (f" (+{len(seeds) - 6} more)" if len(seeds) > 6 else "")
            + f"\nCurators found:      [bold]{len(curators)}[/bold]",
            title="Curator search",
            border_style="cyan",
            expand=False,
        )
    )
    if not curators:
        console.print("No curators with overlapping taste turned up. Try [bold]--genres 20[/bold].")
        return

    # The profile URL rides on the curator's name as a terminal hyperlink
    # rather than taking a column of its own — five columns do not fit an
    # 80-character terminal, and the overlap count is the part to read.
    table = Table(box=box.SIMPLE, header_style="bold cyan")
    table.add_column("#", style="dim", justify="right")
    table.add_column("Curator", style="white", overflow="ellipsis", max_width=30)
    table.add_column("Shared", justify="right", style="bold green")
    table.add_column("Their playlist", style="magenta", overflow="ellipsis", max_width=34)
    for idx, c in enumerate(curators, start=1):
        table.add_row(
            str(idx),
            f"[link={c.url}]{c.display_name}[/link]",
            str(c.shared_tracks),
            c.example_playlist,
        )
    console.print(table)
    console.print("\n[dim]Profiles:[/dim]")
    for idx, c in enumerate(curators, start=1):
        console.print(f"  [dim]{idx:>2}.[/dim] {c.url}")
    console.print(
        "\n[dim]'Shared' counts your own liked songs found in that curator's playlist. "
        "Open a profile and follow the ones you actually like — that is the kind of "
        "follow Spotify rewards.[/dim]"
    )


@curate_app.command("covers")
def curate_covers(
    min_size: int = _MIN_SIZE,
    max_size: int = _MAX_SIZE,
    max_tracks: int | None = _MAX_TRACKS,
    exclusive: bool = _EXCLUSIVE,
    overwrite: bool = typer.Option(
        False, "--overwrite", help="Replace covers that are already set."
    ),
    photos: bool = typer.Option(
        False,
        "--photos",
        help="Use licensed photos (Pexels) matched to each playlist's vibe, "
        "covering personal playlists too. Needs SPOTIFYFORGE_PEXELS_API_KEY.",
    ),
) -> None:
    """Give your playlists cover art.

    By default each forged playlist gets generated art whose colour is
    derived from its genre — stable across runs, one collection. With
    [bold]--photos[/bold], every owned playlist instead gets a licensed
    photograph matched to its vibe; picks are pinned locally so re-runs
    are stable, and a Pexels rate limit pauses the run resumably rather
    than failing it.
    """
    if photos:
        _photo_covers(min_size, max_size, max_tracks, exclusive, overwrite)
        return

    from spotifyforge.core.curation import apply_covers
    from spotifyforge.core.playlist_manager import PlaylistManager

    opts = _curation_options(min_size, max_size, max_tracks, exclusive)

    async def _covers(sp):
        plan = await _plan(sp, opts)
        uploaded, failed = await apply_covers(
            PlaylistManager(sp), sp, plan.specs, overwrite=overwrite
        )
        return uploaded, failed, len(plan.specs)

    uploaded, failed, total = _run_spotify(
        "Painting playlist covers...", "Failed to set covers", _covers
    )

    if not uploaded:
        console.print(
            Panel(
                f"All {total} playlists already have artwork "
                "(use [bold]--overwrite[/bold] to replace it).",
                title="Nothing to paint",
                border_style="green",
                expand=False,
            )
        )
        return

    console.print(
        Panel(
            f"[green]Set artwork on {len(uploaded)} playlist(s)[/green] of {total}."
            + (f"\n[yellow]{len(failed)} failed[/yellow]" if failed else ""),
            title="Covers",
            border_style="green",
            expand=False,
        )
    )


def _photo_covers(min_size, max_size, max_tracks, exclusive, overwrite) -> None:
    """The --photos path of ``curate covers``: every owned playlist."""
    from spotifyforge.core.photo_covers import PexelsSource, apply_photo_covers, picks_path
    from spotifyforge.core.playlist_manager import PlaylistManager

    if not settings.pexels_api_key:
        _error_panel(
            "Photo covers need a Pexels key.\n"
            "Get a free one at pexels.com/api and set "
            "[bold]SPOTIFYFORGE_PEXELS_API_KEY[/bold].",
            title="Pexels key missing",
        )

    opts = _curation_options(min_size, max_size, max_tracks, exclusive)

    async def _photo(sp):
        plan = await _plan(sp, opts)
        vibe_by_title = {s.title: s.genre_label for s in plan.specs}
        me = await sp.current_user()
        owned = [
            p for p in await PlaylistManager(sp).get_user_playlists() if p["owner_id"] == me.id
        ]
        # Forged playlists search by genre; personal ones by their name.
        targets = [(p["name"], p["id"], vibe_by_title.get(p["name"], p["name"])) for p in owned]
        source = PexelsSource(settings.pexels_api_key)
        try:
            covered, failed, limited = await apply_photo_covers(
                sp, targets, source, overwrite=overwrite
            )
        finally:
            await source.close()
        return covered, failed, limited, len(targets)

    covered, failed, limited, total = _run_spotify(
        "Matching photographs to playlists...", "Failed to set photo covers", _photo
    )

    body = f"[green]Photo-covered {len(covered)} playlist(s)[/green] of {total}."
    if failed:
        body += f"\n[yellow]{len(failed)} had no usable photo[/yellow] (kept their current art)."
    if limited:
        body += (
            "\n[yellow]Pexels' hourly limit reached[/yellow] — progress is saved; "
            "re-run in an hour to continue."
        )
    if not covered and not failed and not limited:
        body = f"All {total} playlists already have pinned photos (use [bold]--overwrite[/bold])."
    console.print(Panel(body, title="Photo covers", border_style="green", expand=False))
    console.print(f"[dim]Picks + attribution: {picks_path()}[/dim]")


@curate_app.command("describe")
def curate_describe(
    min_size: int = _MIN_SIZE,
    max_size: int = _MAX_SIZE,
    max_tracks: int | None = _MAX_TRACKS,
    exclusive: bool = _EXCLUSIVE,
    harmonic: bool = _HARMONIC,
) -> None:
    """Refresh forged playlists' descriptions from the current templates.

    Descriptions are the only text besides the name that Spotify's
    search indexes. This rewrites each forged playlist's description to
    the current template — leading with the playlist's own artists —
    without touching tracks, titles, followers, or artwork. Playlists
    already carrying the wanted text are skipped.
    """
    from spotifyforge.core.curation import apply_descriptions
    from spotifyforge.core.playlist_manager import PlaylistManager

    opts = _curation_options(min_size, max_size, max_tracks, exclusive)
    features = _features_or_warn(harmonic)

    async def _push(sp):
        plan = await _plan(sp, opts, features)
        updated, failed = await apply_descriptions(PlaylistManager(sp), sp, plan.specs)
        return updated, failed, len(plan.specs)

    updated, failed, total = _run_spotify(
        "Rewriting playlist descriptions...", "Failed to update descriptions", _push
    )

    if not updated and not failed:
        console.print(
            Panel(
                f"All {total} playlists already carry the current descriptions.",
                title="Nothing to rewrite",
                border_style="green",
                expand=False,
            )
        )
        return

    console.print(
        Panel(
            f"[green]Updated {len(updated)} description(s)[/green] of {total}."
            + (f"\n[yellow]{len(failed)} failed[/yellow]" if failed else ""),
            title="Descriptions",
            border_style="green",
            expand=False,
        )
    )


@curate_app.command("expand")
def curate_expand(
    target: int = typer.Option(
        12, "--target", min=3, help="Grow playlists below this many tracks."
    ),
    limit: int = typer.Option(
        10, "--limit", "-l", min=1, help="Maximum playlists to expand this run."
    ),
    min_size: int = _MIN_SIZE,
    max_size: int = _MAX_SIZE,
    max_tracks: int | None = _MAX_TRACKS,
    exclusive: bool = _EXCLUSIVE,
    harmonic: bool = _HARMONIC,
) -> None:
    """Grow thin playlists with unheard tracks from the same niche.

    Searches Spotify for more of a playlist's genre — music you have
    never heard — and pins the picks locally. Nothing is written to
    Spotify here: run [bold]curate reflow[/bold] afterwards to push the
    grown playlists (and [bold]curate describe[/bold] to refresh their
    descriptions). Repeat runs continue where the last one stopped.
    """
    from spotifyforge.core.curation import plan_catalogue
    from spotifyforge.core.expansion import expand_catalogue, expansions_path, load_expansions

    opts = _curation_options(min_size, max_size, max_tracks, exclusive)
    features = _features_or_warn(harmonic)

    async def _expand(sp):
        # Loaded once and shared: the plan folds these pins in, and
        # expand_catalogue appends this run's picks to the same dict.
        pins = load_expansions()
        plan = await plan_catalogue(sp, opts, features, pins)
        return await expand_catalogue(sp, plan.specs, target=target, limit=limit, expansions=pins)

    added, thin = _run_spotify(
        "Digging for unheard tracks...", "Failed to expand playlists", _expand
    )

    if not added:
        console.print(
            Panel(
                f"No playlists below {target} tracks had unheard music to pin "
                f"({thin} are below the target).",
                title="Nothing to expand",
                border_style="green",
                expand=False,
            )
        )
        return

    table = Table(box=box.SIMPLE, header_style="bold cyan")
    table.add_column("Playlist", style="white", no_wrap=True, max_width=45)
    table.add_column("Pinned", justify="right")
    table.add_column("New artists", style="green", max_width=50)
    for title, tracks in added.items():
        artists = ", ".join(dict.fromkeys(t.artist_names[0] for t in tracks if t.artist_names))
        table.add_row(title, str(len(tracks)), artists)
    console.print(table)
    console.print(
        Panel(
            f"[green]Pinned {sum(len(t) for t in added.values())} track(s) across "
            f"{len(added)} playlist(s)[/green] of {thin} below the target; "
            "re-run to continue.\n"
            "Run [bold]spotifyforge curate reflow[/bold] to push them to Spotify.",
            title="Expanded",
            border_style="green",
            expand=False,
        )
    )
    console.print(f"[dim]Pins: {expansions_path()}[/dim]")


@curate_app.command("stats")
def curate_stats() -> None:
    """Snapshot follower counts and show growth since the last run.

    Spotify keeps no follower history, so growth is only measurable if
    each run records what it saw. Snapshots accumulate locally; run this
    on any cadence and it reports the change since the previous run.
    """
    from spotifyforge.core.stats import record_snapshot

    snapshot, growth, path = _run_spotify(
        "Counting followers...", "Failed to read follower counts", record_snapshot
    )

    lines = [
        f"Account followers:   [bold]{snapshot.account_followers}[/bold]",
        f"Owned playlists:     [bold]{len(snapshot.playlists)}[/bold]",
        f"Playlist followers:  [bold]{snapshot.playlist_followers}[/bold] "
        f"across {snapshot.followed_playlists} playlist(s)",
    ]
    if growth is None:
        lines.append("First snapshot — the next run will show growth.")
    else:
        lines.append(
            f"Since {growth.since.split('T')[0]}:  "
            f"account [bold]{growth.account_delta:+d}[/bold], "
            f"playlist followers [bold]{growth.playlist_delta:+d}[/bold]"
        )
    console.print(Panel("\n".join(lines), title="Growth", border_style="cyan", expand=False))

    if growth is not None and growth.movers:
        table = Table(box=box.SIMPLE, header_style="bold cyan")
        table.add_column("Playlist", style="white", no_wrap=True, max_width=45)
        table.add_column("Followers", justify="right")
        for name, delta in growth.movers[:10]:
            table.add_row(name, f"{delta:+d}")
        console.print(table)

    console.print(f"[dim]History: {path}[/dim]")


@curate_app.command("features")
def curate_features(
    deep: bool = typer.Option(
        False,
        "--deep",
        help="Also fetch musical key via MusicBrainz/AcousticBrainz (~1 track/sec).",
    ),
    max_tracks: int | None = _MAX_TRACKS,
) -> None:
    """Fetch tempo and key for your liked songs, caching them on disk.

    Spotify's own audio-features endpoint is withdrawn for this app, so
    tempo comes from Deezer and musical key from AcousticBrainz, both
    looked up by ISRC. Results are cached, so this is slow once and
    instant afterwards; re-run it after liking new music.
    """
    from spotifyforge.core.audio_features import feature_cache_path, gather_features
    from spotifyforge.core.curation import CurationEngine
    from spotifyforge.core.expansion import load_expansions

    async def _fetch(sp):
        return await CurationEngine(sp).fetch_liked(max_tracks=max_tracks)

    tracks = _run_spotify("Reading your liked songs...", "Failed to read library", _fetch)
    # Pinned expansion tracks play in the same playlists, so they need
    # tempo/key just as much as the liked songs they sit between.
    pinned = [t for entries in load_expansions().values() for t in entries]
    isrcs = sorted({t.isrc for t in [*tracks, *pinned] if t.isrc})

    if deep:
        console.print(
            f"[yellow]Deep lookup resolves {len(isrcs)} recordings in batches of 25, "
            "pausing between MusicBrainz calls as their rate limit asks.[/yellow]"
        )

    from rich.progress import Progress

    with Progress(transient=True) as progress:
        task = progress.add_task("Looking up tempo/key...", total=len(isrcs))
        features, learned = _run(
            gather_features(isrcs, deep=deep, progress=lambda: progress.advance(task))
        )

    analysed = sum(1 for f in features.values() if f.tempo is not None)
    keyed = sum(1 for f in features.values() if f.has_key)
    console.print(
        Panel(
            f"Recordings with an ISRC: [bold]{len(isrcs)}[/bold] "
            f"from {len(tracks)} liked + {len(pinned)} pinned tracks\n"
            f"Newly resolved:          [bold]{learned}[/bold]\n"
            f"Tempo known:             [bold]{analysed}[/bold]\n"
            f"Key known:               [bold]{keyed}[/bold]"
            + ("" if deep else "  [dim](use --deep to fetch keys)[/dim]")
            + f"\nCache: {feature_cache_path()}",
            title="Audio features",
            border_style="cyan",
            expand=False,
        )
    )
    if keyed:
        console.print(
            "\nRun [bold]spotifyforge curate reflow --harmonic[/bold] to re-sequence "
            "your playlists by key and BPM."
        )


@curate_app.command("reflow")
def curate_reflow(
    min_size: int = _MIN_SIZE,
    max_size: int = _MAX_SIZE,
    max_tracks: int | None = _MAX_TRACKS,
    exclusive: bool = _EXCLUSIVE,
    harmonic: bool = _HARMONIC,
) -> None:
    """Re-sequence playlists you already forged, keeping their URLs and followers."""
    from spotifyforge.core.curation import reflow
    from spotifyforge.core.playlist_manager import PlaylistManager

    opts = _curation_options(min_size, max_size, max_tracks, exclusive)
    features = _features_or_warn(harmonic)

    async def _reflow(sp):
        plan = await _plan(sp, opts, features)
        rewritten, failed = await reflow(PlaylistManager(sp), sp, plan.specs)
        return rewritten, failed, len(plan.specs)

    rewritten, failed, total = _run_spotify(
        "Re-sequencing your forged playlists...", "Failed to reflow playlists", _reflow
    )

    if not rewritten:
        console.print(
            Panel(
                f"All {total} planned playlists are already in the right order.",
                title="Nothing to reflow",
                border_style="green",
                expand=False,
            )
        )
        return

    table = Table(box=box.SIMPLE, header_style="bold cyan")
    table.add_column("#", style="dim", justify="right")
    table.add_column("Playlist", style="white", no_wrap=True, max_width=55)
    table.add_column("Tracks", justify="right")
    for idx, (title, count) in enumerate(rewritten, start=1):
        table.add_row(str(idx), title, str(count))

    console.print(
        Panel(
            f"[green]Re-sequenced {len(rewritten)} playlist(s)[/green] of {total} planned."
            + (f"\n[yellow]{len(failed)} could not be updated[/yellow]" if failed else ""),
            title="Reflow",
            border_style="green",
            expand=False,
        )
    )
    console.print(table)


@curate_app.command("rename")
def curate_rename(
    apply: bool = typer.Option(
        False, "--apply", help="Actually rename (default: show the mapping and stop)."
    ),
    limit: int | None = typer.Option(None, "--limit", "-l", help="Rename at most this many."),
    min_size: int = _MIN_SIZE,
    max_size: int = _MAX_SIZE,
    max_tracks: int | None = _MAX_TRACKS,
    exclusive: bool = _EXCLUSIVE,
    harmonic: bool = _HARMONIC,
) -> None:
    """Move forged playlists onto the current naming scheme.

    Playlist identity here is the title, so changing the naming scheme
    without renaming the live playlists would make the next
    [bold]forge[/bold] create duplicates and strand the originals —
    followers, artwork and all. This renames them in place instead, and
    carries their cover picks across.

    Shows the mapping and stops unless [bold]--apply[/bold] is passed.
    Playlists already carrying their new name are skipped, so an
    interrupted run can simply be re-run.
    """
    from spotifyforge.core.playlist_manager import PlaylistManager
    from spotifyforge.core.renaming import apply_renames, plan_renames

    opts = _curation_options(min_size, max_size, max_tracks, exclusive)
    features = _features_or_warn(harmonic)

    async def _rename(sp):
        plan = await _plan(sp, opts, features)
        renames = plan_renames(plan.specs)[: limit or None]
        if not apply:
            return renames, [], [], False
        renamed, already, failed = await apply_renames(PlaylistManager(sp), sp, renames)
        return renamed, already, failed, True

    renames, already, failed, applied = _run_spotify(
        "Renaming playlists..." if apply else "Working out the new names...",
        "Failed to rename playlists",
        _rename,
    )

    if not renames and not already:
        console.print(
            Panel(
                "Every playlist already carries its current name.",
                title="Nothing to rename",
                border_style="green",
                expand=False,
            )
        )
        return

    if renames:
        table = Table(box=box.SIMPLE, header_style="bold cyan")
        table.add_column("Was", style="dim", max_width=44)
        table.add_column("Now", style="green", max_width=44)
        for rename in renames[:40]:
            table.add_row(rename.old, rename.new)
        console.print(table)
        if len(renames) > 40:
            console.print(f"[dim]…and {len(renames) - 40} more[/dim]")

    if applied:
        body = f"[green]Renamed {len(renames)} playlist(s)[/green] in place."
        if already:
            body += f"\n{len(already)} already carried the new name."
        if failed:
            body += f"\n[yellow]{len(failed)} could not be renamed[/yellow] (not found on Spotify)."
        body += "\nFollowers, artwork and descriptions are unchanged."
    else:
        body = (
            f"[bold]{len(renames)} playlist(s)[/bold] would be renamed.\n"
            "Nothing has changed — re-run with [bold]--apply[/bold] to do it."
        )
    console.print(Panel(body, title="Rename", border_style="green", expand=False))


# ╔═════════════════════════════════════════════════════════════════════════╗
# ║  EXPORT                                                                ║
# ╚═════════════════════════════════════════════════════════════════════════╝
export_app = typer.Typer(
    name="export",
    help="Hand this library's knowledge to other music platforms.",
    no_args_is_help=True,
)
app.add_typer(export_app)


@export_app.command("library")
def export_library(
    out: Path | None = typer.Option(
        None,
        "--out",
        "-o",
        help="Where to write (default: music-library.json beside the database).",
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
    from spotifyforge.core.curation import CurationEngine
    from spotifyforge.core.expansion import load_expansions
    from spotifyforge.core.export import build_library_export, write_export
    from spotifyforge.models.models import utc_now

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


# ╔═════════════════════════════════════════════════════════════════════════╗
# ║  CONFIG                                                                ║
# ╚═════════════════════════════════════════════════════════════════════════╝
config_app = typer.Typer(
    name="config",
    help="View and modify SpotifyForge configuration.",
    no_args_is_help=True,
)
app.add_typer(config_app)

_SECRET_FIELDS = {"spotify_client_id", "spotify_client_secret", "secret_key"}


@config_app.command("show")
def config_show() -> None:
    """Display the current SpotifyForge configuration (secrets masked)."""
    import os

    table = Table(
        title="SpotifyForge Configuration",
        box=box.ROUNDED,
        show_lines=True,
        header_style="bold magenta",
    )
    table.add_column("Key", style="bold cyan")
    table.add_column("Value", style="white")
    table.add_column("Source", style="dim")

    for field_name in Settings.model_fields:
        value = getattr(settings, field_name)
        display_value = str(value)

        if field_name in _SECRET_FIELDS and value:
            display_value = "****" + display_value[-4:] if len(display_value) > 4 else "****"

        env_key = f"SPOTIFYFORGE_{field_name.upper()}"
        source = "env" if os.environ.get(env_key) else "default"
        table.add_row(field_name, display_value, source)

    console.print(table)


@config_app.command("set")
def config_set(
    key: str = typer.Argument(..., help="Configuration key (e.g. 'spotify_client_id')."),
    value: str = typer.Argument(..., help="New value for the configuration key."),
) -> None:
    """Set a configuration value in the .env file."""
    valid_keys = set(Settings.model_fields.keys())
    if key not in valid_keys:
        _error_panel(
            f"Unknown configuration key: [bold]{key}[/bold]\n\n"
            f"Valid keys: {', '.join(sorted(valid_keys))}",
            title="Invalid Key",
        )

    env_key = f"SPOTIFYFORGE_{key.upper()}"
    env_path = Path(".env")

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

    display_value = "****" if key in _SECRET_FIELDS else value
    console.print(
        f"[green]Configuration updated:[/green] "
        f"[bold]{key}[/bold] = [cyan]{display_value}[/cyan]  "
        f"(written to .env as {env_key})"
    )


# ---------------------------------------------------------------------------
# Module guard — allow ``python -m spotifyforge.cli.app``
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app()
