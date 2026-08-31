"""Tests for following artists (core/following).

Following writes to a public profile, so the tests are mostly about
restraint: who is *not* followed, and what a second run does.
"""

from __future__ import annotations

from spotifyforge.core.curation import CurationTrack
from spotifyforge.core.following import (
    follow_artists,
    rank_candidates,
    unfollowed,
)


def _ct(track_id: str, artists: tuple[tuple[str, str], ...]) -> CurationTrack:
    return CurationTrack(
        id=track_id,
        uri=f"spotify:track:{track_id}",
        name=f"Track {track_id}",
        artist_ids=tuple(a for a, _ in artists),
        artist_names=tuple(n for _, n in artists),
        release_year=2020,
        popularity=50,
    )


# ---------------------------------------------------------------------------
# Ranking (pure)
# ---------------------------------------------------------------------------


def test_ranks_by_how_many_liked_songs_are_theirs():
    tracks = [_ct(f"t{i}", (("stott", "Andy Stott"),)) for i in range(5)]
    tracks += [_ct(f"u{i}", (("field", "The Field"),)) for i in range(2)]

    assert [(c.name, c.liked_tracks) for c in rank_candidates(tracks)] == [
        ("Andy Stott", 5),
        ("The Field", 2),
    ]


def test_a_one_off_guest_is_not_someone_you_follow():
    tracks = [_ct(f"t{i}", (("rico", "Rico Nasty"),)) for i in range(4)]
    tracks.append(_ct("t9", (("rico", "Rico Nasty"), ("locked", "Locked Club"))))

    assert [c.id for c in rank_candidates(tracks)] == ["rico"]


def test_a_repeat_guest_counts_as_much_as_a_lead():
    """Unlike genre inheritance — where a guest's tags misdescribe the
    song — being on several songs you saved is real evidence."""
    tracks = [_ct(f"t{i}", (("host", "Host"), ("guest", "Guest"))) for i in range(3)]

    assert {c.id: c.liked_tracks for c in rank_candidates(tracks)} == {"host": 3, "guest": 3}


def test_ranking_is_stable_so_a_dry_run_predicts_the_apply():
    tracks = [_ct(f"a{i}", (("b", "Beta"),)) for i in range(3)]
    tracks += [_ct(f"b{i}", (("a", "Alpha"),)) for i in range(3)]

    assert [c.name for c in rank_candidates(tracks)] == ["Alpha", "Beta"]


# ---------------------------------------------------------------------------
# Following (through the fake Spotify backend)
# ---------------------------------------------------------------------------


async def test_follows_only_the_artists_not_already_followed(fake_spotify, client_for):
    fake_spotify.add_user("user1")
    fake_spotify.followed_artists["user1"] = {"stott"}
    sp = client_for("user1")
    candidates = rank_candidates(
        [_ct(f"t{i}", (("stott", "Andy Stott"),)) for i in range(3)]
        + [_ct(f"u{i}", (("field", "The Field"),)) for i in range(2)]
    )

    followed, failed = await follow_artists(sp, candidates, delay=0)

    assert [c.id for c in followed] == ["field"]
    assert failed == []
    assert fake_spotify.followed_artists["user1"] == {"stott", "field"}


async def test_a_second_run_reports_nothing_rather_than_re_following(fake_spotify, client_for):
    fake_spotify.add_user("user1")
    sp = client_for("user1")
    candidates = rank_candidates([_ct(f"t{i}", (("stott", "Andy Stott"),)) for i in range(3)])

    await follow_artists(sp, candidates, delay=0)
    followed, failed = await follow_artists(sp, candidates, delay=0)

    assert (followed, failed) == ([], [])


async def test_unfollowed_keeps_the_ranked_order(fake_spotify, client_for):
    fake_spotify.add_user("user1")
    fake_spotify.followed_artists["user1"] = {"b"}
    sp = client_for("user1")

    assert await unfollowed(sp, ["a", "b", "c"]) == ["a", "c"]
