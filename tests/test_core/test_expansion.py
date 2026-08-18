"""Tests for playlist expansion (core/expansion.py).

Candidate searches run against the FakeSpotify backend through the real
tekore stack; the sidecar and query building are pure. The fake's track
search matches on words of the query head, so candidate tracks here are
named to contain their genre.
"""

from __future__ import annotations

from spotifyforge.core.curation import (
    CurationOptions,
    CurationTrack,
    PlaylistSpec,
    forge_next,
    merge_expansions,
    plan_catalogue,
    reflow,
)
from spotifyforge.core.expansion import (
    _search_query,
    expand_catalogue,
    load_expansions,
    save_expansions,
)


def _seed_coldwave(fake, count=8):
    """A library that clusters into one thin coldwave playlist."""
    fake.add_user("user1")
    for i in range(count):
        fake.add_track(
            f"lib{i}",
            name=f"Coldwave Song {i}",
            popularity=80 - i,
            artist_id="cold1",
            artist_name="Coldband",
            album_id=f"alb{i}",
            release_date="2020-01-01",
        )
        fake.save_track("user1", f"lib{i}")
    fake.set_artist_genres("cold1", ["coldwave"])


def _seed_candidates(fake):
    """Search fodder: legit picks plus every kind of reject."""
    fake.add_track(
        "c1", name="Coldwave Nugget 1", popularity=30, artist_id="x1", artist_name="Xylo Void"
    )
    fake.add_track(
        "c2", name="Coldwave Nugget 2", popularity=20, artist_id="x1", artist_name="Xylo Void"
    )
    # Third track by the same artist — over the per-artist cap.
    fake.add_track(
        "c3", name="Coldwave Nugget 3", popularity=10, artist_id="x1", artist_name="Xylo Void"
    )
    # Chart-level track — not niche.
    fake.add_track("hot", name="Coldwave Hit", popularity=90, artist_id="x2", artist_name="Big Act")
    # A remaster of a song the library already holds, under a new id.
    fake.add_track(
        "dupe",
        name="Coldwave Song 0 - 2020 Remaster",
        popularity=5,
        artist_id="cold1",
        artist_name="Coldband",
    )
    fake.add_track(
        "c4", name="Coldwave Litany", popularity=25, artist_id="x3", artist_name="Mira Frost"
    )
    fake.add_track(
        "c5", name="Coldwave Vespers", popularity=15, artist_id="x4", artist_name="Nul Choir"
    )


def _spec(genre="coldwave", decade=None, tracks=()) -> PlaylistSpec:
    return PlaylistSpec(
        title="strictly coldwave", description="", genre=genre, decade=decade, tracks=list(tracks)
    )


# ---------------------------------------------------------------------------
# Pure pieces
# ---------------------------------------------------------------------------


def test_search_query_is_genre_filtered_and_era_bounded():
    assert _search_query(_spec()) == 'genre:"coldwave"'
    assert _search_query(_spec(decade=1980)) == 'genre:"coldwave" year:1980-1989'


def test_sidecar_roundtrip(tmp_path):
    path = tmp_path / "expansions.json"
    track = CurationTrack(
        id="c1",
        uri="spotify:track:c1",
        name="Coldwave Nugget 1",
        artist_ids=("x1",),
        artist_names=("Xylo Void",),
        release_year=1984,
        popularity=30,
        isrc="ISRCC1",
        genres=("coldwave",),
    )
    save_expansions({("coldwave", None): [track]}, path)

    assert load_expansions(path) == {("coldwave", None): [track]}
    assert load_expansions(tmp_path / "missing.json") == {}


def test_merge_without_pins_changes_nothing():
    specs = [_spec(tracks=[])]
    assert merge_expansions(specs, None) is specs
    assert merge_expansions(specs, {}) is specs


# ---------------------------------------------------------------------------
# Picking candidates (through the fake backend)
# ---------------------------------------------------------------------------


async def test_expand_picks_unheard_and_rejects_known_hot_and_flooding(
    fake_spotify, client_for, tmp_path
):
    _seed_coldwave(fake_spotify)
    _seed_candidates(fake_spotify)
    path = tmp_path / "expansions.json"
    sp = client_for("user1")

    plan = await plan_catalogue(sp, CurationOptions(min_size=6))
    added, thin = await expand_catalogue(sp, plan.specs, target=12, path=path)

    assert thin == 1
    (title,) = added
    picked = {t.id for t in added[title]}
    # Four slots (8 -> 12): the two capped Xylo Void tracks plus the two
    # other niche finds. The library's own search hits, the third
    # same-artist track, the chart track, and the remaster are rejected.
    assert picked == {"c1", "c2", "c4", "c5"}
    # Pins are keyed by (genre, decade) — the stable identity — not title.
    assert {t.id for t in load_expansions(path)[("coldwave", None)]} == picked
    # Pins carry what ordering and dedupe need later.
    pinned = {t.id: t for t in load_expansions(path)[("coldwave", None)]}
    assert pinned["c1"].genres == ("coldwave",)
    assert pinned["c1"].isrc == "ISRCC1"


async def test_expand_stops_once_pins_reach_target(fake_spotify, client_for, tmp_path):
    _seed_coldwave(fake_spotify)
    _seed_candidates(fake_spotify)
    path = tmp_path / "expansions.json"
    sp = client_for("user1")

    plan = await plan_catalogue(sp, CurationOptions(min_size=6))
    await expand_catalogue(sp, plan.specs, target=12, path=path)

    # The next plan folds the pins in, so the playlist is no longer thin.
    grown = await plan_catalogue(sp, CurationOptions(min_size=6), expansions=load_expansions(path))
    (spec,) = [s for s in grown.specs if s.genre == "coldwave"]
    assert len(spec.tracks) == 12

    added, thin = await expand_catalogue(sp, grown.specs, target=12, path=path)
    assert added == {}
    assert thin == 0


# ---------------------------------------------------------------------------
# End to end: pins survive reflow
# ---------------------------------------------------------------------------


async def test_reflow_pushes_pins_and_keeps_them(
    fake_spotify, client_for, isolated_db, db_user, tmp_path
):
    from spotifyforge.core.playlist_manager import PlaylistManager

    _seed_coldwave(fake_spotify)
    _seed_candidates(fake_spotify)
    path = tmp_path / "expansions.json"
    owner_id = db_user()
    sp = client_for("user1")
    manager = PlaylistManager(sp)

    plan = await plan_catalogue(sp, CurationOptions(min_size=6))
    created, _ = await forge_next(manager, owner_id, plan.specs, limit=99, delay=0)
    (spec, playlist) = created[0]
    assert len(fake_spotify.playlist_tracks[playlist.spotify_id]) == 8

    await expand_catalogue(sp, plan.specs, target=12, path=path)
    grown = await plan_catalogue(sp, CurationOptions(min_size=6), expansions=load_expansions(path))

    rewritten, failed = await reflow(manager, sp, grown.specs, delay=0)
    assert failed == []
    live = fake_spotify.playlist_tracks[playlist.spotify_id]
    assert len(live) == 12
    assert {"c1", "c2", "c4", "c5"} <= set(live)

    # A second reflow of the same grown plan is a no-op — the pins are
    # part of the catalogue now, not something reflow strips back out.
    assert await reflow(manager, sp, grown.specs, delay=0) == ([], [])

    # And the grown description names an unheard artist it can stand on.
    (grown_spec,) = [s for s in grown.specs if s.genre == "coldwave"]
    assert "Coldband" in grown_spec.description
