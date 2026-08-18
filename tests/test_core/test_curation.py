"""Tests for the curation engine (liked-songs → niche playlist catalogue).

Library fetching and genre enrichment run against the FakeSpotify backend
through the real tekore stack; clustering and flow ordering are pure
functions tested directly.
"""

from __future__ import annotations

import html
from dataclasses import replace

import pytest
import tekore as tk
from sqlmodel import Session, select

from spotifyforge.core.audio_features import AudioFeature
from spotifyforge.core.curation import (
    CurationEngine,
    CurationOptions,
    CurationTrack,
    apply_descriptions,
    cluster_library,
    dedupe_versions,
    forge_next,
    order_for_flow,
    plan_catalogue,
    reflow,
)
from spotifyforge.models.models import Playlist


def _ct(
    track_id: str,
    *,
    genres: tuple[str, ...] = ("indie",),
    popularity: int = 50,
    year: int | None = 2020,
    artist: str = "a1",
    name: str | None = None,
) -> CurationTrack:
    return CurationTrack(
        id=track_id,
        uri=f"spotify:track:{track_id}",
        name=name or f"Track {track_id}",
        artist_ids=(artist,),
        artist_names=(artist,),
        release_year=year,
        popularity=popularity,
        genres=genres,
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


# ---------------------------------------------------------------------------
# Fetching + enrichment (through the real tekore stack)
# ---------------------------------------------------------------------------


async def test_fetch_liked_paginates_full_library(fake_spotify, client_for):
    fake_spotify.add_user("user1")
    for i in range(120):
        fake_spotify.add_track(f"t{i}", artist_id=f"art{i % 7}", artist_name=f"Artist {i % 7}")
        fake_spotify.save_track("user1", f"t{i}")

    engine = CurationEngine(client_for("user1"))
    tracks = await engine.fetch_liked()

    assert len(tracks) == 120
    assert tracks[0].id == "t0"
    assert tracks[0].uri == "spotify:track:t0"
    # 120 liked tracks at 50/page = 3 GET /v1/me/tracks calls.
    assert len([r for r in fake_spotify.requests if r == ("GET", "/v1/me/tracks")]) == 3


async def test_fetch_liked_respects_max_tracks(fake_spotify, client_for):
    fake_spotify.add_user("user1")
    for i in range(80):
        fake_spotify.add_track(f"t{i}")
        fake_spotify.save_track("user1", f"t{i}")

    tracks = await CurationEngine(client_for("user1")).fetch_liked(max_tracks=30)
    assert len(tracks) == 30


async def test_enrich_genres_batches_artists(fake_spotify, client_for):
    fake_spotify.add_user("user1")
    for i in range(60):
        fake_spotify.add_track(f"t{i}", artist_id=f"art{i}", artist_name=f"Artist {i}")
        fake_spotify.save_track("user1", f"t{i}")
        fake_spotify.set_artist_genres(f"art{i}", ["slowcore"] if i % 2 else ["zeuhl"])

    engine = CurationEngine(client_for("user1"))
    tracks = await engine.enrich_genres(await engine.fetch_liked())

    assert tracks[0].genres == ("zeuhl",)
    assert tracks[1].genres == ("slowcore",)
    # 60 unique artists at 50/batch = 2 GET /v1/artists calls.
    assert len([r for r in fake_spotify.requests if r == ("GET", "/v1/artists")]) == 2


async def test_enrich_keeps_empty_genres_empty(fake_spotify, client_for):
    fake_spotify.add_user("user1")
    fake_spotify.add_track("t1", artist_id="obscure")
    fake_spotify.save_track("user1", "t1")
    fake_spotify.set_artist_genres("obscure", [])

    engine = CurationEngine(client_for("user1"))
    tracks = await engine.enrich_genres(await engine.fetch_liked())
    assert tracks[0].genres == ()


# ---------------------------------------------------------------------------
# Version de-duplication (pure)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "variant",
    [
        "Bocanada - Remasterizado 2007",
        "Bocanada - 2011 Remaster",
        "Bocanada - Radio Edit",
        "Bocanada - Live at Luna Park",
        "Bocanada (Remastered)",
    ],
)
def test_dedupe_collapses_edition_variants(variant):
    # The real defect this fixes: "Lamento Boliviano" landed twice in a
    # forged playlist as two different track IDs of the same song.
    tracks = [
        _ct("orig", name="Bocanada", popularity=66, artist="cerati"),
        _ct("variant", name=variant, popularity=40, artist="cerati"),
    ]
    kept = dedupe_versions(tracks)
    assert [t.id for t in kept] == ["orig"]


