# SpotifyForge PRD Research: The Complete Technical and Market Landscape

**SpotifyForge faces a transformed Spotify API landscape but targets a genuine market gap.** Spotify has dramatically restricted its Web API across three waves of changes (November 2024, May 2025, February 2026), deprecating the Audio Features, Recommendations, and Audio Analysis endpoints that would have been core to intelligent playlist automation. However, playlist CRUD operations remain fully functional, third-party alternatives exist for deprecated features, and **no dominant all-in-one curator automation platform exists today** — making this a viable but architecturally constrained opportunity. The platform should be built around a shared Python core with Typer CLI and FastAPI web layers, using Spotipy or Tekore for API access, SQLite for caching, and APScheduler for automation scheduling.

---

## Spotify's API Has Been Gutted — But Playlist Operations Survive

The most critical finding for SpotifyForge is the scope of Spotify's API restrictions. Three waves of changes since late 2024 have fundamentally altered what's possible for third-party developers.

**November 27, 2024** deprecated the Recommendations, Audio Features, Audio Analysis, Related Artists, and Featured/Category Playlists endpoints for all new apps and those still in development mode. **May 15, 2025** restricted extended quota access to legally registered businesses with **250,000+ monthly active users**. Most recently, **February 2026** reduced dev mode from 25 to 5 test users, required Premium accounts, removed batch endpoints, limited search results to 10 per page in dev mode, and stripped response fields like `popularity` and `followers` from artist objects.

What survives is the core playlist infrastructure. SpotifyForge can still:

- Create playlists (`POST /me/playlists`)
- Add/remove/reorder tracks (100 items per request)
- Read a user's saved library and top artists/tracks
- Access recently played history (last 50 tracks)
- Control playback
- Upload custom playlist cover images (Base64 JPEG, max 256KB)

Playlists support up to **10,000 items** and return `snapshot_id` for concurrency control. The Search endpoint remains available with filters including `year:`, `genre:`, `artist:`, `tag:new`, and `tag:hipster` — the last two being particularly useful for discovery automation.

### Third-Party Alternatives for Deprecated Endpoints

For the deprecated audio analysis capabilities, viable third-party alternatives include:

- **SoundNet Track Analysis API** on RapidAPI — provides danceability, energy, tempo, valence, and key
- **Cyanite.ai** — AI-based mood and emotion analysis
- **GetSongBPM API** — tempo data
- **ListenBrainz Labs API** — open-source recommendations and similar-artist discovery

### Required OAuth Scopes

The OAuth scopes needed for full playlist automation are:

- `playlist-modify-public`
- `playlist-modify-private`
- `playlist-read-private`
- `playlist-read-collaborative`
- `user-library-read`
- `user-top-read`
- `user-read-recently-played`
- `ugc-image-upload`

### Rate Limits

Rate limits operate on a rolling 30-second window per app. Spotify does not publish exact numbers, but community guidance suggests **~20 requests/second** is safe in development mode. The `429 Too Many Requests` response includes a `Retry-After` header.

### Terms of Service

Spotify's Terms of Service explicitly **permit** building a playlist manager app and charging for it, but prohibit:

- Using Spotify data to train ML/AI models
- Creating products that replicate core Spotify UX
- Making "excessive service calls not strictly required"

---

## The Python Ecosystem Offers Strong Building Blocks

### Spotipy (v2.25.2)

~5,373 GitHub stars, MIT license. Remains the de facto Python wrapper for the Spotify Web API. It covers all endpoints, handles three auth flows, includes built-in retry mechanisms with configurable `retries`, `backoff_factor`, and `status_forcelist` parameters, and manages token refresh automatically through its auth managers. Its main limitations are the lack of native async support and no built-in auto-pagination — developers must manually loop with offset/limit parameters.

### Tekore (v6.0.0)

The superior technical choice for production systems. Created by the same maintainer as Spotipy (felix-hilden), it offers:

- Native **async/await support**
- Self-refreshing tokens
- Typed response models with serialization
- Built-in **auto-pagination** via `spotify.all_pages()` and `spotify.all_items()`
- Composable `RetryingSender` and `CachingSender`
- `chunked_on=True` for auto-chunking large list operations

