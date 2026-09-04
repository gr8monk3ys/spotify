## Media fleet: the join is alive, the engines are frozen (2026-08-30)

Five repos fed the cross-platform join; the keep-alive review measured cost
(tracked Python) against rows actually delivered into `~/.music`, `~/.movies`,
`~/.books`:

| repo | cost | delivered | call |
|---|---|---|---|
| spotify | 19,455 | 1,736 albums + 172 discoveries | **invest** — public, live CI, spine of the join |
| letterboxd | 30,098 | 1,633 films + 831 watchlist | data leg only; review + follow engines frozen |
| goodreads | 7,520 | 1,063 books (78 read) | export only |
| discogs | 10,379 | 147 rows | export only — worst ratio, irreplaceable rows |
| rym | 3,522 | **0 rows, ever** | **retired + archived** |

**Do not re-grow the review engine.** The ~7,300 lines of letterboxd
review/tone/campaign/follow code and goodreads `post-reviews` stay in their
repos but are unwired from `media_sync.sh`. 34 posted AI reviews drew 0 likes —
and so did the account's own reviews, so this does not condemn the AI; it means
the account has no audience and **volume buys nothing**. Aim at
representativeness (118 films rated ≥4.5 with no review), never coverage.

This deliberately sacrifices "good-looking accounts", one of the four stated
purposes, because it is the only one with a measurement and the measurement is
zero. Portfolio, personal utility and enjoyment are untouched.

RYM was never in `repos.yml` — which is *why* it never ran: no loop ever fed it,
so its hand-rating queue sat at `rated 0 / queued 1633` from the day it was
built. It could not have been fixed by registering it, either (orchestrator #39,
closed): RateYourMusic prohibits automated access and names `ClaudeBot` in its
robots.txt, so `rym` could never carry a `data_refresh` loop. Hand-feeding was
the only path in, and it was never once used. `~/.music/rym.json` is set aside as `rym.json.retired-2026-08-30`.
Reversible: `gh repo unarchive gr8monk3ys/rym` + revert the sync-script commit.


_Moved here from `~/code/CLAUDE.md` on 2026-09-01; the same section lives in spotify, letterboxd, goodreads and discogs._
