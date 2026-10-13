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
end. The 516 tests run in under a minute with no network.
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

# Other platforms (shared files in MUSIC_DIR, default ~/.music)
spotifyforge export library                         # Write ~/.music/music-library.json for discogs
spotifyforge library save --from-discogs            # Dry run: Discogs collection -> Spotify saved albums
spotifyforge library save --from-discogs --apply    # Save the unambiguous matches (needs a re-login once)
```

Groups: `auth`, `playlist`, `discover`, `curate`, `export`, `library`, `config`;
`spotifyforge <group> --help` lists the rest. `curate curators` is read-only
on purpose: auto-following listeners for follow-backs is the engagement
pattern Spotify's rules prohibit. `curate follow-artists` does follow, but
only artists your own liked songs already point at, and it is a dry run
until you pass `--apply`.

There is no scheduler. To run something on a timer, call the CLI from cron or
Task Scheduler; the bulk `curate` commands skip work already done.

## MCP server

`spotifyforge mcp` serves the same core to an MCP client over stdio, using the
login `auth login` already stored. It exposes what a generic Spotify server
cannot, because it depends on how this package reads your library:

| Tool | What it does |
|---|---|
| `liked_genres` | Genres across your liked songs, with counts |
| `search_liked` | Liked songs by genre, artist, title or year, with BPM and key where known |
| `create_playlist` | Create a private playlist from chosen tracks, sequenced by key and tempo |
| `list_playlists`, `get_playlist` | Read your playlists |
| `deduplicate_playlist` | Remove repeated tracks from one of your own playlists |
| `plan_catalogue_preview` | The genre catalogue `curate plan` would show |
| `audience_stats` | Follower counts for the account and each owned playlist |
| `top_tracks`, `recently_played` | Listening history |

So "make me a 25-track late-night jazz fusion playlist from my liked songs"
becomes three calls: learn the genres, pick the tracks, create it sequenced.
Playlists are always created private, write tools only touch playlists you
own, and every id is checked to be a Spotify id before it reaches a request:
playlist and track text comes from other people, so a model reading it should
not be one step away from publishing anything. The bulk writers
(`curate forge`, `reflow`, `retire`) are not exposed: they are only correct
with the sizes a catalogue was forged with, which belongs in a script.

Claude Code picks the server up from the `.mcp.json` in this repo. For any
other client, the command is `uv run spotifyforge mcp` with this directory as
the working directory (that is where `.env` is read from).

## Layout

```
spotifyforge/
├── cli/           Typer app; one module per command group, helpers in _shared.py
├── core/          curation, audio_features, discovery, playlist_manager, stats
├── auth/          Spotify OAuth, keyring token store, the logged-in session
└── mcp_server.py  the MCP tools, over the same core as the CLI
tests/fake_spotify.py   the in-memory Spotify API the whole suite runs against
```

There is no database. Tokens live in the OS keyring; everything else is a
handful of JSON files in `~/.spotifyforge`.

Settings are `SPOTIFYFORGE_*` environment variables or `.env`; see
`.env.example` for the full list. `SPOTIFYFORGE_MUSIC_DIR` (or `MUSIC_DIR`,
default `~/.music`) is the shared directory `export library` writes
`music-library.json` to, read by the discogs repo.

## Develop

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy .
uv run pytest
```

MIT licensed.
