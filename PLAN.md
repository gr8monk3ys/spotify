# Implementation Plan: Features 3, 4, 5, 8

## Feature 3: Smart Curation Rule Engine

The `CurationRule` model already exists with `conditions` (JSON), `actions` (JSON), `rule_type` enum, and `priority`. No execution logic exists.

### New files:
- `spotifyforge/core/curation_engine.py` — Pure-function rule evaluation pipeline
- `tests/test_core/test_curation_engine.py` — Unit tests

### What it does:
- **Rule evaluation pipeline**: Takes a list of tracks + audio features + rules → returns filtered/sorted/modified track list
- **Condition evaluators**: `popularity_below`, `popularity_above`, `older_than_days`, `energy_range`, `tempo_range`, `genre_match`, `added_before`, `acousticness_range`
- **Action executors**: `remove` (filter out matching), `sort_by` (reorder), `limit` (cap count), `add_from_pool` (inject tracks from a candidate pool)
- **Pipeline runner**: Executes rules in priority order, chaining output → input
- **Scheduler integration**: New `curation_apply` job type that runs rules on a playlist

### Rule JSON schema examples:
```json
// Filter rule: remove tracks below popularity 20 that are older than 14 days
{"conditions": {"popularity_below": 20, "added_before_days": 14}, "actions": {"action": "remove"}}

// Sort rule: order by energy descending
{"conditions": {}, "actions": {"sort_by": "energy", "order": "desc"}}

// Limit rule: keep top 50 tracks
{"conditions": {}, "actions": {"limit": 50}}
```

### Changes to existing files:
- `models/models.py` — Add `JobType.curation_apply`, add `CurationRuleEvalLog` model for audit trail
- `core/scheduler.py` — Add `_handle_curation_apply` dispatcher
- `web/routes.py` — Add CRUD endpoints for curation rules + dry-run endpoint
- `cli/app.py` — Add `rule create/list/run/delete` CLI commands
- New alembic migration for `CurationRuleEvalLog` table

---

## Feature 4: ML-Powered Recommendations

### New files:
- `spotifyforge/core/recommender.py` — Content-based recommendation engine using audio features
- `tests/test_core/test_recommender.py` — Unit tests

### What it does:
- **Content-based filtering**: Uses audio features (energy, danceability, valence, tempo, acousticness, instrumentalness, speechiness, liveness) to compute track similarity via cosine distance
- **User taste profile**: Aggregates audio features from user's top tracks to build a preference vector
- **Playlist-aware recommendations**: "Find tracks similar to this playlist but not already in it"
- **Diversity-aware selection**: Ensures recommendations aren't all the same vibe — uses MMR (Maximal Marginal Relevance) to balance relevance vs diversity
- **Track scoring**: Composite score = similarity × freshness × popularity_boost

### No external ML libraries needed — uses numpy-free math (cosine similarity on 8-dim vectors is trivial in pure Python).

### Changes to existing files:
- `web/routes.py` — Add `GET /api/recommend/similar-tracks`, `GET /api/recommend/playlist-expansion`, `GET /api/recommend/taste-profile`
- `cli/app.py` — Add `recommend similar/expand/profile` commands

---

## Feature 5: Real-Time Notifications via WebSocket

### New files:
- `spotifyforge/core/notifications.py` — Event bus + notification manager
- `spotifyforge/web/websocket.py` — WebSocket endpoint + connection manager
- `tests/test_core/test_notifications.py` — Unit tests
- `tests/test_web/test_websocket.py` — WebSocket integration tests

### What it does:
- **Event bus**: In-process pub/sub — services emit events, subscribers receive them
- **Event types**: `playlist_changed`, `new_release_detected`, `competitor_update`, `curation_rule_applied`, `job_completed`, `job_failed`, `health_alert`
- **WebSocket endpoint**: `ws://localhost:8000/ws/notifications` — clients connect with auth token, receive real-time JSON events
- **Connection manager**: Tracks active connections per user, handles reconnect/cleanup
- **Webhook support**: Optional outbound HTTP POST to user-configured URLs on events

### Changes to existing files:
- `models/models.py` — Add `WebhookConfig` model (user_id, url, events, secret, enabled)
- `config.py` — Add `notifications_enabled`, `webhook_timeout` settings
- `web/app.py` — Mount WebSocket endpoint
- `core/scheduler.py` — Emit events after job completion/failure
- New alembic migration for `webhook_configs` table

---

## Feature 8: Production Hardening

### New files:
- `spotifyforge/core/circuit_breaker.py` — Circuit breaker for external API calls
- `spotifyforge/core/retry.py` — Exponential backoff retry with jitter
- `spotifyforge/logging_config.py` — Structured JSON logging with correlation IDs
- `tests/test_core/test_circuit_breaker.py` — Unit tests
- `tests/test_core/test_retry.py` — Unit tests

### What it does:

**Circuit Breaker:**
- States: CLOSED → OPEN → HALF_OPEN → CLOSED
- Tracks failure count per service (spotify_api, database, webhook)
- Opens after N consecutive failures, auto-recovers after cooldown
- Raises `CircuitOpenError` when tripped — callers handle gracefully

**Retry with backoff:**
- Decorator: `@with_retry(max_attempts=3, base_delay=1.0, max_delay=30.0)`
- Exponential backoff with jitter to prevent thundering herd
- Configurable retry-on exception types (e.g., only retry on 429/5xx)

**Structured Logging:**
- JSON log format for production (human-readable for dev)
- Correlation ID middleware — generates UUID per request, threads through all logs
- Request/response logging with timing, status, user_id
- Configurable via `SPOTIFYFORGE_LOG_FORMAT=json|text`

**Rate Limit Improvements:**
- Spotify API rate limit tracking — parse `Retry-After` headers
- Per-endpoint rate limiting (not just per-IP)

### Changes to existing files:
- `config.py` — Add `log_format`, `circuit_breaker_threshold`, `circuit_breaker_cooldown` settings
- `web/app.py` — Add correlation ID middleware, switch to structured logging
- `core/scheduler.py` — Wrap job execution with circuit breaker + retry
- `core/discovery.py` — Wrap Spotify API calls with retry decorator
- `core/playlist_manager.py` — Wrap Spotify API calls with retry decorator

---

## Implementation Order

1. **Feature 8 (Production Hardening)** — Foundation that other features build on
2. **Feature 3 (Curation Engine)** — Pure logic, builds on existing models
3. **Feature 4 (Recommender)** — Pure logic, uses audio features
4. **Feature 5 (Notifications)** — Integrates with all above features

## Database Migration

Single new alembic migration `003_add_curation_notifications` adding:
- `curation_eval_logs` table
- `webhook_configs` table
- New `curation_apply` value in job_type enum
