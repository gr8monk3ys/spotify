"""Tests for naming (curation.title_for) and safe renames (core/renaming).

Renaming is the dangerous operation in this catalogue: identity is the
title, so a half-done or mis-targeted rename strands live playlists
along with their followers and artwork. These tests are mostly about
what must *not* happen.
"""

from __future__ import annotations

import json

from spotifyforge.core.curation import CurationTrack, legacy_title, title_for
from spotifyforge.core.playlist_manager import PlaylistManager
from spotifyforge.core.renaming import (
    Rename,
    apply_renames,
    migrate_cover_picks,
    plan_renames,
)


def _ct(track_id: str, genres: tuple[str, ...]) -> CurationTrack:
    return CurationTrack(
        id=track_id,
        uri=f"spotify:track:{track_id}",
        name=f"Song {track_id}",
        artist_ids=("a1",),
        artist_names=("Artist",),
        release_year=1990,
        popularity=30,
        genres=genres,
    )


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------


def test_stacks_siblings_that_carry_the_playlist():
    tracks = [_ct(f"t{i}", ("shoegaze", "dream pop")) for i in range(10)]
    assert title_for("shoegaze", None, tracks) == "shoegaze | dream pop"


def test_does_not_stack_a_genre_only_a_couple_of_tracks_mention():
    """Stacking a barely-present genre is keyword spam and misdescribes
    the mix."""
    tracks = [_ct(f"t{i}", ("coldwave",)) for i in range(20)]
    tracks[0].genres = ("coldwave", "jazz")
    tracks[1].genres = ("coldwave", "jazz")
    assert "jazz" not in title_for("coldwave", None, tracks)


def test_era_leads_the_name():
    assert title_for("spiritual jazz", 1970, []) == "70s spiritual jazz"
    assert title_for("bedroom pop", 2000, []) == "00s bedroom pop"


def test_hook_is_family_flavoured_stable_and_names_the_genre():
    solo = [_ct(f"t{i}", ("hard bop",)) for i in range(8)]
    title = title_for("hard bop", None, solo)

    assert "hard bop" in title  # search indexes the title; never drop it
    assert title != "hard bop"  # but it should not be bare either
    assert title == title_for("hard bop", None, solo)  # stable across runs
    assert title in {
        "hard bop after the last set",
        "the hard bop smoke room",
        "hard bop at 3am",
        "blue hour hard bop",
        "hard bop on a slow night",
    }


def test_long_stacks_shed_partners_rather_than_run_on():
    tracks = [
        _ct(f"t{i}", ("experimental hip hop", "alternative hip hop", "underground hip hop"))
        for i in range(10)
    ]
    title = title_for("experimental hip hop", None, tracks)
    assert len(title) <= 48


def test_unclassified_keeps_its_name():
    assert title_for(None, None, []) == "beyond genre"
    assert title_for(None, 1980, []) == "beyond genre ('80s)"


def test_legacy_title_reproduces_the_old_scheme():
    """The rename can only find live playlists if this still matches
    what they were actually called."""
    assert legacy_title("zeuhl", None) == "zeuhl // late transmissions"
    assert legacy_title("techno", None) == "strictly techno"
    assert legacy_title("psychedelic rock", 1970) == "psychedelic rock, annotated ('70s)"


# ---------------------------------------------------------------------------
# Planning and migrating
# ---------------------------------------------------------------------------


def test_plan_pairs_legacy_names_with_current_ones():
    from spotifyforge.core.curation import PlaylistSpec

    specs = [
        PlaylistSpec(title="90s shoegaze", description="", genre="shoegaze", decade=1990),
        PlaylistSpec(title="beyond genre", description="", genre=None, decade=None),
    ]
    renames = plan_renames(specs)

    # The unclassified playlist's name did not change, so it is left out.
    assert renames == [Rename(old="shoegaze after hours ('90s)", new="90s shoegaze")]


def test_cover_picks_follow_the_rename(tmp_path):
    """Picks are keyed by title; without this every cover would look
    unset and be re-picked with fresh photographs."""
    path = tmp_path / "photo_covers.json"
    path.write_text(json.dumps({"strictly techno": {"photo_id": 7}, "untouched": {"photo_id": 9}}))

    moved = migrate_cover_picks(
        [Rename(old="strictly techno", new="techno until the lights")], path
    )

    picks = json.loads(path.read_text())
    assert moved == 1
    assert picks["techno until the lights"]["photo_id"] == 7
    assert "strictly techno" not in picks
    assert picks["untouched"]["photo_id"] == 9


# ---------------------------------------------------------------------------
# Applying (through the fake Spotify backend)
# ---------------------------------------------------------------------------


async def test_renames_in_place_and_leaves_everything_else_alone(fake_spotify, client_for):
    fake_spotify.add_user("user1")
    fake_spotify.add_playlist("pl1", name="strictly techno")
    fake_spotify.add_playlist("pl2", name="marble rooms")  # personal, hand-named
    sp = client_for("user1")

    renamed, already, failed = await apply_renames(
        PlaylistManager(sp),
        sp,
        [Rename(old="strictly techno", new="techno until the lights")],
        delay=0,
    )

    assert (len(renamed), already, failed) == (1, [], [])
    # Same playlist id — followers, artwork and description all survive.
    assert fake_spotify.playlists["pl1"]["name"] == "techno until the lights"
    assert fake_spotify.playlists["pl2"]["name"] == "marble rooms"


async def test_rerun_after_an_interrupted_rename_is_a_no_op(fake_spotify, client_for):
    """A half-finished run leaves two naming schemes live; re-running
    must finish the job rather than fail on the ones already moved."""
    fake_spotify.add_user("user1")
    fake_spotify.add_playlist("pl1", name="techno until the lights")  # already done
    fake_spotify.add_playlist("pl2", name="strictly coldwave")  # still pending
    sp = client_for("user1")

    renamed, already, failed = await apply_renames(
        PlaylistManager(sp),
        sp,
        [
            Rename(old="strictly techno", new="techno until the lights"),
            Rename(old="strictly coldwave", new="coldwave after dark"),
        ],
        delay=0,
    )

    assert [r.new for r in renamed] == ["coldwave after dark"]
    assert already == ["techno until the lights"]
    assert failed == []


async def test_a_playlist_that_is_not_there_is_reported_not_crashed(fake_spotify, client_for):
    fake_spotify.add_user("user1")
    sp = client_for("user1")

    renamed, already, failed = await apply_renames(
        PlaylistManager(sp), sp, [Rename(old="strictly gone", new="gone, quietly")], delay=0
    )

    assert (renamed, already, failed) == ([], [], ["strictly gone"])


async def test_other_peoples_playlists_are_never_touched(fake_spotify, client_for):
    fake_spotify.add_user("user1")
    fake_spotify.add_user("user2")
    fake_spotify.add_playlist("pl9", name="strictly techno", owner="user2")
    sp = client_for("user1")

    renamed, _, failed = await apply_renames(
        PlaylistManager(sp),
        sp,
        [Rename(old="strictly techno", new="techno until the lights")],
        delay=0,
    )

    assert renamed == []
    assert failed == ["strictly techno"]
    assert fake_spotify.playlists["pl9"]["name"] == "strictly techno"
