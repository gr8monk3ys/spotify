"""Application configuration using pydantic-settings."""

import os
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SPOTIFYFORGE_",
        env_file=".env",
        env_file_encoding="utf-8",
    )

    # Environment
    environment: str = "development"  # "development" or "production"

    # Spotify OAuth
    spotify_client_id: str = ""
    spotify_client_secret: str = ""
    spotify_redirect_uri: str = "http://localhost:8000/api/auth/callback"

    # Security
    secret_key: str = ""

    # Database
    db_path: Path = Path.home() / ".spotifyforge" / "spotifyforge.db"
    database_url: str = ""  # If set, overrides db_path; use postgresql://... for production

    # Scheduling
    scheduler_enabled: bool = True

    # Web server
    web_host: str = "127.0.0.1"
    web_port: int = 8000

    # Logging
    log_level: str = "INFO"

    @model_validator(mode="after")
    def _validate_config(self) -> "Settings":
        """Validate required settings and normalise paths.

        Any environment other than the literal ``development`` is treated
        as production-like: credentials and the secret key must be set
        explicitly so nothing silently falls back to insecure defaults.
        """
        self.db_path = self.db_path.expanduser()
        if self.environment != "development":
            missing = []
            if not self.spotify_client_id:
                missing.append("SPOTIFYFORGE_SPOTIFY_CLIENT_ID")
            if not self.spotify_client_secret:
                missing.append("SPOTIFYFORGE_SPOTIFY_CLIENT_SECRET")
            if not self.secret_key:
                missing.append("SPOTIFYFORGE_SECRET_KEY")
            if missing:
                raise ValueError(
                    f"Environment {self.environment!r} requires these "
                    f"environment variables: {', '.join(missing)}"
                )
        return self


settings = Settings()


def sidecar_path(name: str) -> Path:
    """Where a local data file lives: beside the SQLite database.

    Sidecars (the tempo/key cache, the stats history) deliberately follow
    ``db_path`` even when ``database_url`` points the application at
    another database entirely, so under Postgres they still land in the
    local config directory rather than nowhere.
    """
    return settings.db_path.parent / name


def write_json_atomic(path: Path, data: object) -> None:
    """Serialize *data* to *path* without a torn-file window.

    A plain ``write_text`` truncates first, so an interrupt lands in
    exactly the window a checkpoint exists to protect. Write a sibling
    file and rename — ``os.replace`` is atomic on POSIX and Windows.
    """
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)
