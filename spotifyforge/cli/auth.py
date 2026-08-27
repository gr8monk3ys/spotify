"""`spotifyforge auth` commands."""

from __future__ import annotations

import webbrowser
from datetime import UTC, datetime

import typer
from rich import box
from rich.panel import Panel
from rich.table import Table

from spotifyforge.cli._shared import (
    _current_user_file,
    _error_panel,
    _make_auth,
    _upsert_db_user,
    console,
)

# ╔═════════════════════════════════════════════════════════════════════════╗
# ║  AUTH                                                                  ║
# ╚═════════════════════════════════════════════════════════════════════════╝
auth_app = typer.Typer(
    name="auth",
    help="Manage Spotify authentication (OAuth 2.0 authorization-code flow).",
    no_args_is_help=True,
)


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
