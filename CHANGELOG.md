# Changelog

## Unreleased

### Features

* **mcp:** `spotifyforge mcp` serves the curation core to MCP clients over stdio
* **history:** `history sync` appends recent plays to a local log, `history show`
  and the `listening_history` MCP tool summarise it
* **auth:** concurrent processes take a lock before refreshing the shared token
* **curate:** `curate genres` fills in genres for artists Spotify has not tagged,
  from MusicBrainz tags looked up by ISRC
* **library:** `library soundtracks` builds a playlist from the soundtracks of the
  films rated highest in the Letterboxd export
* **history:** `history playlists` and the `playlist_usage_report` MCP tool show
  which owned playlists are actually played
* **export:** `export page` writes the library as one self-contained HTML page
* **history:** `history likes` and the `liked_vs_played_report` MCP tool list songs
  played but not liked, and liked songs in no top list and not in the log;
  `--playlist` writes the oldest `--limit` (default 100) of the second list to a new
  private playlist, and dates are shown on the local clock, as on the Wrapped page
* **export:** `export wrapped` writes your own Wrapped from the listening log as one
  self-contained local HTML page; `history sync` now records possible gaps beside the log
* **export:** the page adds genres against the year each song was liked, with
  bulk-import days set aside
* **library:** `library records` and the `records_vs_streams` MCP tool join the
  Discogs collection and wantlist to liked songs and plays: wanted and already
  loved, owned but not streamed, loved but on neither shelf
* **curate:** `curate features` estimates tempo from Deezer previews for tracks no
  database has a BPM for (optional `tempo` extra); `--retry-misses` re-asks Deezer
  about misses its quota errors left in the cache
* **discover:** `discover shows` and the `upcoming_shows` MCP tool list concerts
  near home by the artists you play, from Ticketmaster's Discovery API

### Removed

* the FastAPI server, the APScheduler service and `schedule` commands, the
  SQLite database with its Alembic migrations, and the Docker files. Nothing
  in the CLI read the database back; tokens were already in the OS keyring.
  `playlist sync` (which only filled that database) is gone with it.
* settings `SECRET_KEY`, `ENVIRONMENT`, `DB_PATH`, `DATABASE_URL`, `WEB_HOST`,
  `WEB_PORT` and `SCHEDULER_ENABLED`. They are ignored if still present in
  `.env`. `DATA_DIR` (default `~/.spotifyforge`) replaces the parent of
  `DB_PATH` as the home of the JSON sidecars.

### Bug Fixes

* **playlist:** `export --format csv -o FILE` wrote a blank line between rows on Windows

## 0.1.0 (2026-08-19)


### Features

* **auth:** implement Spotify OAuth flow, config, and token encryption ([ed2e948](https://github.com/gr8monk3ys/spotify/commit/ed2e948085e09393bf894bbcc4af0a7693050ad8))
* **cli:** add Typer command-line interface ([aad81a5](https://github.com/gr8monk3ys/spotify/commit/aad81a5f89f1082ca2206c7170fb9f448643d3b6))
* **core:** add track discovery, playlist manager, and scheduler ([d4f035a](https://github.com/gr8monk3ys/spotify/commit/d4f035a2fb2a392473f19ef4fb7e0b54d11505b7))
* **curate:** build a catalogue of niche playlists from liked songs ([#28](https://github.com/gr8monk3ys/spotify/issues/28)) ([488d841](https://github.com/gr8monk3ys/spotify/commit/488d8414a9eac8bebfd83c4e627267c5df938788))
* **curate:** expand — grow thin playlists with unheard same-niche tracks ([#32](https://github.com/gr8monk3ys/spotify/issues/32)) ([dd70b89](https://github.com/gr8monk3ys/spotify/commit/dd70b890c385a901e65b860e59a58438345dab62))
* **curate:** human, artist-led playlist descriptions + describe command ([#30](https://github.com/gr8monk3ys/spotify/issues/30)) ([e74aef9](https://github.com/gr8monk3ys/spotify/commit/e74aef99d71dde383f78779c66dc405d6e98ea0a))
* **curate:** photo cover art from Pexels, matched to each playlist's vibe ([#35](https://github.com/gr8monk3ys/spotify/issues/35)) ([7628412](https://github.com/gr8monk3ys/spotify/commit/762841271a7e67b7a6520a120b243ae95cab9fdd))
* **curate:** stats command — snapshot follower counts, report growth ([#31](https://github.com/gr8monk3ys/spotify/issues/31)) ([db7006c](https://github.com/gr8monk3ys/spotify/commit/db7006c1a7d38b1776788f321f8e46e46ad22de9))
* **db:** add Alembic migrations with initial schema ([b8940f3](https://github.com/gr8monk3ys/spotify/commit/b8940f30883e0014a8a48bbe14a1393a21712cfe))
* **db:** add engine, session, and repositories ([1464683](https://github.com/gr8monk3ys/spotify/commit/14646834172d6458152beee6b1403052b94b3eb8))
* **models:** define core SQLAlchemy data models ([938cac7](https://github.com/gr8monk3ys/spotify/commit/938cac7fda2d04967f4c6b9125c181f101047a68))
* **web:** add FastAPI app, routes, and dependencies ([697aeba](https://github.com/gr8monk3ys/spotify/commit/697aeba98b334b07137216f78a7e7a5d3b795329))


### Bug Fixes

* **ci:** call the OSV reusable workflow instead of a nonexistent action ([#20](https://github.com/gr8monk3ys/spotify/issues/20)) ([8834785](https://github.com/gr8monk3ys/spotify/commit/8834785d2ab7821d2222bac1fbf8766e6e6e5d32))
* **ci:** declare only the languages this repo contains ([#19](https://github.com/gr8monk3ys/spotify/issues/19)) ([4882f75](https://github.com/gr8monk3ys/spotify/commit/4882f75a6b5883a6c4ad26a1df80ba5531d21bc4))
* **covers:** abstract art only, no people, account-unique photos ([#36](https://github.com/gr8monk3ys/spotify/issues/36)) ([f398b52](https://github.com/gr8monk3ys/spotify/commit/f398b52f7512709871b4f865e038616515fa8c49))
* **curate:** features covers pinned expansion tracks too ([#33](https://github.com/gr8monk3ys/spotify/issues/33)) ([2d5f057](https://github.com/gr8monk3ys/spotify/commit/2d5f057eaecbf68ebb441ef89c34ed7ea8fcb0c7))
* make SpotifyForge fully functional, proven by execution ([#27](https://github.com/gr8monk3ys/spotify/issues/27)) ([b2099bc](https://github.com/gr8monk3ys/spotify/commit/b2099bc8a5ba11be670e9609adb9a3235f307a04))


### Performance Improvements

* **curators:** inspect candidates concurrently and read each once ([#29](https://github.com/gr8monk3ys/spotify/issues/29)) ([f3d25fa](https://github.com/gr8monk3ys/spotify/commit/f3d25fa18cd1a61b2f11e74c4da2ab585dc2c3b7))


### Documentation

* add contributing guide and code of conduct ([cabc6bd](https://github.com/gr8monk3ys/spotify/commit/cabc6bdd885daf5d53dfb425e4a35440dd45082a))
* add hero image to README ([#18](https://github.com/gr8monk3ys/spotify/issues/18)) ([49c1a12](https://github.com/gr8monk3ys/spotify/commit/49c1a127567a17a47a57b8933063fb736a835910))
* add SpotifyForge PRD research and project README ([0507d87](https://github.com/gr8monk3ys/spotify/commit/0507d87119879302d5f973c4db877b03d4d2b882))
