"""Application configuration using pydantic-settings."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SPOTIFYFORGE_",
        env_file=".env",
        env_file_encoding="utf-8",
    )

    # Spotify OAuth
    spotify_client_id: str = ""
    spotify_client_secret: str = ""
    spotify_redirect_uri: str = "http://localhost:8888/callback"

    # Database
    db_path: Path = Path.home() / ".spotifyforge" / "spotifyforge.db"

    # Scheduling
    scheduler_enabled: bool = True

    # Web server
    web_host: str = "127.0.0.1"
    web_port: int = 8000

    # Cache TTLs (seconds)
    cache_ttl_audio_features: int = 0  # indefinite
    cache_ttl_track_metadata: int = 604800  # 7 days
    cache_ttl_artist_data: int = 86400  # 24 hours
    cache_ttl_playlist_contents: int = 3600  # 1 hour


settings = Settings()
