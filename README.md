# SpotifyForge

The all-in-one platform for serious Spotify playlist curators.

SpotifyForge gives you a powerful CLI and REST API to manage, curate, discover,
and automate your Spotify playlists -- backed by a local cache database and a
built-in scheduler for hands-off playlist maintenance.

## Feature Highlights

- **Playlist management** -- list, inspect, create, sync, export (JSON/CSV), and deduplicate playlists
- **Music discovery** -- top tracks, deep cuts, genre-based playlists, and time-capsule generators
- **Automated scheduling** -- cron-driven jobs for syncing, archiving Discover Weekly, deduplication, and genre refresh
- **REST API** -- full FastAPI server with OAuth login, CRUD endpoints, and Swagger docs at `/docs`
- **Rich CLI** -- beautiful terminal output powered by Typer + Rich
- **Local cache** -- SQLite database with async support (aiosqlite) for offline-friendly workflows
- **Type-safe** -- strict mypy, Pydantic v2 settings, and PEP 561 `py.typed` marker

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                   User / Browser                    │
└──────────┬──────────────────┬───────────────────────┘
           │ CLI (Typer)      │ HTTP (FastAPI)
           ▼                  ▼
┌──────────────────┐ ┌───────────────────┐
│ spotifyforge.cli │ │ spotifyforge.web  │
│   (Typer app)    │ │ (FastAPI + routes) │
└────────┬─────────┘ └────────┬──────────┘
         │                    │
         ▼                    ▼
┌─────────────────────────────────────────┐
│           spotifyforge.core             │
│  PlaylistManager · DiscoveryEngine      │
│  SchedulerService                       │
└────────┬──────────────────┬─────────────┘
         │                  │
         ▼                  ▼
┌─────────────────┐ ┌──────────────────┐
│ spotifyforge.db │ │ spotifyforge.auth│
│ SQLite (async)  │ │ Spotify OAuth    │
│ SQLModel models │ │ (tekore / PKCE)  │
└─────────────────┘ └──────────────────┘
```

## Quick Start

### 1. Install

```bash
pip install -e .
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env and add your Spotify client ID and secret.
# Get credentials at https://developer.spotify.com/dashboard
```

### 3. Authenticate

```bash
spotifyforge auth login
```

This opens your browser for Spotify OAuth. Tokens are stored securely via
`keyring`.

### 4. Run Your First Command

```bash
spotifyforge playlist list
```

## CLI Usage

SpotifyForge organises commands into sub-groups: `auth`, `playlist`, `discover`,
`schedule`, and `config`.

```bash
# Authentication
spotifyforge auth login          # Open browser for Spotify OAuth
spotifyforge auth status         # Show current auth state
spotifyforge auth logout         # Remove stored tokens

# Playlists
spotifyforge playlist list                         # List all your playlists
spotifyforge playlist show <playlist_id>           # Show playlist details and tracks
spotifyforge playlist create "Chill Vibes"         # Create a new playlist
spotifyforge playlist create "Private Mix" --private
spotifyforge playlist sync <playlist_id>           # Sync playlist to local cache
spotifyforge playlist deduplicate <playlist_id>    # Remove duplicate tracks
spotifyforge playlist export <playlist_id> -f csv -o tracks.csv

# Discovery
spotifyforge discover top-tracks --time-range short_term --limit 10
spotifyforge discover deep-cuts "Radiohead" --threshold 25
spotifyforge discover genre indie-rock --limit 30
spotifyforge discover time-capsule --time-range long_term

# Scheduling
spotifyforge schedule list
spotifyforge schedule add \
    --name "Weekly sync" \
    --type sync \
    --playlist <playlist_id> \
    --cron "0 8 * * 1"
spotifyforge schedule remove <job_id>
spotifyforge schedule run       # Start the scheduler daemon

# Configuration
spotifyforge config show
spotifyforge config set spotify_client_id <your_id>

# Version
spotifyforge --version
```

## API Usage

### Start the Server

```bash
# Development (auto-reload)
uvicorn spotifyforge.web.app:app --reload --port 8000

# Production
uvicorn spotifyforge.web.app:app --host 0.0.0.0 --port 8000 --workers 1

# Or with Docker
docker compose up --build
```

Interactive API docs are available at `http://localhost:8000/docs` (Swagger UI).

### Key Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health check |
| `GET` | `/api/auth/login` | Get Spotify OAuth URL |
| `GET` | `/api/auth/me` | Current user profile |
| `GET` | `/api/playlists` | List user playlists |
| `POST` | `/api/playlists` | Create a playlist |
| `GET` | `/api/playlists/{id}` | Get playlist details |
| `PUT` | `/api/playlists/{id}` | Update a playlist |
| `POST` | `/api/playlists/{id}/sync` | Sync from Spotify |
| `POST` | `/api/playlists/{id}/deduplicate` | Remove duplicates |
| `POST` | `/api/playlists/{id}/tracks` | Add tracks |
| `DELETE` | `/api/playlists/{id}/tracks` | Remove tracks |
| `GET` | `/api/discover/top-tracks` | User's top tracks |
| `GET` | `/api/discover/top-artists` | User's top artists |
| `GET` | `/api/discover/deep-cuts/{artist_id}` | Artist deep cuts |
| `POST` | `/api/discover/genre-playlist` | Create genre playlist |
| `POST` | `/api/discover/time-capsule` | Create time capsule |
| `GET` | `/api/schedules` | List scheduled jobs |
| `POST` | `/api/schedules` | Create a scheduled job |
| `DELETE` | `/api/schedules/{id}` | Delete a job |
| `PUT` | `/api/schedules/{id}/toggle` | Enable/disable a job |

