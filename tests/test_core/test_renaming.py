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
    match_by_contents,
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


# ---------------------------------------------------------------------------
# Matching a re-clustered catalogue onto the live one
# ---------------------------------------------------------------------------


def _spec(title, genre, track_ids):
    from spotifyforge.core.curation import PlaylistSpec

    return PlaylistSpec(
        title=title,
        description="",
        genre=genre,
        decade=None,
        tracks=[_ct(t, (genre,)) for t in track_ids],
    )


def test_a_re_clustered_playlist_is_recognised_by_its_songs():
    """The genre key moved, so only the tracks can say these are the
    same playlist. Getting this wrong creates a duplicate and strands
    the original's followers and artwork."""
    spec = _spec("hard techno | techno | tekno", "hard techno", ["a", "b", "c", "d"])
    live = {"acid techno | techno | hard techno": {"a", "b", "c", "x"}}

    renames, unmatched = match_by_contents([spec], live)

    assert renames == [
        Rename(old="acid techno | techno | hard techno", new="hard techno | techno | tekno")
    ]
    assert unmatched == []


def test_a_genuinely_new_playlist_is_left_to_be_forged():
    spec = _spec("70s spiritual jazz", "spiritual jazz", ["p", "q", "r"])
    live = {"strictly techno": {"a", "b", "c"}}

    renames, unmatched = match_by_contents([spec], live)

    assert renames == []
    assert [s.title for s in unmatched] == ["70s spiritual jazz"]


def test_two_similar_specs_cannot_claim_the_same_live_playlist():
    """Both descend from one over-broad playlist; only the closer one
    inherits it, and the other is forged fresh."""
    close = _spec("hard techno | tekno", "hard techno", ["a", "b", "c", "d"])
    loose = _spec("dub techno | idm", "dub techno", ["a", "b", "y", "z"])
    live = {"techno": {"a", "b", "c", "d"}}

    renames, unmatched = match_by_contents([close, loose], live)

    assert renames == [Rename(old="techno", new="hard techno | tekno")]
    assert [s.title for s in unmatched] == ["dub techno | idm"]


def test_a_playlist_already_correctly_named_is_not_renamed_or_reclaimed():
    """Its tracklist may have been re-sequenced out of recognition; the
    name is proof enough, and nothing else may take it."""
    keeper = _spec("shoegaze | dream pop", "shoegaze", ["m", "n"])
    other = _spec("slowcore, quietly", "slowcore", ["m", "n", "o"])
    live = {"shoegaze | dream pop": {"totally", "different", "ids"}}

    renames, unmatched = match_by_contents([keeper, other], live)

    assert renames == []
    assert [s.title for s in unmatched] == ["slowcore, quietly"]


def test_a_faint_overlap_is_not_a_match():
    spec = _spec("bebop at 3am", "bebop", ["a", "b", "c", "d", "e", "f"])
    live = {"the reptile house": {"a", "z1", "z2", "z3", "z4", "z5"}}

    renames, unmatched = match_by_contents([spec], live)

    assert renames == []
    assert [s.title for s in unmatched] == ["bebop at 3am"]


def test_an_empty_discovery_spec_never_claims_a_live_playlist():
    """A spec waiting on pins has no tracks to match on, and must not
    match a live playlist by having nothing in common with it."""
    spec = _spec("gabber for the long room", "gabber", [])
    live = {"strictly gabber": {"a", "b"}}

    renames, unmatched = match_by_contents([spec], live)

    assert renames == []
    assert [s.title for s in unmatched] == ["gabber for the long room"]


def test_matching_is_deterministic_so_a_dry_run_predicts_the_apply():
    specs = [
        _spec("one", "a", ["x", "y", "z"]),
        _spec("two", "b", ["x", "y", "z"]),
    ]
    live = {"live a": {"x", "y", "z"}, "live b": {"x", "y", "z"}}

    first = match_by_contents(specs, live)[0]
    again = match_by_contents(list(reversed(specs)), dict(reversed(list(live.items()))))[0]

    assert first == again


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
