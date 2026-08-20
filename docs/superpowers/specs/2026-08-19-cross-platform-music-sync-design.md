# Cross-platform music sync: Spotify → Discogs → RateYourMusic

**Status:** approved design, not yet implemented
**Date:** 2026-08-19
**Repos touched:** `~/code/spotify` (source), `~/code/discogs` (consumer), `~/code/rym` (new consumer)

## Problem

Three accounts describe the same taste and none of them know about each
other.

`spotify` holds the richest picture: 2,624 unique liked tracks across 421
artist genres, 304 curated playlists, an ISRC→MusicBrainz cache 3,490
entries deep, and tempo/key readings for 1,928/1,478 of those recordings.
That knowledge cost hours of API walking and is currently trapped in one
repo.

`discogs` has a working recommender whose seed set is drawn entirely from
the local library cache — **12 collection items and 89 wantlist items**.
It recommends records based on a hundred-item picture of a listener whose
real library is twenty-six times larger.

`rym` does not exist. The RateYourMusic account is maintained by hand,
and there is no local record of what has been rated or what is missing.

## Goal

Make the Spotify library the source of truth for taste, and let both other
platforms consume it — so Discogs recommends from what is actually
listened to, and RYM's ratings stay current without hand-auditing.

Non-goal: a general-purpose music metadata framework. Each repo stays
independently useful and independently testable; the only coupling is one
versioned JSON file.

## Constraints that shaped this design

**RateYourMusic prohibits automated access.** Their `robots.txt` opens
with "Sonemic, Inc. prohibits any kind of automated means, (e.g. crawling,
scraping, etc) of access to the service without express permission" and
blocks `ClaudeBot`, `anthropic-ai`, `GPTBot`, `Scrapy`, and `User-agent:
*`. There is no public API — the Sonemic API has been "in development"
for years, with only a register-interest form.

This is categorically different from the `goodreads` and `letterboxd`
repos, which write through a logged-in browser against a *grey* ToS. Here
the prohibition is explicit and names the tooling. The `rym` repo
therefore has **no browser, no scraper, and no write path** — it consumes
the official "Export your data" CSV, which is a first-party, user-initiated
feature, and it produces a queue for the human to act on.

**Discogs has real API limits and no local handling of them.** There is no
429 retry, no backoff, no `Retry-After` respect anywhere in `src/` — only
a daily call-budget counter (`_api_call_counts`, default 1500). Any bulk
matching must therefore minimise Discogs calls rather than assume the
client will pace itself.

**Discogs has no release→main-artist relation.** `release_credits` is
populated from Discogs `extraartists` — producers, engineers,
instrumentalists — *not* the billed artist. `select_seeds()` counts
occurrences in that table, so "artist X made album Y" has nowhere to live
in the current schema.

**`resolve_artist_name()` is subtly broken.** In
`src/discogs/api/search.py` the `return None` sits *inside* the result
loop, so only the first search hit is ever considered and a near-miss on
hit #1 discards hits #2..n.

**`CurationTrack` drops album identity.** The curation model carries
`id, uri, name, artist_ids, artist_names, release_year, popularity, isrc,
genres` — no album. The local SQLite `albums`/`artists`/`tracks` tables
exist but are empty (0 rows); the pipeline runs API → sidecar JSON, and
only `playlists` is persisted. There is no album-level dataset today.

## Key insight: match through MusicBrainz, not Discogs search

The hard problem is identity: one album is a Spotify album id, a Discogs
release id, and an RYM slug, with no shared key.

MusicBrainz is the bridge, keyed by something this repo has already
collected for the whole library: the ISRC. MusicBrainz stores Discogs
URLs as native `url` relationships on releases and release-groups.

So the join is:

```
Spotify track → ISRC → MB recording → MB release-group → Discogs release id
              (exported)   (25 per call, free)  (1 req/sec)  (url relationship)
```

**Correction, found while implementing Phase 0.** An earlier draft claimed
the MusicBrainz ids were already cached and could simply be exported.
They are not. `FeatureCache` entries hold `{sources, tempo, key, mode}`
only — `AcousticBrainzProvider._resolve()` looks the ids up, takes the
readings, and discards the mapping. What is banked is the ISRC set and
the tempo/key data. Consumers therefore re-resolve, which MusicBrainz
answers 25 ISRCs per search call and each consumer caches on its own
side: a real cost, but small and paid once per repo.

This is still an *exact* match and still costs zero Discogs API budget.
Fuzzy artist+title+year search against Discogs becomes
the fallback for release-groups with no Discogs relationship — not the
primary path.

## Architecture

