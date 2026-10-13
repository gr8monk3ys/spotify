"""Helpers shared by every CLI command group: console, auth, client construction."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import NoReturn

import tekore
import typer
from rich.console import Console
from rich.panel import Panel

import spotifyforge
from spotifyforge.auth import session

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
    return session.current_user_file()


def _current_spotify_user_id() -> str:
    """Return the Spotify user id of the logged-in CLI user, or exit."""
    try:
        return session.current_user_id()
    except session.NotLoggedInError:
        _error_panel(
            "Not logged in. Run [bold]spotifyforge auth login[/bold] first.",
            title="Authentication Required",
        )


def _make_auth():
    """Build a keyring-backed :class:`SpotifyAuth`, or exit with guidance."""
    from spotifyforge.auth.oauth import AuthenticationError

    try:
        return session.make_auth()
    except AuthenticationError as exc:
        _error_panel(str(exc), title="Configuration Error")


def _spotify_client() -> tekore.Spotify:
    """Return an authenticated async Spotify client for the CLI user."""
    from spotifyforge.core.clients import build_spotify

    auth = _make_auth()
    spotify_user_id = _current_spotify_user_id()
    try:
        token = session.load_token(auth, spotify_user_id)
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
            if "insufficient client scope" in detail.lower():
                # A saved token carries the scopes it was granted, so
                # adding one to REQUIRED_SCOPES leaves every existing
                # token short of it. Spotify answers with the offending
                # URL and nothing actionable; the fix is always the same.
                _error_panel(
                    "Your saved token was granted before this command's "
                    "permissions existed.\nRun [bold]spotifyforge auth login[/bold] "
                    "to re-authorise, then try again.",
                    title="Re-authorisation needed",
                )
            _error_panel(f"{error_msg}: {detail}")
