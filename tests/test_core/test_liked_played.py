"""Liked against played: candidates to like, and likes no record has heard."""

from __future__ import annotations

from datetime import UTC, date, timedelta, timezone

from spotifyforge.core.curation import CurationTrack
from spotifyforge.core.history import Play
from spotifyforge.core.liked_played import (
    PLAYLIST_LIMIT,
    Heard,
    TopList,
    compare,
    liked_vs_played,
    local_day,
    read_top_list,
    write_playlist,
)


def _liked(track_id: str, name: str | None = None, artist: str = "Alpha", isrc: str | None = None):
    return CurationTrack(
        id=track_id,
        uri=f"spotify:track:{track_id}",
        name=name or f"Song {track_id}",
        artist_ids=(artist.lower(),),
        artist_names=(artist,),
        release_year=2001,
        popularity=50,
        isrc=isrc,
        album_name=f"Album {track_id}",
    )


def _play(track_id: str, day: int = 10, name: str | None = None, artist: str = "Beta", isrc=None):
    return Play(
        played_at=f"2026-10-{day:02d}T12:00:00+00:00",
        track_id=track_id,
        name=name or f"Song {track_id}",
        artists=(artist,),
        artist_ids=(artist.lower(),),
        album="",
        album_id=None,
        duration_ms=200_000,
        isrc=isrc,
    )


def _top(time_range: str, *ids: str, artist: str = "Beta") -> TopList:
    return TopList(time_range, tuple(Heard(i, f"Song {i}", (artist,)) for i in ids))


def test_a_song_played_often_and_not_liked_is_a_candidate():
    plays = [_play("p1", day) for day in (10, 11, 12)] + [_play("once", 13)]

    report = compare([], plays, [])

    assert [(c.track_id, c.plays) for c in report.played_not_liked] == [("p1", 3)]


def test_a_song_already_liked_is_not_a_candidate_however_often_played():
    plays = [_play("l1", day, artist="Alpha") for day in (10, 11, 12)]

    report = compare([_liked("l1")], plays, [])

    assert report.played_not_liked == ()


def test_the_head_of_a_top_list_nominates_but_its_tail_does_not():
    tail = [f"x{i}" for i in range(60)]
    tops = [_top("long_term", "head", *tail)]

    report = compare([], [], tops, head=50)

    nominated = {c.track_id for c in report.played_not_liked}
    assert "head" in nominated
    assert "x10" in nominated  # rank 12
    assert "x55" not in nominated  # rank 57: may be one play
    (head,) = [c for c in report.played_not_liked if c.track_id == "head"]
    assert head.top_ranks == (("long_term", 1),)


def test_candidates_are_ranked_by_log_plays_then_best_top_rank():
    plays = [_play("p1", d) for d in (10, 11, 12, 13)] + [_play("p2", d) for d in (10, 11, 12)]
    tops = [_top("short_term", "t1", "t2")]

    report = compare([], plays, tops)

    assert [c.track_id for c in report.played_not_liked] == ["p1", "p2", "t1", "t2"]


def test_editions_of_one_song_are_counted_together():
    plays = [
        _play("album", 10, name="Bocanada"),
        _play("remaster", 11, name="Bocanada - Remasterizado 2007"),
        _play("album", 12, name="Bocanada"),
    ]

    report = compare([], plays, [])

    assert [(c.name, c.plays) for c in report.played_not_liked] == [("Bocanada", 3)]


def test_a_played_remaster_or_isrc_match_counts_as_the_liked_song():
    liked = [
        _liked("cut", name="Bocanada", artist="Beta"),
        _liked("isrc1", name="Other Title", artist="Gamma", isrc="usabc0000001"),
    ]
    plays = [_play("rm", d, name="Bocanada - Remasterizado 2007") for d in (10, 11, 12)]
    plays += [_play("re", d, name="Different", isrc="USABC0000001") for d in (10, 11, 12)]

    report = compare(liked, plays, [])

    assert report.played_not_liked == ()
    assert report.liked_not_heard == ()


def test_liked_but_not_heard_is_oldest_like_first_and_skips_anything_heard():
    # Library order is newest like first, as Spotify returns it.
    liked = [_liked("newest"), _liked("logged"), _liked("deep"), _liked("oldest")]
    plays = [_play("logged", artist="Alpha")]
    tops = [_top("long_term", *[f"x{i}" for i in range(300)], "deep", artist="Alpha")]

    report = compare(liked, plays, tops)

    # A rank far past the head still counts as heard.
    assert [t.track_id for t in report.liked_not_heard] == ["oldest", "newest"]


