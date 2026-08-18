"""Tests for curator discovery.

Runs against the FakeSpotify backend through tekore's real client stack,
so playlist search, ownership filtering and overlap counting are all
exercised as they would be against Spotify.
"""

from __future__ import annotations

import pytest
import tekore as tk

from spotifyforge.core.curators import find_curators, top_genres


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


def _library(fake, count: int = 10) -> set[str]:
    """Seed *count* tracks and return their ids as the user's liked set."""
    fake.add_user("user1")
    ids = []
    for i in range(count):
        fake.add_track(f"t{i}", name=f"Zeuhl Song {i}")
        fake.save_track("user1", f"t{i}")
        ids.append(f"t{i}")
    return set(ids)


async def test_finds_a_curator_sharing_liked_tracks(fake_spotify, client_for):
    liked = _library(fake_spotify)
    fake_spotify.add_user("curator", display_name="Niche Person")
    fake_spotify.add_playlist(
        "pl1", owner="curator", name="Zeuhl deep cuts", track_ids=["t1", "t2", "t3"]
    )

    found = await find_curators(client_for("user1"), ["Zeuhl"], liked, me="user1")

    assert len(found) == 1
    curator = found[0]
    assert curator.user_id == "curator"
    assert curator.display_name == "Niche Person"
    assert curator.shared_tracks == 3
    assert curator.example_playlist == "Zeuhl deep cuts"
    assert curator.url == "https://open.spotify.com/user/curator"


async def test_excludes_the_user_and_spotify_itself(fake_spotify, client_for):
    liked = _library(fake_spotify)
    fake_spotify.add_user("spotify", display_name="Spotify")
    fake_spotify.add_playlist("mine", owner="user1", name="Zeuhl mine", track_ids=["t1", "t2"])
    fake_spotify.add_playlist(
        "editorial", owner="spotify", name="Zeuhl official", track_ids=["t1", "t2"]
    )

    found = await find_curators(client_for("user1"), ["Zeuhl"], liked, me="user1")
    assert found == []


async def test_ignores_playlists_with_no_overlap(fake_spotify, client_for):
    liked = _library(fake_spotify)
    fake_spotify.add_user("stranger")
    fake_spotify.add_track("other", name="Zeuhl Song other")  # not liked
    fake_spotify.add_playlist("pl", owner="stranger", name="Zeuhl unrelated", track_ids=["other"])

    found = await find_curators(client_for("user1"), ["Zeuhl"], liked, me="user1")
    assert found == []


async def test_ranks_by_overlap_and_keeps_the_best_example(fake_spotify, client_for):
    liked = _library(fake_spotify)
    fake_spotify.add_user("small")
    fake_spotify.add_user("big")
    fake_spotify.add_playlist("s1", owner="small", name="Zeuhl a", track_ids=["t1"])
    fake_spotify.add_playlist("b1", owner="big", name="Zeuhl b", track_ids=["t1", "t2"])
    fake_spotify.add_playlist(
        "b2", owner="big", name="Zeuhl best", track_ids=["t1", "t2", "t3", "t4"]
    )

    found = await find_curators(client_for("user1"), ["Zeuhl"], liked, me="user1")

    assert [c.user_id for c in found] == ["big", "small"]
    assert found[0].shared_tracks == 4
    assert found[0].example_playlist == "Zeuhl best"  # the strongest match, not the last
    assert found[0].playlists_seen == 2


async def test_limit_caps_the_shortlist(fake_spotify, client_for):
    liked = _library(fake_spotify)
    for i in range(5):
        fake_spotify.add_user(f"c{i}")
        fake_spotify.add_playlist(f"p{i}", owner=f"c{i}", name=f"Zeuhl {i}", track_ids=["t1"])

    found = await find_curators(client_for("user1"), ["Zeuhl"], liked, me="user1", limit=2)
    assert len(found) == 2


async def test_a_failing_genre_search_does_not_sink_the_run(fake_spotify, client_for):
    liked = _library(fake_spotify)
    fake_spotify.add_user("curator")
    fake_spotify.add_playlist("pl", owner="curator", name="Zeuhl good", track_ids=["t1"])

    # "" produces no search words in the fake, standing in for a dud query.
    found = await find_curators(client_for("user1"), ["", "Zeuhl"], liked, me="user1")
    assert [c.user_id for c in found] == ["curator"]


def test_top_genres_skips_catch_all_labels():
    class T:
        def __init__(self, genres):
            self.genres = genres

    tracks = [T(("pop", "zeuhl")) for _ in range(10)]
    tracks += [T(("rock", "coldwave")) for _ in range(8)]
    tracks += [T(("dungeon synth",)) for _ in range(3)]

    assert top_genres(tracks, count=5) == ["zeuhl", "coldwave", "dungeon synth"]