```
spotify repo                    interchange                consumers
────────────                    ───────────                ─────────
liked songs ──┐
audio_features│
expansions.json─→ spotifyforge  music-library.json ──┬──→ discogs import-spotify
              │   export                             │      └→ seeds, wantlist
playlists ────┘                 (versioned, one file)└──→ rym import-spotify
                                                            └→ rating queue
```

One versioned file is the entire contract. Each consumer owns its own
database, its own matching cache, and its own tests; neither imports the
other's code.

---

## Phase 0 — `spotifyforge export` (spotify repo)

**New command:** `spotifyforge export library [--out PATH]`, defaulting to
`~/.spotifyforge/music-library.json`.

**Why it needs new code:** album identity is dropped at
`to_curation_track()`. The saved-tracks API response *does* carry
`track.album` (id, name, release_date, total_tracks), so the exporter
walks saved tracks directly rather than reusing the curation model, and
rolls up per album.

**Output schema (`music-library/1`):**

```json
{
  "schema": "music-library/1",
  "generated_at": "2026-08-19T00:00:00Z",
  "source": {"platform": "spotify", "user": "gr8monk3ys"},
  "albums": [
    {
      "spotify_album_id": "...",
      "title": "...",
      "artists": [{"name": "...", "spotify_id": "..."}],
      "year": 1997,
      "liked_track_count": 7,
      "total_tracks": 12,
      "affinity": 0.58,
      "isrcs": ["..."],
      "genres": ["..."],
      "tempo_known": 5,
      "key_known": 3
    }
  ],
  "discoveries": [
    {"genre": "dungeon synth", "tracks": [{"name": "...", "artists": ["..."], "isrc": "..."}]}
  ]
}
```

`affinity` (liked ÷ total tracks) is the signal both consumers rank by: an
album with 9 of 10 tracks liked is a record you want; one with a single
liked track is a playlist add.

`isrcs` is what makes the consumers' matching possible: it is the key
MusicBrainz accepts, and the only cross-platform identifier this library
actually owns.

`discoveries` carries the 801 pinned unheard tracks so consumers can
distinguish *listened* from *found-but-unheard*, which matters: an unheard
track should never become a wantlist push or a rating-queue entry.

**Testing:** exporter unit tests against the existing `FakeSpotify`
backend; a golden-file test pinning the schema, since two repos depend on
its shape.

---

## Phase 1a — Discogs artist seeding (first PR)

The cheap change with the largest effect.

**New:** `discogs import-spotify [--file PATH]`

1. Read the interchange file.
2. Resolve each distinct Spotify artist → Discogs artist id via
   `resolve_artist_name()`, **fixing the `return None`-inside-the-loop
   bug** so all hits are scored, not just the first.
3. Persist to a new table:

```sql
CREATE TABLE IF NOT EXISTS spotify_artists (
    spotify_artist_id TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    discogs_artist_id INTEGER,           -- NULL = unresolved
    liked_track_count INTEGER NOT NULL,
    match_method      TEXT NOT NULL,     -- 'search' | 'manual' | 'unresolved'
    resolved_at       TEXT NOT NULL
);
```

Resolution is cached permanently — re-running costs nothing for artists
already resolved, which keeps the run well inside the daily budget.

**Extend `select_seeds()`** with a Spotify source. Today:

```python
Mode = Literal["collection", "wantlist", "both"]
```

becomes `Literal["collection", "wantlist", "spotify", "all"]`. Spotify
seeds carry `sources=("spotify",)` and a weight scaled by
`liked_track_count` against the same `[0.1, 1.0]` range the existing
weights use, so scoring needs no change.

This deliberately **does not** require album→release matching: seeds are
artists, and the release→main-artist gap is irrelevant to them. It is
also why this phase ships alone.

**Testing:** unit tests for the fixed searcher (multi-hit scoring),
seed-weight tests over a real tmp SQLite store, and a CLI test through
`CliRunner`. Coverage gate is 90%, mypy strict.

---

## Phase 1b — Album matching + wantlist push (second PR)

**Prerequisite, landed first in this PR:** 429/5xx backoff in
`DiscogsClient.call()`. Bulk matching is the first workload that can
plausibly hit the ceiling, and the client currently has no retry at all.

**Matching ladder**, cheapest first:

1. MusicBrainz recording id → release-group → Discogs `url` relationship
   (free, 1 req/sec, exact). Cached locally so it is paid once.
2. Discogs release search by artist + title + year (new
   `search_release()` — no such function exists today).
3. Unmatched: recorded with a reason, never silently dropped.

**New tables:**

