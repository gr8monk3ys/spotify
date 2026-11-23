# JSON sidecar files instead of a database

SpotifyForge keeps no database. Tokens live in the OS keyring and everything else (feature cache, pinned expansions, listening log, stats snapshots) is a small JSON or JSONL file under `~/.spotifyforge`, with the shared exports in the music dir. Earlier versions carried a database path and web settings; they were dropped, and `Settings` ignores those stale `.env` keys. The data is a local, single-user cache of public lookups plus one append-only log, so files keep it inspectable and the CLI free of migrations, at the cost of no queries across them.
