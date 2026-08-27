"""`spotifyforge config` commands."""

from __future__ import annotations

from pathlib import Path

import typer
from rich import box
from rich.table import Table

from spotifyforge.cli._shared import (
    _error_panel,
    console,
)
from spotifyforge.config import Settings, settings

# ╔═════════════════════════════════════════════════════════════════════════╗
# ║  CONFIG                                                                ║
# ╚═════════════════════════════════════════════════════════════════════════╝
config_app = typer.Typer(
    name="config",
    help="View and modify SpotifyForge configuration.",
    no_args_is_help=True,
)

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