def test_dedupe_keeps_the_most_popular_version():
    tracks = [
        _ct("quiet", name="Bocanada", popularity=12, artist="cerati"),
        _ct("loud", name="Bocanada - Remaster", popularity=88, artist="cerati"),
    ]
    assert [t.id for t in dedupe_versions(tracks)] == ["loud"]


def test_dedupe_distinguishes_same_title_by_different_artists():
    tracks = [
        _ct("a", name="Crazy", artist="Gnarls"),
        _ct("b", name="Crazy", artist="Patsy"),
    ]
    assert len(dedupe_versions(tracks)) == 2


def test_dedupe_keeps_genuinely_different_songs():
    tracks = [_ct(f"t{i}", name=f"Song {i}") for i in range(5)]
    assert len(dedupe_versions(tracks)) == 5


def test_dedupe_keeps_distinct_cjk_titles_by_one_artist():
    # An ASCII-only normaliser flattens both of these to "" and collapses
    # the artist's whole catalogue into a single track.
    tracks = [
        _ct("a", name="タイム・リミット", artist="CASIOPEA"),
        _ct("b", name="ティアーズ・オブ・ザ・スター", artist="CASIOPEA"),
    ]
    assert len(dedupe_versions(tracks)) == 2


def test_dedupe_still_collapses_cjk_remasters():
    tracks = [
        _ct("a", name="四面道歌", artist="Hosono", popularity=50),
        _ct("b", name="四面道歌 - 2019 Remastering", artist="Hosono", popularity=10),
    ]
    assert [t.id for t in dedupe_versions(tracks)] == ["a"]


@pytest.mark.parametrize("suffix", ["(Reprise)", "(continued)", "(Part 2)"])
def test_dedupe_keeps_non_edition_parentheticals(suffix):
    tracks = [
        _ct("a", name="Skiptracing", artist="mhc"),
        _ct("b", name=f"Skiptracing {suffix}", artist="mhc"),
    ]
    assert len(dedupe_versions(tracks)) == 2


def test_dedupe_then_cluster_drops_the_duplicate_from_the_playlist():
    tracks = [_ct(f"t{i}", name=f"Song {i}", genres=("noise",)) for i in range(12)]
    tracks += [_ct("dupe", name="Song 0 - Remastered 2020", genres=("noise",), popularity=1)]
    specs = cluster_library(dedupe_versions(tracks), min_size=10)
    assert len(specs[0].tracks) == 12


# ---------------------------------------------------------------------------
# Clustering (pure)
# ---------------------------------------------------------------------------


def test_exclusive_assigns_track_to_rarest_genre():
    # 20 plain rock tracks, plus 15 tagged both rock and slowcore:
    # the dual-tagged ones must land in slowcore (the rarer label).
    tracks = [_ct(f"r{i}", genres=("rock",)) for i in range(20)]
    tracks += [_ct(f"s{i}", genres=("rock", "slowcore")) for i in range(15)]

    specs = cluster_library(tracks, min_size=10, exclusive=True)
    by_genre = {s.genre: s for s in specs}

    assert set(by_genre) == {"rock", "slowcore"}
    assert len(by_genre["slowcore"].tracks) == 15
    assert len(by_genre["rock"].tracks) == 20


def test_overlap_puts_a_track_in_every_viable_genre():
    tracks = [_ct(f"r{i}", genres=("rock",)) for i in range(20)]
    tracks += [_ct(f"s{i}", genres=("rock", "slowcore")) for i in range(15)]

    by_genre = {s.genre: s for s in cluster_library(tracks, min_size=10)}

    assert len(by_genre["slowcore"].tracks) == 15
    assert len(by_genre["rock"].tracks) == 35  # all of them carry "rock"


def test_cluster_falls_back_to_next_rarest_viable_genre():
    # "microgenre" only has 4 tracks (below min_size) — its tracks must
    # fall back to "rock" rather than being stranded.
    tracks = [_ct(f"r{i}", genres=("rock",)) for i in range(12)]
    tracks += [_ct(f"m{i}", genres=("rock", "microgenre")) for i in range(4)]

    specs = cluster_library(tracks, min_size=10)
    assert [s.genre for s in specs] == ["rock"]
    assert len(specs[0].tracks) == 16


