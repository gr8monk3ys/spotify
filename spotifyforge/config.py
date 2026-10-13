"""Application configuration using pydantic-settings."""

import os
from pathlib import Path

from media_core.paths import movies_dir as _default_movies_dir
from media_core.paths import music_dir as _default_music_dir
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ``_default_music_dir`` is ``media_core.paths.music_dir``: ``~/.music`` unless
# the plain ``MUSIC_DIR`` variable says otherwise. The directory is shared with
# the other collection tools, none of which know this package's
# ``SPOTIFYFORGE_`` prefix — so the unprefixed name is honoured here too, and
# resolving it the same way in every tool is the whole point of sharing the
# function. ``SPOTIFYFORGE_MUSIC_DIR`` still wins, via the normal prefixed
# lookup.


class Settings(BaseSettings):
    # ``extra="ignore"``: a ``.env`` written for an older version still carries
    # keys this class no longer has (SECRET_KEY, DB_PATH, WEB_PORT, ...), and
    # the default would refuse to start over them.
    model_config = SettingsConfigDict(
        env_prefix="SPOTIFYFORGE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Spotify OAuth
    spotify_client_id: str = ""
    spotify_client_secret: str = ""
    spotify_redirect_uri: str = "http://localhost:8000/api/auth/callback"

    # Local state: the logged-in user marker and the JSON sidecars.
    data_dir: Path = Path.home() / ".spotifyforge"

    # Shared interchange directory (music-library.json, discogs.json).
    music_dir: Path = Field(default_factory=_default_music_dir)
    # Where the letterboxd repo writes letterboxd.json (MOVIES_DIR, ~/.movies).
    movies_dir: Path = Field(default_factory=_default_movies_dir)

    # Logging
    log_level: str = "INFO"

    # Pexels (photo cover art); free key from pexels.com/api.
    pexels_api_key: str = ""

    @model_validator(mode="after")
    def _expand_paths(self) -> "Settings":
        self.data_dir = self.data_dir.expanduser()
        self.music_dir = self.music_dir.expanduser()
        self.movies_dir = self.movies_dir.expanduser()
        return self


settings = Settings()


def sidecar_path(name: str) -> Path:
    """Where a local data file (tempo/key cache, stats history, pins) lives."""
    return settings.data_dir / name


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