def test_two_unheard_editions_of_one_liked_song_are_listed_once():
    liked = [_liked("a", name="Song - 2011 Remaster"), _liked("b", name="Song")]

    report = compare(liked, [], [])

    assert [t.track_id for t in report.liked_not_heard] == ["b"]


def test_the_wording_names_the_log_start_and_never_says_never():
    report = compare([_liked("a")], [_play("z", 10), _play("y", 12)], [])

    assert report.log_since == "2026-10-10"
    assert report.not_heard_wording == "not in your top lists or your log since 2026-10-10"
    assert "never" not in report.not_heard_wording
    assert compare([], [], []).not_heard_wording == "not in your top lists (the log is empty)"


def test_dates_are_read_on_the_local_clock_not_utc():
    pacific = timezone(timedelta(hours=-7))

    assert local_day("2026-10-10T03:00:00+00:00", pacific) == "2026-10-09"
    assert local_day("2026-10-10T03:00:00+00:00", UTC) == "2026-10-10"


def test_the_playlist_size_is_capped_and_says_so():
    liked = [_liked(f"t{i:03d}") for i in range(150)]

    report = compare(liked, [], [])

    assert len(report.liked_not_heard) == 150
    assert report.playlist_size() == PLAYLIST_LIMIT == 100
    assert report.playlist_size(5) == 5
    assert compare(liked[:3], [], []).playlist_size() == 3


async def test_a_top_list_is_read_past_its_first_page(fake_spotify, client_for):
    fake = fake_spotify
    fake.add_user("user1")
    ids = [fake.add_track(f"t{i:03d}") for i in range(120)]
    fake.top_tracks_by_range[("user1", "long_term")] = ids
    fake.top_tracks_by_range[("user1", "short_term")] = ids[:3]

    long_list = await read_top_list(client_for(), "long_term")
    short_list = await read_top_list(client_for(), "short_term")
    capped = await read_top_list(client_for(), "long_term", depth=60)

    assert [h.track_id for h in long_list.tracks] == ids
    assert long_list.tracks[0].isrc == "ISRCT000"
    assert [h.track_id for h in short_list.tracks] == ids[:3]
    assert len(capped.tracks) == 60


async def test_end_to_end_against_the_fake_and_the_playlist_is_private(fake_spotify, client_for):
    fake = fake_spotify
    fake.add_user("user1")
    for track_id in ("liked_heard", "liked_quiet", "liked_older"):
        fake.add_track(track_id, name=track_id, artist_id=f"a_{track_id}")
        fake.save_track("user1", track_id)
    fake.add_track("habit", name="Habit", artist_id="a_habit")
    fake.top_tracks_by_range[("user1", "medium_term")] = ["liked_heard", "habit"]
    plays = [_play("habit", day, name="Habit", artist="Artist One") for day in (10, 11, 12)]

    report = await liked_vs_played(client_for(), plays)

    assert report.liked == 3
    assert dict(report.top_lists) == {"short_term": 0, "medium_term": 2, "long_term": 0}
    assert [(c.name, c.plays) for c in report.played_not_liked] == [("Habit", 3)]
    # Saved in that order, so the fake returns liked_older last: liked longest ago.
    assert [t.track_id for t in report.liked_not_heard] == ["liked_older", "liked_quiet"]

    created = await write_playlist(client_for(), report, date(2026, 10, 22))

    meta = fake.playlists[created.spotify_id]
    assert meta["public"] is False
    assert meta["name"] == "Liked, not heard lately (2026-10-22)"
    assert "since 2026-10-10" in meta["description"]
    assert fake.playlist_tracks[created.spotify_id] == ["liked_older", "liked_quiet"]


async def test_the_playlist_keeps_only_the_oldest_likes_up_to_the_limit(fake_spotify, client_for):
    fake = fake_spotify
    fake.add_user("user1")
    for track_id in ("newest", "middle", "oldest"):
        fake.add_track(track_id, name=track_id, artist_id=f"a_{track_id}")
        fake.save_track("user1", track_id)

    report = await liked_vs_played(client_for(), [])
    created = await write_playlist(client_for(), report, date(2026, 10, 22), limit=2)

    assert [t.track_id for t in report.liked_not_heard] == ["oldest", "middle", "newest"]
    assert fake.playlist_tracks[created.spotify_id] == ["oldest", "middle"]