def test_cluster_drops_undersized_genres_and_untagged_tracks():
    tracks = [_ct(f"a{i}", genres=("ambient",)) for i in range(15)]
    tracks += [_ct(f"b{i}", genres=("bit-music",)) for i in range(3)]
    tracks += [_ct("nogenre", genres=())]

    specs = cluster_library(tracks, min_size=10, include_unclassified=False)
    assert [s.genre for s in specs] == ["ambient"]


def test_unclassified_tracks_become_their_own_playlist():
    tracks = [_ct(f"a{i}", genres=("ambient",)) for i in range(15)]
    tracks += [_ct(f"u{i}", genres=(), year=1998) for i in range(12)]

    specs = cluster_library(tracks, min_size=10)
    unclassified = [s for s in specs if s.genre is None]

    assert len(unclassified) == 1
    spec = unclassified[0]
    assert len(spec.tracks) == 12
    assert spec.decade == 1990
    assert spec.title == "beyond genre ('90s)"
    assert "never tagged" in spec.description
    assert spec.genre_label == "unclassified"


def test_unclassified_can_be_disabled():
    tracks = [_ct(f"a{i}", genres=("ambient",)) for i in range(15)]
    tracks += [_ct(f"u{i}", genres=()) for i in range(12)]

    specs = cluster_library(tracks, min_size=10, include_unclassified=False)
    assert all(s.genre is not None for s in specs)


def test_unclassified_collects_tracks_whose_genres_are_all_too_rare():
    tracks = [_ct(f"a{i}", genres=("ambient",)) for i in range(15)]
    # Two genres too rare to be viable on their own (6 and 7 tracks),
    # but together they fill a playlist of orphans.
    tracks += [_ct(f"x{i}", genres=("ultrarare",), year=2015) for i in range(6)]
    tracks += [_ct(f"y{i}", genres=("evenrarer",), year=2015) for i in range(7)]

    specs = cluster_library(tracks, min_size=12)
    unclassified = [s for s in specs if s.genre is None]
    assert len(unclassified) == 1
    assert len(unclassified[0].tracks) == 13


def test_cluster_splits_oversized_genre_by_decade():
    tracks = [_ct(f"n{i}", genres=("shoegaze",), year=1991) for i in range(50)]
    tracks += [_ct(f"m{i}", genres=("shoegaze",), year=2021) for i in range(50)]

    specs = cluster_library(tracks, min_size=10, max_size=80)
    assert {s.decade for s in specs} == {1990, 2020}
    assert all("'" in s.title for s in specs)  # era shows in the name
    assert all(len(s.tracks) == 50 for s in specs)


def test_cluster_titles_are_deterministic():
    tracks = [_ct(f"t{i}", genres=("dungeon synth",)) for i in range(20)]
    first = cluster_library(tracks, min_size=10)[0].title
    second = cluster_library(list(reversed(tracks)), min_size=10)[0].title
    assert first == second
    assert "dungeon synth" in first


# ---------------------------------------------------------------------------
# Flow ordering (pure)
# ---------------------------------------------------------------------------


def test_flow_without_features_builds_popularity_arc():
    tracks = [_ct(f"t{i}", popularity=i * 10, artist=f"a{i}") for i in range(10)]
    ordered = order_for_flow(tracks)

    assert sorted(t.id for t in ordered) == sorted(t.id for t in tracks)
    pops = [t.popularity for t in ordered]
    # Opens on the most popular track, buries the deepest cut mid-list,
    # and resurfaces (last track more popular than the middle).
    assert pops[0] == max(pops)
    trough = pops.index(min(pops))
    assert 0 < trough < len(pops) - 1
    assert pops[-1] > min(pops)


def test_flow_spaces_artists_even_when_one_dominates_the_arc():
    # The measured defect: the popularity arc grouped an artist's tracks
    # together and the old swap pass left most of them adjacent. Here the
    # top 8 tracks are all one artist, so a naive arc puts them in a run.
    tracks = [_ct(f"d{i}", popularity=99 - i, artist="dominant") for i in range(8)]
    tracks += [_ct(f"o{i}", popularity=50 - i, artist=f"other{i}") for i in range(8)]

    ordered = order_for_flow(tracks)
    adjacent = sum(
        1
        for a, b in zip(ordered, ordered[1:], strict=False)
        if set(a.artist_ids) & set(b.artist_ids)
    )
    assert adjacent == 0
    assert len(ordered) == 16


