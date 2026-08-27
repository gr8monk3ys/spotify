"""SpotifyForge CLI — the main user-facing interface.

Built with Typer + Rich. Every sub-command group lives in its own module
under :mod:`spotifyforge.cli` as a ``typer.Typer`` instance, and is added
to the root ``app`` here. Shared helpers (console, auth, DB user lookup)
live in :mod:`spotifyforge.cli._shared`.

Command bodies are thin async wrappers over the same core services the web
API uses (:class:`PlaylistManager`, :class:`DiscoveryEngine`,
:class:`SchedulerService`), authenticated through the OS keyring.

Entry-point (registered in ``pyproject.toml``):
    spotifyforge = "spotifyforge.cli.app:app"
"""

from __future__ import annotations

import typer

from spotifyforge.cli import auth, config, curate, discover, export, library, playlist, schedule
from spotifyforge.cli._shared import _version_callback

# ---------------------------------------------------------------------------
# Root application
# ---------------------------------------------------------------------------
app = typer.Typer(
    name="spotifyforge",
    help="Curate, discover, and schedule Spotify playlists from the terminal.",
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


app.add_typer(auth.auth_app)
app.add_typer(playlist.playlist_app)
app.add_typer(discover.discover_app)
app.add_typer(curate.curate_app)
app.add_typer(export.export_app)
app.add_typer(library.library_app)
app.add_typer(schedule.schedule_app)
app.add_typer(config.config_app)


# ---------------------------------------------------------------------------
# Module guard — allow ``python -m spotifyforge.cli.app``
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app()
