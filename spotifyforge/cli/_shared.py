"""Helpers shared by every CLI command group: console, auth, DB user lookup."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, NoReturn

import tekore
import typer
from rich.console import Console
from rich.panel import Panel

import spotifyforge
from spotifyforge.config import settings

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