def test_flow_tolerates_a_playlist_with_only_one_artist():
    # No ordering can avoid adjacency here; it must not hang or drop tracks.
    tracks = [_ct(f"s{i}", popularity=i, artist="solo") for i in range(6)]
    ordered = order_for_flow(tracks)
    assert sorted(t.id for t in ordered) == sorted(t.id for t in tracks)


def test_flow_spaces_out_same_artist_runs():
    tracks = [_ct(f"x{i}", popularity=90 - i, artist="same") for i in range(3)]
    tracks += [_ct(f"y{i}", popularity=60 - i, artist=f"other{i}") for i in range(5)]
    ordered = order_for_flow(tracks)

    adjacent_same = sum(
        1
        for a, b in zip(ordered, ordered[1:], strict=False)
        if set(a.artist_ids) & set(b.artist_ids)
    )
    assert adjacent_same == 0


def test_flow_preserves_track_set_and_handles_tiny_lists():
    two = [_ct("a"), _ct("b")]
    assert order_for_flow(two) == two
    assert order_for_flow([]) == []


# ---------------------------------------------------------------------------
# End-to-end: liked songs -> forged playlist on Spotify + local DB
# ---------------------------------------------------------------------------


async def test_forged_playlist_lands_on_spotify_and_in_db(fake_spotify, client_for, isolated_db):
    from spotifyforge.core.playlist_manager import PlaylistManager
    from spotifyforge.db.engine import get_engine
    from spotifyforge.models.models import User

    fake_spotify.add_user("user1")
    for i in range(20):
        fake_spotify.add_track(f"t{i}", artist_id=f"art{i % 4}", artist_name=f"Artist {i % 4}")
        fake_spotify.save_track("user1", f"t{i}")
        fake_spotify.set_artist_genres(f"art{i % 4}", ["coldwave"])

    with Session(get_engine()) as session:
        user = User(spotify_id="user1")
        session.add(user)
        session.commit()
        session.refresh(user)
        owner_id = user.id

    sp = client_for("user1")
    engine = CurationEngine(sp)
    tracks = await engine.enrich_genres(await engine.fetch_liked())
    specs = cluster_library(tracks, min_size=10)
    assert len(specs) == 1

    spec = specs[0]
    playlist = await PlaylistManager(sp).create_playlist_with_tracks(
        name=spec.title,
        owner_id=owner_id,
        tracks=spec.tracks,
        description=spec.description,
    )

    # On the fake Spotify: playlist exists with all 20 tracks in spec order.
    assert fake_spotify.playlists[playlist.spotify_id]["name"] == spec.title
    assert fake_spotify.playlist_tracks[playlist.spotify_id] == [t.id for t in spec.tracks]

    # In the local DB: the Playlist row was persisted.
    with Session(get_engine()) as session:
        row = session.exec(select(Playlist).where(Playlist.spotify_id == playlist.spotify_id)).one()
        assert row.name == spec.title
        assert row.owner_id == owner_id


# ---------------------------------------------------------------------------
# Orchestration: plan -> forge -> reflow, against the fake backend
# ---------------------------------------------------------------------------


def _seed_library(fake, *, users=("user1",), genres=("coldwave", "minimal wave"), count=40):
    """Seed *count* liked tracks split across two genres and four artists."""
    for user in users:
        fake.add_user(user)
        for i in range(count):
            fake.add_track(
                f"t{i}",
                name=f"Song {i}",
                popularity=(i * 7) % 100,
                artist_id=f"art{i % 4}",
                artist_name=f"Artist {i % 4}",
                album_id=f"alb{i}",
                release_date=f"{1990 + (i % 3) * 10}-01-01",
            )
            fake.save_track(user, f"t{i}")
    for i in range(4):
        fake.set_artist_genres(f"art{i}", [genres[i % len(genres)]])


