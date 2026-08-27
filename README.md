# SpotifyForge

I wrote this to turn my 3,001 liked songs into genre playlists. Spotify tags
genres on artists, not tracks, so the first pass unions every credited
artist's genres onto each liked track; that library carried 421 distinct
genres, and letting a track join every genre it carries is what turned ~50
obvious playlists into 221 niche ones, each sequenced so it flows. Running it
against the real account is also how the interesting bugs surfaced: the very
first forged playlist held "Lamento Boliviano" twice under two track IDs
(album cut and remaster), and collapsing edition variants removed 377
duplicates, 12.5% of the library.

The tests are the part I would point to. `tests/fake_spotify.py` is an
in-memory Spotify Web API served through `httpx.MockTransport`, so the real
tekore client builds every request and parses every response; nothing in
`spotifyforge` is mocked. The fake rejects any bearer token it did not issue,
which is what proves OAuth exchange, token refresh and storage work end to
end. The 465 tests run in about 15 seconds with no network.
`tests/test_core/test_curation.py` records the decisions that came out of
real data: an ASCII-only title normaliser flattened every CASIOPEA title to
`""` and merged the whole catalogue into one track, so CJK titles are kept
distinct while `四面道歌 - 2019 Remastering` still collapses; `(Reprise)` and
`(Part 2)` are different songs, `(Remastered)` is not; and `--exclusive`
files a track under its rarest genre rather than the umbrella one.

Spotify withdrew `GET /v1/audio-features` for new apps, so tempo and key come
from Deezer and AcousticBrainz, looked up by ISRC (`core/audio_features.py`).
On my library that covered key for 52% of recordings, enough for 97 of the
221 playlists to be sequenced by Camelot-wheel key distance; the rest fall
back to a popularity arc. Same-artist separation always outranks harmony.

## Install

Requires Python 3.11+ and a Spotify app from
<https://developer.spotify.com/dashboard>.

```bash
git clone https://github.com/gr8monk3ys/spotify.git
cd spotify
uv sync --all-extras          # or: pip install -e ".[dev]"
cp .env.example .env          # set SPOTIFYFORGE_SPOTIFY_CLIENT_ID / _SECRET
uv run spotifyforge auth login
```

`auth login` opens the browser; paste the redirect URL back into the prompt.
Tokens go to the OS keyring.

## Use

```bash
spotifyforge curate plan                 # preview the catalogue; writes nothing
spotifyforge curate forge --limit 25     # create the next 25; re-run to continue
spotifyforge curate features             # fetch tempo/key by ISRC, cached on disk
spotifyforge curate reflow --harmonic    # re-sequence in place, URLs preserved
spotifyforge curate curators             # people whose public playlists overlap yours

spotifyforge playlist list
spotifyforge playlist deduplicate <playlist_id>
spotifyforge playlist export <playlist_id> -f csv -o tracks.csv
spotifyforge discover deep-cuts <artist_id> --threshold 25
spotifyforge schedule add --name "Nightly dedup" --type deduplicate \
    --playlist <playlist_id> --cron "0 0 * * *"

# Other platforms (shared files in MUSIC_DIR, default ~/.music)
spotifyforge export library                         # Write ~/.music/music-library.json for discogs + rym
spotifyforge library save --from-discogs            # Dry run: Discogs collection -> Spotify saved albums
spotifyforge library save --from-discogs --apply    # Save the unambiguous matches (needs a re-login once)
spotifyforge schedule run
```

Groups: `auth`, `playlist`, `discover`, `curate`, `export`, `library`, `schedule`,
`config`;
`spotifyforge <group> --help` lists the rest. `curate curators` is read-only
on purpose: auto-following for follow-backs is the engagement pattern
Spotify's rules prohibit, and `REQUIRED_SCOPES` deliberately omits
`user-follow-modify`.

There is also a FastAPI server (`uv run uvicorn spotifyforge.web.app:app`,
Swagger at `/docs`) over the same core services, and `docker compose up`
runs it with the scheduler.

## Layout

```
spotifyforge/
├── cli/        Typer app; one module per command group, helpers in _shared.py
├── core/       curation, audio_features, discovery, playlist_manager, scheduler
├── auth/       Spotify OAuth (PKCE) and keyring token store
├── db/, models/  SQLite via SQLModel; alembic/ holds migrations
└── web/        FastAPI app and routes
tests/fake_spotify.py   the in-memory Spotify API the whole suite runs against
```

Settings are `SPOTIFYFORGE_*` environment variables or `.env`; see
`.env.example` for the full list. `SPOTIFYFORGE_MUSIC_DIR` (or `MUSIC_DIR`,
default `~/.music`) is the shared directory `export library` writes
`music-library.json` to, read by the discogs and rym repos.

## Develop

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy .
uv run pytest
```

MIT licensed.