**Tekore is the recommended library for SpotifyForge's core**, with Spotipy as a fallback given its larger community and tutorial ecosystem.

### Existing CLI Tools in the Ecosystem

- **spotify-tui** (~19,000 stars) — effectively dead since 2021, succeeded by **spotatui** (December 2025 fork)
- **spotifyd** (~10,100 stars, v0.4.1) — actively maintained lightweight Spotify Connect daemon
- **spotify_player** — most feature-rich terminal client, aiming for full parity with the official app
- **spotDL** (~23,900 stars) — most popular Spotify download/sync tool
- **Exportify** — exists in multiple versions for CSV export

### MCP Servers

Multiple **MCP (Model Context Protocol) servers** for Spotify already exist, most notably **varunneal/spotify-mcp** (Python/Spotipy-based) offering playback control, search, queue management, and playlist operations through Claude or Cursor. This pattern validates the AI-assistant integration angle for SpotifyForge.

### No-Code Automation Platforms

- **n8n** — best price-performance with 15+ native Spotify operations and pre-built GPT-4 workflow templates at ~$50/month for 100K tasks
- **Zapier** — 5 native triggers and 10+ actions
- **Make.com** — most visual workflow builder

---

## Creative Playlist Automation Has a Deep Feature Space

The most compelling automation ideas fall into categories that can be implemented despite API restrictions, plus advanced features requiring third-party data enrichment.

### Audio Feature-Based Curation

Remains the highest-value feature category, even though Spotify's native endpoint is deprecated. By sourcing audio data from third-party APIs, SpotifyForge can auto-generate playlists by:

- **Mood** — using valence × energy as a 2D mood space: high valence + high energy = euphoric, low valence + low energy = melancholic
- **Activity** — workout playlists structured as warm-up at **100–120 BPM**, peak intensity at **130–150 BPM**, cooldown at **60–90 BPM**. Research shows **120–140 BPM** combined with moderate-intensity exercise optimally improves mood
- **DJ-style flow** — using **Camelot Wheel key matching** where adjacent keys on the wheel blend seamlessly

### Deep Cuts Discovery

Leverages Spotify's track popularity score (0–100) to find lesser-known tracks by favorite artists. The algorithm fetches a user's top artists, retrieves all their tracks, and filters by popularity below 30–40 while excluding known hits. The open-source **SoulSync** project already implements this pattern with a mix of "20 popular + 20 mid-tier + 10 deep cuts" from a 1,000+ track discovery pool.

### Genre Taxonomy Automation

Can draw on the **Every Noise at Once** project's catalog of **6,291 named micro-genres** (including 56 varieties of reggae, 202 kinds of folk, and 230 types of hip hop). Although the site is frozen since Glenn McDonald's layoff from Spotify in December 2023, it remains an invaluable reference taxonomy. Artist-level genre data is still available via the API's `GET /artists/{id}` genres array. Regional micro-genre playlists (Peruvian chicha, Japanese city pop, Turkish psych rock) represent an underserved niche.

### Time-Capsule Playlists

Uses the `GET /me/top/{type}` endpoint with `time_range` parameters:

- `short_term` = ~4 weeks
- `medium_term` = ~6 months
- `long_term` = several years

Auto-generates monthly listening snapshots. **Seasonal auto-rotation** uses date-based triggers combined with mood filters — SoulSync already implements automated Halloween, Christmas, Valentine's, and seasonal playlists.

### External Data Cross-Referencing

Dramatically expands curation capabilities:

| Source | Cost | Use Case |
|--------|------|----------|
| **Last.fm API** | Free, active | Scrobble history for "forgotten favorites," community genre tags, similar artist discovery |
| **MusicBrainz** | Free, open-source | 12 core resources for metadata enrichment — credits, release dates, labels, ISRC matching |
| **Songkick/Bandsintown APIs** | Free/paid tiers | Concert calendar data for auto-generating "upcoming show prep" playlists |
| **Pitchfork** | Scrapable (no API) | "Critically acclaimed" playlists from albums rated 8.0+ or tagged "Best New Music" |
| **RateYourMusic** | Community scrapers | Detailed genre taxonomy more nuanced than Spotify's (Cloudflare protection is aggressive) |
| **Genius API** | Free tier available | Lyrics data for thematic playlist curation |