def _db_user(spotify_id="user1") -> int:
    from spotifyforge.db.engine import get_engine
    from spotifyforge.models.models import User

    with Session(get_engine()) as session:
        user = User(spotify_id=spotify_id)
        session.add(user)
        session.commit()
        session.refresh(user)
        return user.id


async def test_plan_catalogue_reports_collapse_and_placement(fake_spotify, client_for):
    _seed_library(fake_spotify, count=40)
    # One extra remaster of Song 0 that must be collapsed away.
    fake_spotify.add_track(
        "dupe", name="Song 0 - Remastered 2019", artist_id="art0", artist_name="Artist 0"
    )
    fake_spotify.save_track("user1", "dupe")

    plan = await plan_catalogue(client_for("user1"), CurationOptions(min_size=10))

    assert plan.liked_count == 41
    assert plan.unique_count == 40
    assert plan.collapsed_count == 1
    assert plan.specs
    assert plan.placed_count <= plan.unique_count
    assert plan.entry_count == sum(len(s.tracks) for s in plan.specs)


async def test_plan_splits_a_large_genre_by_decade(fake_spotify, client_for):
    # All 60 tracks share one genre, spread over three decades, so the
    # genre exceeds max_size and must split into per-decade playlists.
    _seed_library(fake_spotify, genres=("coldwave",), count=60)

    plan = await plan_catalogue(client_for("user1"), CurationOptions(min_size=10, max_size=30))

    decades = {s.decade for s in plan.specs if s.genre == "coldwave"}
    assert decades == {1990, 2000, 2010}
    assert all(len(s.tracks) <= 30 for s in plan.specs if s.decade)


async def test_forge_next_is_resumable_and_skips_existing(fake_spotify, client_for, isolated_db):
    from spotifyforge.core.playlist_manager import PlaylistManager

    _seed_library(fake_spotify, count=40)
    owner_id = _db_user()
    sp = client_for("user1")
    manager = PlaylistManager(sp)
    plan = await plan_catalogue(sp, CurationOptions(min_size=10))
    assert len(plan.specs) >= 2

    created, pending = await forge_next(manager, owner_id, plan.specs, limit=1, delay=0)
    assert len(created) == 1
    assert pending == len(plan.specs)

    # Second run must not recreate the first playlist.
    created2, pending2 = await forge_next(manager, owner_id, plan.specs, limit=1, delay=0)
    assert len(created2) == 1
    assert pending2 == len(plan.specs) - 1
    assert created2[0][0].title != created[0][0].title

    names = [p["name"] for p in await manager.get_user_playlists()]
    assert len(names) == len(set(names))  # no duplicates created


async def test_forge_next_reports_nothing_pending_when_drained(
    fake_spotify, client_for, isolated_db
):
    from spotifyforge.core.playlist_manager import PlaylistManager

    _seed_library(fake_spotify, count=40)
    owner_id = _db_user()
    sp = client_for("user1")
    manager = PlaylistManager(sp)
    plan = await plan_catalogue(sp, CurationOptions(min_size=10))

    await forge_next(manager, owner_id, plan.specs, limit=99, delay=0)
    created, pending = await forge_next(manager, owner_id, plan.specs, limit=99, delay=0)

    assert created == []
    assert pending == 0


async def test_reflow_rewrites_a_scrambled_playlist_in_place(fake_spotify, client_for, isolated_db):
    from spotifyforge.core.playlist_manager import PlaylistManager

    _seed_library(fake_spotify, count=40)
    owner_id = _db_user()
    sp = client_for("user1")
    manager = PlaylistManager(sp)
    plan = await plan_catalogue(sp, CurationOptions(min_size=10))
    created, _ = await forge_next(manager, owner_id, plan.specs, limit=99, delay=0)

    spec, playlist = created[0]
    wanted = [t.id for t in spec.tracks]
    # Scramble the playlist behind the tool's back.
    fake_spotify.playlist_tracks[playlist.spotify_id] = list(reversed(wanted))

    rewritten, failed = await reflow(manager, sp, plan.specs, delay=0)

    assert failed == []
    assert [title for title, _ in rewritten] == [spec.title]
    assert fake_spotify.playlist_tracks[playlist.spotify_id] == wanted
    # The playlist kept its identity — same id, not recreated.
    assert playlist.spotify_id in fake_spotify.playlists


