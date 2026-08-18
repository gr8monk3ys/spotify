"""Tests for follower-growth snapshots (core/stats.py).

Snapshot reads run against the FakeSpotify backend through the real
tekore stack; persistence and delta arithmetic are pure and tested
directly.
"""

from __future__ import annotations

import pytest
import tekore as tk

from spotifyforge.core.stats import (
    PlaylistStat,
    Snapshot,
    append_snapshot,
    growth_since,
    load_previous,
    take_snapshot,
)


@pytest.fixture()
async def client_for(fake_spotify):
    clients: list[tk.Spotify] = []

    def make(user_id: str = "user1") -> tk.Spotify:
        client = fake_spotify.async_client(user_id)
        clients.append(client)
        return client

    yield make
    for client in clients:
        await client.close()


def _snap(taken_at: str, account: int, playlists: list[tuple[str, str, int]]) -> Snapshot:
    return Snapshot(
        taken_at=taken_at,
        account_followers=account,
        playlists=[PlaylistStat(id=i, name=n, followers=f, tracks=10) for i, n, f in playlists],
    )


# ---------------------------------------------------------------------------
# Reading the account
# ---------------------------------------------------------------------------


async def test_snapshot_counts_owned_playlists_by_followers(fake_spotify, client_for):
    fake_spotify.add_user("user1")
    fake_spotify.add_track("t1")
    fake_spotify.add_playlist("quiet", name="Quiet", followers=0)
    fake_spotify.add_playlist("loud", name="Loud", track_ids=["t1"], followers=12)
    fake_spotify.add_user("user2")
    fake_spotify.add_playlist("theirs", owner="user2", name="Not Mine", followers=99)

    snap = await take_snapshot(client_for("user1"))

    assert snap.account_followers == 3  # the fake profile's fixed count
    # Someone else's playlist measures someone else's audience.
    assert [p.id for p in snap.playlists] == ["loud", "quiet"]  # busiest first
    assert snap.playlist_followers == 12
    assert snap.followed_playlists == 1
    assert snap.playlists[0].tracks == 1
    assert snap.taken_at  # stamped, ISO-parseable enough to sort


async def test_snapshot_paginates_past_50_playlists(fake_spotify, client_for):
    fake_spotify.add_user("user1")
    for i in range(60):
        fake_spotify.add_playlist(f"pl{i}", name=f"P{i}", followers=i)

    snap = await take_snapshot(client_for("user1"))

    assert len(snap.playlists) == 60
    assert snap.playlist_followers == sum(range(60))


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_history_roundtrips_and_returns_the_latest(tmp_path):
    path = tmp_path / "stats_history.jsonl"
    append_snapshot(_snap("2026-08-01T00:00:00+00:00", 5, [("a", "A", 1)]), path)
    append_snapshot(_snap("2026-08-08T00:00:00+00:00", 9, [("a", "A", 4)]), path)

    latest = load_previous(path)

    assert latest is not None
    assert latest.taken_at == "2026-08-08T00:00:00+00:00"
    assert latest.account_followers == 9
    assert latest.playlists[0].followers == 4


def test_history_tolerates_a_torn_trailing_line(tmp_path):
    path = tmp_path / "stats_history.jsonl"
    append_snapshot(_snap("2026-08-01T00:00:00+00:00", 5, [("a", "A", 1)]), path)
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"taken_at": "2026-08-08T00:00')  # killed mid-append

    latest = load_previous(path)

    assert latest is not None
    assert latest.taken_at == "2026-08-01T00:00:00+00:00"


def test_history_is_none_on_first_run(tmp_path):
    assert load_previous(tmp_path / "missing.jsonl") is None


# ---------------------------------------------------------------------------
# Growth arithmetic
# ---------------------------------------------------------------------------


def test_growth_ranks_movers_and_matches_by_id():
    previous = _snap("2026-08-01T00:00:00+00:00", 5, [("a", "A", 1), ("b", "B", 10)])
    current = _snap(
        "2026-08-08T00:00:00+00:00",
        8,
        [("a", "A renamed", 6), ("b", "B", 7), ("c", "Newcomer", 50)],
    )

    growth = growth_since(previous, current)

    assert growth.since == "2026-08-01T00:00:00+00:00"
    assert growth.account_delta == 3
    # The total is honest about newcomers; the movers list is not — a
    # playlist with no baseline has no delta to rank.
    assert growth.playlist_delta == (6 + 7 + 50) - (1 + 10)
    assert growth.movers == [("A renamed", 5), ("B", -3)]


def test_growth_is_quiet_when_nothing_changed():
    snap = _snap("2026-08-01T00:00:00+00:00", 5, [("a", "A", 1)])
    growth = growth_since(snap, snap)

    assert growth.account_delta == 0
    assert growth.playlist_delta == 0
    assert growth.movers == []