### AI-Powered Features

Represent the frontier:

- **Natural language playlist creation** — Spotify's own research team published **Text2Tracks** (April 2025), a fine-tuned LLM that generates track IDs directly from text prompts using Semantic IDs built on collaborative filtering vectors. SpotifyForge can implement this by translating prompts into audio feature filters and search queries
- **AI-generated playlist descriptions**
- **AI cover art** — via DALL-E/Midjourney pipelines analyzing playlist mood averages
- **Automatic naming**

---

## The Optimal Architecture Shares a Core Between CLI and Web

The recommended stack centers on a **shared core layer** pattern where business logic lives in framework-agnostic Python modules, with thin CLI and web layers calling into the same services.

### CLI Layer

- **Typer** — built on Click, by the same author as FastAPI. Eliminates boilerplate through Python type hints, provides auto-completion and auto-generated help
- **Rich** (50k+ GitHub stars) — handles terminal output with tables, progress bars, panels, and live displays
- **Textual** (also by Textualize) — for power users wanting a full terminal dashboard; CSS-styled, reactive TUI framework at 120 FPS

### Web Layer

- **FastAPI** — clear backend choice: async-first, auto-generated API docs, Pydantic validation, 78.9k+ stars
- Frontend tiers:
  - **HTMX + Jinja2** — simplest approach (no JS build tools, SSE for real-time updates)
  - **NiceGUI** — richer Python-native UI (built on FastAPI + Vue.js + Tailwind, event-driven with no page reruns) — **recommended sweet spot for initial release**
  - **React/Vue SPA** — maximum customization

### Scheduling

**APScheduler** with `AsyncIOScheduler` for FastAPI integration and `BackgroundScheduler` for CLI mode. Supports cron, interval, and date triggers with SQLite job persistence. Only upgrade to Celery + Redis if distributed workers become necessary.

Example schedule configuration (TOML):

```toml
[scheduling.jobs.daily_discover]
type = "update"
playlist = "Daily Discover"
cron = "0 6 * * *"
source = "recommendations"
```

### Database

**SQLite** for personal/single-user use (zero-config, Python built-in support), with **SQLModel** as the ORM (combines SQLAlchemy + Pydantic, same author as FastAPI/Typer).

Cache TTL guidelines:

| Data Type | TTL |
|-----------|-----|
| Audio features | Indefinite (never change) |
| Track metadata | 7 days |
| Artist data | 24 hours |
| Playlist contents | 1–6 hours (use `snapshot_id` for change detection) |

Spotify's ToS requires deleting user data on logout.

### OAuth Token Management

- **keyring** library for local CLI use (stores in OS keychain)
- Environment variables for Docker deployments
- Encrypted database storage (Fernet from `cryptography`) for multi-user web deployment
- Multi-account support uses separate cache files keyed by Spotify user ID

### Project Structure

```
spotifyforge/
├── core/          # Business logic (framework-agnostic)
├── models/        # SQLModel/Pydantic models
├── db/            # Repository layer
├── auth/          # OAuth handling
├── cli/           # Typer CLI layer
├── web/           # FastAPI web layer
└── pyproject.toml # Entry points configuration
```

### Testing

- **pytest** with `unittest.mock` for API mocking
- **vcrpy** for recording/replaying API responses
- Typer's `CliRunner` for CLI tests
- FastAPI's `TestClient` for web endpoint tests

---

## No Dominant Curator Platform Exists — A Clear Market Gap

The competitive landscape reveals a fragmented ecosystem with **no all-in-one platform for serious playlist curators**.

### Existing Competitors

| Tool | Category | Limitations |
|------|----------|-------------|
| **Skiley** | Closest competitor — AI playlists, deduplication, stats, Discover Weekly archiving | Limited free tier, no growth features |
| **Smarter Playlists / Playlist Machinery** | Graph-based playlist logic | Terrible UX, effectively unmaintained |
| **PlaylistAI** | Generation only | No ongoing management, $3.99–$99.99 |
| **IFTTT / Zapier** | Generic automation | Limited Spotify-specific intelligence, expensive for power users |

### Submission Platforms (Artist-Facing, Not Curator-Facing)

