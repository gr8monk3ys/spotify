"""Application configuration using pydantic-settings."""

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
    spotify_redirect_uri: str = "http://localhost:8888/callback"

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

    # Cache TTLs (seconds)
    cache_ttl_audio_features: int = 0  # indefinite
    cache_ttl_track_metadata: int = 604800  # 7 days
    cache_ttl_artist_data: int = 86400  # 24 hours
    cache_ttl_playlist_contents: int = 3600  # 1 hour

    @model_validator(mode="after")
    def _validate_config(self) -> "Settings":
        """Validate that required settings are present for production."""
        if self.environment == "production":
            missing = []
            if not self.spotify_client_id:
                missing.append("SPOTIFYFORGE_SPOTIFY_CLIENT_ID")
            if not self.spotify_client_secret:
                missing.append("SPOTIFYFORGE_SPOTIFY_CLIENT_SECRET")
            if not self.secret_key:
                missing.append("SPOTIFYFORGE_SECRET_KEY")
            if missing:
                raise ValueError(
                    f"Production mode requires these environment variables: {', '.join(missing)}"
                )
        return self


settings = Settings()