async def test_reflow_is_a_no_op_when_order_already_matches(fake_spotify, client_for, isolated_db):
    from spotifyforge.core.playlist_manager import PlaylistManager

    _seed_library(fake_spotify, count=40)
    owner_id = _db_user()
    sp = client_for("user1")
    manager = PlaylistManager(sp)
    plan = await plan_catalogue(sp, CurationOptions(min_size=10))
    await forge_next(manager, owner_id, plan.specs, limit=99, delay=0)

    assert await reflow(manager, sp, plan.specs, delay=0) == ([], [])


async def test_reflow_handles_playlists_longer_than_one_request(
    fake_spotify, client_for, isolated_db
):
    from spotifyforge.core.playlist_manager import PlaylistManager

    # 150 tracks in one genre exceeds the 100-URI replace limit.
    _seed_library(fake_spotify, genres=("coldwave",), count=150)
    owner_id = _db_user()
    sp = client_for("user1")
    manager = PlaylistManager(sp)
    plan = await plan_catalogue(sp, CurationOptions(min_size=10, max_size=200))
    created, _ = await forge_next(manager, owner_id, plan.specs, limit=99, delay=0)

    spec, playlist = created[0]
    assert len(spec.tracks) > 100
    fake_spotify.playlist_tracks[playlist.spotify_id] = []

    await reflow(manager, sp, plan.specs, delay=0)
    assert fake_spotify.playlist_tracks[playlist.spotify_id] == [t.id for t in spec.tracks]


async def test_reflow_keeps_going_when_one_playlist_fails(fake_spotify, client_for, isolated_db):
    """A timeout on one playlist must not discard hundreds of others."""
    import httpx

    from spotifyforge.core.playlist_manager import PlaylistManager

    _seed_library(fake_spotify, count=40)
    owner_id = _db_user()
    sp = client_for("user1")
    manager = PlaylistManager(sp)
    plan = await plan_catalogue(sp, CurationOptions(min_size=10))
    created, _ = await forge_next(manager, owner_id, plan.specs, limit=99, delay=0)
    assert len(created) >= 2

    for _, playlist in created:
        fake_spotify.playlist_tracks[playlist.spotify_id] = []

    doomed = created[0][0].title
    original_replace = sp.playlist_replace

    async def flaky(playlist_id, uris):
        by_title = {p["name"]: p["id"] for p in await manager.get_user_playlists()}
        if by_title.get(doomed) == playlist_id:
            raise httpx.ReadTimeout("")
        return await original_replace(playlist_id, uris)

    sp.playlist_replace = flaky
    rewritten, failed = await reflow(manager, sp, plan.specs, delay=0)

    assert failed == [doomed]
    assert len(rewritten) == len(created) - 1


def test_catalogue_is_deterministic_regardless_of_library_order():
    # `forge` resumes by matching titles and `reflow` compares track
    # order, so both break if the same library can produce two different
    # catalogues.
    tracks = [
        _ct(f"t{i}", name=f"Song {i}", genres=("coldwave",), popularity=(i * 7) % 100)
        for i in range(30)
    ]
    first = cluster_library(tracks, min_size=10)
    second = cluster_library(list(reversed(tracks)), min_size=10)

    assert [s.title for s in first] == [s.title for s in second]
    assert [[t.id for t in s.tracks] for s in first] == [[t.id for t in s.tracks] for s in second]


# ---------------------------------------------------------------------------
# Harmonic sequencing (tempo/key aware)
# ---------------------------------------------------------------------------


def _keyed(track_id, *, key, mode, tempo, popularity=50, artist="a1"):
    return _ct(track_id, popularity=popularity, artist=artist), AudioFeature(
        tempo=tempo, key=key, mode=mode
    )


def test_flow_chains_by_key_when_features_are_available():
    # 8A (A minor) -> 8B (C major, relative) -> 9A (E minor) are all
    # compatible moves; 2B (F# major) is the far side of the wheel and
    # must be visited last.
    pairs = [
        _keyed("amin", key=9, mode=0, tempo=120, popularity=99, artist="w"),
        _keyed("cmaj", key=0, mode=1, tempo=121, popularity=50, artist="x"),
        _keyed("emin", key=4, mode=0, tempo=122, popularity=40, artist="y"),
        _keyed("far", key=6, mode=1, tempo=120, popularity=60, artist="z"),
    ]
    tracks = [replace(t, isrc=t.id) for t, _ in pairs]
    features = {t.id: f for t, (_, f) in zip(tracks, pairs, strict=True)}

    ordered = order_for_flow(tracks, features)

    assert ordered[0].id == "amin"  # most popular opens
    assert ordered[-1].id == "far"  # harmonically distant closes
    assert sorted(t.id for t in ordered) == ["amin", "cmaj", "emin", "far"]