| Platform | Pricing | Notes |
|----------|---------|-------|
| **SubmitHub** | ~$0.63–$1/credit | 20% premium approval rate |
| **PlaylistPush** | Min $250/campaign | 32% average acceptance |
| **Groover** | €2/credit | — |
| **PlaylistSupply** | $19.99/month | Pure search/research |

A documented $600 A/B test showed SubmitHub was **9x more cost-effective** than PlaylistPush ($0.009 vs $0.061 per stream).

### Playlist Influence Thresholds

| Followers | Level |
|-----------|-------|
| 1,000+ | Minimum platform acceptance |
| 5,000–10,000 | Monetization potential |
| 50,000+ | Major independent influence |
| 100,000+ | Industry-level impact |

### Growth Tactics

- Meta ads at **$0.12/conversion** with 60% follower conversion rate
- Reddit community engagement (one curator gained 100+ followers in a single night posting to college subreddit during finals)
- Artist collaboration (adding indie artists who cross-promote)
- Playlist SEO through keyword-rich titles and descriptions

### Open-Source Landscape

| Project | Stars | Focus |
|---------|-------|-------|
| **Spicetify** | 22.2K | Client modification |
| **spotDL** | ~15K+ | Downloading |
| **Spotipy** | ~5K+ | Library layer |

No major open-source playlist curation automation project exists. The gap between personal scripts and production tools is wide open.

---

## Positioning SpotifyForge and Navigating Key Risks

### Positioning

SpotifyForge should position as **"the all-in-one platform for serious Spotify playlist curators"** — combining playlist management, AI curation, growth analytics, and automation in one tool.

### Key Differentiators

- **Automated rules engine with good UX** — unlike Smarter Playlists' graph-based complexity, offer intuitive rules like "if this artist releases a track matching mood X and energy Y, auto-add to playlist Z"
- **Curator-first orientation** — most competitors target artists paying for placement; the underserved market is curators wanting to build, manage, and monetize their playlists
- **Growth toolkit** — built-in follower analytics, social sharing asset generation, playlist SEO suggestions, and optimal update timing recommendations
- **Playlist health monitoring** — alert when tracks are removed from Spotify (rights changes), detect engagement drops, flag low-engagement tracks, auto-deduplicate

### Pricing Model

| Tier | Price | Features |
|------|-------|----------|
| **Free** | $0 | Basic organization, limited actions |
| **Pro** | $9.99–$19.99/month | Unlimited actions, AI curation, auto-archiving |
| **Business** | $49–$99/month | Multi-account management, analytics dashboards, API access |
| **Agency** | $199–$499/month | White-label and team features |

### Critical Risks

1. **Platform dependency** — Spotify can revoke API access at any time and has been increasingly restrictive
2. **Chicken-and-egg problem with extended quota** — requiring 250K+ MAU to access key endpoints, but needing those endpoints to attract users
3. **Spotify building competing features natively** — they already launched AI Playlists and Prompted Playlists

### Mitigation Strategy

Build core value around features Spotify doesn't offer natively:

- Cross-platform data enrichment
- External API integration
- Growth analytics
- Curator workflow automation

Stay compliant with Developer Terms by focusing on non-streaming functionality and never training ML models on Spotify content.

---

## Conclusion

SpotifyForge enters a market defined by a paradox: Spotify's API restrictions have raised the barrier to entry while simultaneously eliminating weaker competitors who relied on deprecated endpoints. The surviving opportunity lies in combining playlist CRUD operations (still fully functional) with third-party audio analysis APIs, external data sources (Last.fm, MusicBrainz, concert calendars, music critics), and AI-powered curation to create a platform no single existing tool matches.

The Typer + FastAPI + SQLModel technology stack provides the rare combination of CLI power-user appeal and web dashboard accessibility from a shared codebase. The **6,291 micro-genres** from Every Noise at Once, combined with BPM/mood/energy sorting from third-party APIs, concert calendar integration, and AI-generated descriptions and cover art, create a feature surface that could make SpotifyForge-curated playlists genuinely worth following.

Starting with ≤5 users in dev mode, building differentiated value through external integrations, and planning for extended quota application after achieving scale through non-API channels represents the most pragmatic path to market.