## Scheduling Examples

SpotifyForge uses APScheduler with standard 5-field cron expressions
(`minute hour day month day_of_week`).

```bash
# Sync a playlist every Monday at 8:00 AM
spotifyforge schedule add \
    --name "Monday sync" \
    --type sync \
    --playlist 37i9dQZF1DXcBWIGoYBM5M \
    --cron "0 8 * * 1"

# Deduplicate a playlist daily at midnight
spotifyforge schedule add \
    --name "Nightly dedup" \
    --type deduplicate \
    --playlist 37i9dQZF1DXcBWIGoYBM5M \
    --cron "0 0 * * *"

# Start the scheduler daemon (foreground)
spotifyforge schedule run
```

## Configuration Reference

All settings are loaded from environment variables (with a `SPOTIFYFORGE_`
prefix) or a `.env` file. See `.env.example` for a complete template.

| Variable | Default | Description |
|----------|---------|-------------|
| `SPOTIFYFORGE_ENVIRONMENT` | `development` | Set to `production` to enforce required settings |
| `SPOTIFYFORGE_SECRET_KEY` | `""` | Encryption key for tokens (required in production) |
| `SPOTIFYFORGE_SPOTIFY_CLIENT_ID` | `""` | Spotify app client ID (required) |
| `SPOTIFYFORGE_SPOTIFY_CLIENT_SECRET` | `""` | Spotify app client secret (required) |
| `SPOTIFYFORGE_SPOTIFY_REDIRECT_URI` | `http://localhost:8000/api/auth/callback` | OAuth redirect URI |
| `SPOTIFYFORGE_DB_PATH` | `~/.spotifyforge/spotifyforge.db` | SQLite database path |
| `SPOTIFYFORGE_DATABASE_URL` | `""` | Database URL; overrides DB_PATH (e.g. `postgresql://...`) |
| `SPOTIFYFORGE_WEB_HOST` | `127.0.0.1` | API server bind address |
| `SPOTIFYFORGE_WEB_PORT` | `8000` | API server port |
| `SPOTIFYFORGE_LOG_LEVEL` | `INFO` | Logging level (DEBUG, INFO, WARNING, ERROR) |
| `SPOTIFYFORGE_SCHEDULER_ENABLED` | `true` | Enable background scheduler |
| `SPOTIFYFORGE_CACHE_TTL_AUDIO_FEATURES` | `0` | Audio features cache (seconds, 0 = indefinite) |
| `SPOTIFYFORGE_CACHE_TTL_TRACK_METADATA` | `604800` | Track metadata cache (7 days) |
| `SPOTIFYFORGE_CACHE_TTL_ARTIST_DATA` | `86400` | Artist data cache (24 hours) |
| `SPOTIFYFORGE_CACHE_TTL_PLAYLIST_CONTENTS` | `3600` | Playlist contents cache (1 hour) |

## Development Setup

```bash
# Clone and install with dev dependencies
git clone https://github.com/your-org/spotifyforge.git
cd spotifyforge
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Copy environment template
cp .env.example .env

# Run linter
ruff check .

# Run type checker
mypy spotifyforge

# Run tests
pytest

# Run tests with coverage
pytest --cov=spotifyforge --cov-report=term-missing
```

## Project Structure

```
spotifyforge/
├── pyproject.toml               # Build config, dependencies, tool settings
├── Dockerfile                   # Multi-stage production container
├── docker-compose.yml           # Local development orchestration
├── .env.example                 # Environment variable template
├── conftest.py                  # Shared pytest fixtures
├── tests/
│   ├── test_auth/               # Authentication tests
│   ├── test_cli/                # CLI command tests
│   ├── test_core/               # Core business logic tests
│   ├── test_db/                 # Database layer tests
│   ├── test_models/             # Model and schema tests
│   └── test_web/                # API endpoint tests
└── spotifyforge/
    ├── __init__.py              # Package version
    ├── config.py                # Pydantic settings (env-driven)
    ├── py.typed                 # PEP 561 typed package marker
    ├── auth/
    │   └── oauth.py             # Spotify OAuth (PKCE) + token management
    ├── cli/
    │   └── app.py               # Typer CLI with Rich output
    ├── core/
    │   ├── playlist_manager.py  # Playlist CRUD, sync, dedup, export
    │   ├── discovery.py         # Top tracks, deep cuts, genre, time capsule
    │   └── scheduler.py         # APScheduler service + job dispatch
    ├── db/
    │   ├── engine.py            # SQLite engine, session helpers, init_db()
    │   └── repositories.py      # Data access layer
    ├── models/
    │   └── models.py            # SQLModel tables + Pydantic schemas
    └── web/
        ├── app.py               # FastAPI factory, lifespan, dependencies
        └── routes.py            # API routers (auth, playlists, discovery, schedules)
```

## Contributing

1. Fork the repository and create a feature branch.
2. Install dev dependencies: `pip install -e ".[dev]"`
3. Make your changes and ensure linting passes: `ruff check .`
4. Verify types: `mypy spotifyforge`
5. Add or update tests and confirm they pass: `pytest`
6. Submit a pull request with a clear description of your changes.

## License

This project is licensed under the MIT License. See the [pyproject.toml](pyproject.toml) for details.
