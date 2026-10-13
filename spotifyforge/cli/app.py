"""SpotifyForge CLI — the main user-facing interface.

Built with Typer + Rich. Every sub-command group lives in its own module
under :mod:`spotifyforge.cli` as a ``typer.Typer`` instance, and is added
to the root ``app`` here. Shared helpers (console, auth)
live in :mod:`spotifyforge.cli._shared`.

Command bodies are thin async wrappers over the core services
(:class:`PlaylistManager`, :class:`DiscoveryEngine`, the curation engine),
authenticated through the OS keyring. :mod:`spotifyforge.mcp_server` exposes
the same services to MCP clients.

Entry-point (registered in ``pyproject.toml``):
    spotifyforge = "spotifyforge.cli.app:app"
"""

from __future__ import annotations

import typer

from spotifyforge.cli import auth, config, curate, discover, export, library, playlist
from spotifyforge.cli._shared import _version_callback

# ---------------------------------------------------------------------------
# Root application
# ---------------------------------------------------------------------------
app = typer.Typer(
    name="spotifyforge",
    help="Curate and discover Spotify playlists from the terminal.",
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
    """SpotifyForge CLI — curate and discover Spotify playlists."""


@app.command("mcp")
def mcp_serve() -> None:
    """Serve the curation tools to an MCP client over stdio.

    Meant to be launched by the client (Claude Code, Claude Desktop), not
    typed by hand: it speaks JSON-RPC on stdin/stdout and prints nothing
    else. Uses the login from [bold]spotifyforge auth login[/bold].
    """
    from spotifyforge.mcp_server import server

    server.run()


app.add_typer(auth.auth_app)
app.add_typer(playlist.playlist_app)
app.add_typer(discover.discover_app)
app.add_typer(curate.curate_app)
app.add_typer(export.export_app)
app.add_typer(library.library_app)
app.add_typer(config.config_app)


# ---------------------------------------------------------------------------
# Module guard — allow ``python -m spotifyforge.cli.app``
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app()