def test_flow_prefers_close_tempo_when_keys_tie():
    tracks, features = [], {}
    for tid, tempo, pop in [("start", 100.0, 99), ("near", 102.0, 10), ("far", 160.0, 60)]:
        t = replace(_ct(tid, popularity=pop, artist=tid), isrc=tid)
        tracks.append(t)
        features[tid] = AudioFeature(tempo=tempo, key=9, mode=0)  # identical keys

    ordered = order_for_flow(tracks, features)
    assert [t.id for t in ordered] == ["start", "near", "far"]


def test_harmonic_ordering_still_refuses_back_to_back_artists():
    # Two tracks by one artist share a key; a purely harmonic chain would
    # put them together. Artist separation has to win.
    tracks, features = [], {}
    for tid, artist, pop in [("s1", "same", 99), ("s2", "same", 98), ("o1", "other", 10)]:
        t = replace(_ct(tid, popularity=pop, artist=artist), isrc=tid)
        tracks.append(t)
        features[tid] = AudioFeature(tempo=120.0, key=9, mode=0)

    ordered = order_for_flow(tracks, features)
    assert [t.artist_ids[0] for t in ordered] == ["same", "other", "same"]


def test_flow_falls_back_to_the_arc_when_too_little_is_analysed():
    # Only one of six tracks has a key — below the coverage floor, so the
    # popularity arc must be used instead of a mostly-guessed chain.
    tracks = [
        replace(_ct(f"t{i}", popularity=i * 10, artist=f"a{i}"), isrc=f"t{i}") for i in range(6)
    ]
    features = {"t0": AudioFeature(tempo=120.0, key=9, mode=0)}

    ordered = order_for_flow(tracks, features)
    assert ordered[0].popularity == max(t.popularity for t in tracks)


def test_flow_ignores_features_that_match_no_track():
    tracks = [replace(_ct(f"t{i}", popularity=i, artist=f"a{i}"), isrc=f"t{i}") for i in range(5)]
    ordered = order_for_flow(tracks, {"unrelated": AudioFeature(tempo=99.0, key=1, mode=1)})
    assert sorted(t.id for t in ordered) == sorted(t.id for t in tracks)


async def test_isrc_is_carried_off_spotify_into_curation_tracks(fake_spotify, client_for):
    fake_spotify.add_user("user1")
    fake_spotify.add_track("t1")
    fake_spotify.save_track("user1", "t1")

    tracks = await CurationEngine(client_for("user1")).fetch_liked()
    # The fake mints ISRCs the same shape Spotify does; without this the
    # whole tempo/key lookup has nothing to key on.
    assert tracks[0].isrc == "ISRCT1"


async def test_library_read_retries_a_transient_failure(fake_spotify, client_for, monkeypatch):
    """A timeout on one page must not silently shrink the library."""
    import httpx

    monkeypatch.setattr("spotifyforge.core.curation._READ_BACKOFF", 0)
    fake_spotify.add_user("user1")
    for i in range(120):
        fake_spotify.add_track(f"t{i}")
        fake_spotify.save_track("user1", f"t{i}")

    sp = client_for("user1")
    calls = {"n": 0}
    real = sp.saved_tracks

    async def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise httpx.ReadTimeout("")
        return await real(*a, **kw)

    sp.saved_tracks = flaky
    tracks = await CurationEngine(sp).fetch_liked()

    assert len(tracks) == 120  # nothing lost to the blip
    assert len({t.id for t in tracks}) == 120


async def test_library_read_raises_rather_than_returning_a_partial_library(
    fake_spotify, client_for, monkeypatch
):
    """Silently dropping a page would make reflow delete real tracks."""
    import httpx

    monkeypatch.setattr("spotifyforge.core.curation._READ_BACKOFF", 0)
    fake_spotify.add_user("user1")
    for i in range(120):
        fake_spotify.add_track(f"t{i}")
        fake_spotify.save_track("user1", f"t{i}")

    sp = client_for("user1")
    real = sp.saved_tracks

    async def always_fails(*a, **kw):
        if kw.get("offset"):
            raise httpx.ReadTimeout("")
        return await real(*a, **kw)

    sp.saved_tracks = always_fails
    with pytest.raises(httpx.ReadTimeout):
        await CurationEngine(sp).fetch_liked()


