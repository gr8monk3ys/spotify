"""The listening history: an append-only log fed from a fifty-play window."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from spotifyforge.core.history import (
    WINDOW,
    load_plays,
    playlist_usage,
    summarise,
    sync_history,
)

START = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _at(minutes: int) -> str:
    return (START + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture()
def fake(fake_spotify):
    fake_spotify.add_user("user1")
    fake_spotify.add_track("t1", name="One", artist_id="a1", artist_name="Alpha")
    fake_spotify.add_track("t2", name="Two", artist_id="a2", artist_name="Beta")
    return fake_spotify


async def test_first_sync_writes_plays_oldest_first(fake, client_for, tmp_path):
    fake.play("user1", "t2", _at(5))
    fake.play("user1", "t1", _at(0), context="spotify:playlist:pl1")
    path = tmp_path / "history.jsonl"

    result = await sync_history(client_for(), path)

    assert (result.added, result.total, result.possible_gap) == (2, 2, False)
    plays = load_plays(path)
    assert [p.track_id for p in plays] == ["t1", "t2"]
    first = plays[0]
    assert first.name == "One"
    assert first.artists == ("Alpha",)
    assert first.context == "spotify:playlist:pl1"
    assert first.at == START
    assert plays[1].context is None


async def test_resync_appends_only_what_is_new(fake, client_for, tmp_path):
    path = tmp_path / "history.jsonl"
    fake.play("user1", "t1", _at(0))
    await sync_history(client_for(), path)

    again = await sync_history(client_for(), path)
    assert (again.added, again.total) == (0, 1)

    fake.play("user1", "t2", _at(10))
    third = await sync_history(client_for(), path)
    assert (third.added, third.total) == (1, 2)
    assert [p.track_id for p in load_plays(path)] == ["t1", "t2"]


async def test_the_same_track_twice_is_two_plays(fake, client_for, tmp_path):
    path = tmp_path / "history.jsonl"
    fake.play("user1", "t1", _at(0))
    fake.play("user1", "t1", _at(4))

    result = await sync_history(client_for(), path)

    assert result.added == 2


async def test_a_full_window_of_new_plays_is_reported_as_a_possible_gap(fake, client_for, tmp_path):
    path = tmp_path / "history.jsonl"
    fake.play("user1", "t1", _at(0))
    await sync_history(client_for(), path)

    # Sixty more plays before the next poll: Spotify forgets the first ten.
    for i in range(1, 61):
        fake.play("user1", "t2", _at(i * 3))
    result = await sync_history(client_for(), path)

    assert result.added == WINDOW
    assert result.possible_gap is True


async def test_a_full_window_on_the_very_first_sync_is_not_a_gap(fake, client_for, tmp_path):
    for i in range(60):
        fake.play("user1", "t1", _at(i * 3))

    result = await sync_history(client_for(), tmp_path / "history.jsonl")

    assert result.added == WINDOW
    assert result.possible_gap is False


async def test_a_torn_last_line_does_not_lose_the_history(fake, client_for, tmp_path):
    path = tmp_path / "history.jsonl"
    fake.play("user1", "t1", _at(0))
    await sync_history(client_for(), path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"played_at": "2026-10-01T12:0')  # killed mid-write

    assert [p.track_id for p in load_plays(path)] == ["t1"]

    fake.play("user1", "t2", _at(10))
    result = await sync_history(client_for(), path)
    assert result.added == 1


async def test_history_is_stored_as_utf8_json_lines(fake, client_for, tmp_path):
    fake.add_track("jp", name="街のドルフィン", artist_id="a3", artist_name="濱田金吾")
    fake.play("user1", "jp", _at(0))
    path = tmp_path / "history.jsonl"

    await sync_history(client_for(), path)

    line = path.read_text(encoding="utf-8").splitlines()[0]
    assert "街のドルフィン" in line  # readable, not \\u-escaped
    assert json.loads(line)["artists"] == ["濱田金吾"]


async def test_load_plays_since(fake, client_for, tmp_path):
    path = tmp_path / "history.jsonl"
    fake.play("user1", "t1", _at(0))
    fake.play("user1", "t2", _at(120))
    await sync_history(client_for(), path)

    recent = load_plays(path, since=START + timedelta(hours=1))

    assert [p.track_id for p in recent] == ["t2"]


def test_load_plays_without_a_file_is_empty(tmp_path):
    assert load_plays(tmp_path / "nothing.jsonl") == []


async def test_summarise_counts_tracks_artists_and_contexts(fake, client_for, tmp_path):
    path = tmp_path / "history.jsonl"
    fake.play("user1", "t1", _at(0), context="spotify:playlist:pl1")
    fake.play("user1", "t1", _at(5), context="spotify:playlist:pl1")
    fake.play("user1", "t2", _at(10))
    await sync_history(client_for(), path)

    summary = summarise(load_plays(path))

    assert summary.plays == 3
    assert summary.distinct_tracks == 2
    assert summary.top_tracks[0] == ("One", "Alpha", 2)
    assert summary.top_artists == [("Alpha", 2), ("Beta", 1)]
    assert summary.top_contexts == [("spotify:playlist:pl1", 2)]
    assert summary.first == START.isoformat()


async def test_playlist_usage_counts_only_plays_started_from_the_playlist(
    fake, client_for, tmp_path
):
    path = tmp_path / "history.jsonl"
    fake.play("user1", "t1", _at(0), context="spotify:playlist:mine1")
    fake.play("user1", "t1", _at(5), context="spotify:playlist:mine1")
    fake.play("user1", "t2", _at(10), context="spotify:playlist:dailymix")
    fake.play("user1", "t2", _at(15), context="spotify:album:alb")
    fake.play("user1", "t2", _at(20), context="spotify:artist:art")
    fake.play("user1", "t1", _at(25))  # liked songs: no context
    await sync_history(client_for(), path)

    usage = playlist_usage(load_plays(path), {"mine1": "Late Set", "mine2": "Never Opened"})

    assert usage.plays == 6
    assert usage.from_own_playlists == 2
    assert usage.from_other_playlists == 1
    assert (usage.from_albums, usage.from_artists, usage.from_elsewhere) == (1, 1, 1)
    assert usage.played == [("mine1", "Late Set", 2)]
    assert usage.unplayed == [("mine2", "Never Opened")]
    assert usage.days_covered == 1
    assert usage.long_enough_to_judge is False


def test_playlist_usage_needs_four_weeks_before_unplayed_means_anything():
    from spotifyforge.core.history import Play

    def play(day: int) -> Play:
        return Play(
            played_at=(START + timedelta(days=day)).isoformat(),
            track_id="t",
            name="t",
            artists=("a",),
            artist_ids=("a",),
            album="",
            album_id=None,
            duration_ms=1,
        )

    assert playlist_usage([play(0), play(26)], {}).long_enough_to_judge is False
    assert playlist_usage([play(0), play(27)], {}).long_enough_to_judge is True
    assert playlist_usage([], {"p": "x"}).unplayed == [("p", "x")]