```sql
CREATE TABLE IF NOT EXISTS spotify_albums (
    spotify_album_id  TEXT PRIMARY KEY,
    title             TEXT NOT NULL,
    year              INTEGER,
    liked_track_count INTEGER NOT NULL,
    total_tracks      INTEGER,
    affinity          REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS spotify_release_matches (
    spotify_album_id TEXT PRIMARY KEY,
    release_id       INTEGER,            -- NULL = unmatched
    master_id        INTEGER,
    confidence       TEXT NOT NULL,      -- 'exact' | 'probable' | 'weak' | 'none'
    method           TEXT NOT NULL,      -- 'musicbrainz' | 'search'
    matched_at       TEXT NOT NULL
);
```

**Wantlist push** reuses `push_to_wantlist()` unchanged. Rules:

- Dry-run by default, consistent with `run_recommend()`.
- Only `confidence = 'exact'` is ever eligible.
- Only albums above an affinity floor (default 0.5 — half the record is
  already liked).
- Never anything from `discoveries` (unheard).
- Never anything already in the collection or wantlist.
- Each batch recorded in `recommendation_history` so `undo` works exactly
  as it does for recommendations.

---

## Phase 2 — the `rym` repo (new)

Skeleton copied from `goodreads`, which is the tighter of the two
references: real package, one Typer CLI, `pydantic-settings`, strict
mypy, coverage gate.

```
~/code/rym/
  src/rym/
    config.py          pydantic-settings, env_prefix="RYM_"
    ingest/csv.py      official export CSV → records (tolerant parser)
    store/{db,schema.sql,repository}.py   SQLite at ~/.rym/rym.db
    spotify/import.py  read music-library.json
    match.py           rated albums ↔ Spotify albums (MBID, then artist+title)
    queue.py           unrated-but-listened → prioritised queue
    dashboard.py       static HTML, no server (goodreads pattern)
    stats.py           ratings × genre/tempo/key/decade
    cli.py             Typer
  tests/
  docs/superpowers/specs/
  .github/workflows/ci.yml
  CLAUDE.md  README.md  SECURITY.md  .env.example
```

**Commands:**

| command | does |
|---|---|
| `rym ingest <csv>` | official export → SQLite |
| `rym import-spotify` | read the interchange file |
| `rym queue` | albums listened-to but unrated → ranked list |
| `rym dashboard` | that queue as static HTML with RYM search links |
| `rym stats` | rating distribution against genre, tempo, key, decade |

**The queue is the point.** Rank by `affinity × liked_track_count`,
exclude anything already rated, exclude `discoveries`, and emit a
click-through link per row. The human rates on RYM; nothing is automated.

**Dependencies:** `typer` and `pydantic-settings` only. No Playwright, no
httpx-to-RYM, no scraping library. The absence is a design feature and is
documented in the README so it does not get "fixed" later.

**Unknown to resolve at implementation time:** the exact column names of
RYM's export CSV. The parser is written tolerantly (case-insensitive
header matching, unknown columns preserved) and confirmed against the real
file. This requires the user to click "Export your data" on their profile;
it cannot be automated, by design.

---

## Sequencing

1. **Phase 0** — exporter. Unblocks everything; nothing else can start.
2. **Phase 1a** — Discogs artist seeds. Immediate payoff: `recommend`
   stops guessing from 101 records.
3. **Phase 1b** — album matching, backoff, wantlist push.
4. **Phase 2** — the `rym` repo.

Each phase is one PR against its own repo, verified end-to-end on real
data before opening (per `/go`).

## Risks

| risk | mitigation |
|---|---|
| MusicBrainz has no Discogs link for a release-group | Fall back to Discogs search; record `confidence='weak'` rather than guessing |
| Discogs 429 during bulk match | Backoff lands *before* Phase 1b touches the API; daily budget is the second brake |
| Artist name collisions ("Nirvana", "Prince") | Score all hits (post-bugfix), prefer exact string match, leave ambiguous ones `unresolved` rather than wrong |
| RYM changes its export format | Tolerant parser + a test over a committed sample row |
| Interchange schema drift breaks a consumer | Version string in the file; consumers assert on it; golden-file test in the producer |
| Wantlist pollution from bad matches | `exact`-only, affinity floor, dry-run default, `undo` support |

## What this explicitly does not do

- No scraping of RateYourMusic, ever, and no automated writes to it.
- No writes to the Discogs *collection* — collection means "I own this
  physical copy", which Spotify cannot know.
- No new LLM spend in Phase 0/1a; the existing Claude budget controls
  cover the recommender path unchanged.