# ---------------------------------------------------------------------------
# Descriptions
# ---------------------------------------------------------------------------


def test_description_reads_human_and_names_the_biggest_artists():
    tracks = [
        _ct(f"t{i}", genres=("zeuhl",), popularity=90 - i, artist=f"Band {i}") for i in range(12)
    ]
    (spec,) = cluster_library(tracks, min_size=10)

    # Leads with the most popular artists — the terms searchers type.
    assert "Band 0" in spec.description
    assert "Band 1" in spec.description
    assert "zeuhl" in spec.description
    # No bot signature, no stale-able track count.
    assert "SpotifyForge" not in spec.description
    assert "liked songs" not in spec.description
    assert "12 " not in spec.description
    # Deterministic: the same library always yields the same text.
    (again,) = cluster_library(list(reversed(tracks)), min_size=10)
    assert again.description == spec.description


def test_description_mentions_the_era_on_decade_splits():
    tracks = [
        _ct(f"n{i}", genres=("dungeon synth",), year=1994, artist=f"N{i}") for i in range(12)
    ] + [_ct(f"m{i}", genres=("dungeon synth",), year=2004, artist=f"M{i}") for i in range(12)]

    specs = cluster_library(tracks, min_size=10, max_size=12)
    nineties = next(s for s in specs if s.decade == 1990)

    assert "all from the 1990s." in nineties.description


def test_description_notes_harmonic_sequencing():
    pairs = [
        _keyed("h1", key=9, mode=0, tempo=120, popularity=99, artist="w"),
        _keyed("h2", key=0, mode=1, tempo=121, popularity=50, artist="x"),
        _keyed("h3", key=4, mode=0, tempo=122, popularity=40, artist="y"),
        _keyed("h4", key=6, mode=1, tempo=120, popularity=60, artist="z"),
    ]
    tracks = [replace(t, isrc=t.id) for t, _ in pairs]
    features = {t.id: f for t, (_, f) in zip(tracks, pairs, strict=True)}

    (spec,) = cluster_library(tracks, min_size=3, features=features)

    assert spec.ordering == "harmonic"
    assert spec.description.endswith("mixed by key, like a dj set.")


def test_description_stays_within_spotify_limit():
    verbose = "The Extraordinarily Long Ensemble Of The Northern Archipelago Revival "
    tracks = [
        _ct(f"t{i}", genres=("hyperniche revival",), artist=verbose * 3 + str(i)) for i in range(12)
    ]

    (spec,) = cluster_library(tracks, min_size=10)

    assert len(spec.description) <= 300


async def test_apply_descriptions_updates_stale_text_and_skips_current(
    fake_spotify, client_for, isolated_db
):
    from spotifyforge.core.playlist_manager import PlaylistManager

    _seed_library(fake_spotify, count=40)
    owner_id = _db_user()
    sp = client_for("user1")
    manager = PlaylistManager(sp)
    plan = await plan_catalogue(sp, CurationOptions(min_size=10))
    created, _ = await forge_next(manager, owner_id, plan.specs, limit=99, delay=0)

    # Freshly forged playlists already carry the wanted text — even once
    # Spotify hands it back HTML-escaped, as it does live.
    spec, playlist = created[0]
    fake_spotify.playlists[playlist.spotify_id]["description"] = html.escape(spec.description)
    assert await apply_descriptions(manager, sp, plan.specs, delay=0) == ([], [])

    # An out-of-date description is rewritten in place, nothing else.
    tracks_before = list(fake_spotify.playlist_tracks[playlist.spotify_id])
    fake_spotify.playlists[playlist.spotify_id]["description"] = "42 tracks, sequenced for flow."
    updated, failed = await apply_descriptions(manager, sp, plan.specs, delay=0)

    assert failed == []
    assert spec.title in updated
    assert fake_spotify.playlists[playlist.spotify_id]["description"] == spec.description
    assert fake_spotify.playlist_tracks[playlist.spotify_id] == tracks_before
