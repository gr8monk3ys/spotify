"""Tests for curator discovery.

Runs against the FakeSpotify backend through tekore's real client stack,
so playlist search, ownership filtering and overlap counting are all
exercised as they would be against Spotify.
"""

from __future__ import annotations

from spotifyforge.core.curators import find_curators, top_genres


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


async def test_a_playlist_surfacing_in_two_genres_is_read_once(fake_spotify, client_for):
    """Neighbouring genres return overlapping playlists; re-reading one
    costs a round trip and changes no answer."""
    liked = _library(fake_spotify)
    fake_spotify.add_user("curator")
    # Its name matches both search terms.
    fake_spotify.add_playlist(
        "pl", owner="curator", name="Zeuhl Krautrock hybrid", track_ids=["t1", "t2"]
    )

    found = await find_curators(client_for("user1"), ["Zeuhl", "Krautrock"], liked, me="user1")

    reads = [r for r in fake_spotify.requests if r == ("GET", "/v1/playlists/pl/tracks")]
    assert len(reads) == 1
    assert found[0].shared_tracks == 2
    assert found[0].playlists_seen == 1


async def test_an_unreadable_playlist_does_not_sink_the_report(fake_spotify, client_for):
    """Strangers' playlists routinely 404 or are region-locked."""
    import httpx

    liked = _library(fake_spotify)
    fake_spotify.add_user("good")
    fake_spotify.add_playlist("ok", owner="good", name="Zeuhl fine", track_ids=["t1", "t2"])
    fake_spotify.add_playlist("gone", owner="good", name="Zeuhl missing", track_ids=["t3"])

    sp = client_for("user1")
    original = sp.playlist_items

    async def flaky(playlist_id, *a, **kw):
        if playlist_id == "gone":
            raise httpx.ReadTimeout("")
        return await original(playlist_id, *a, **kw)

    sp.playlist_items = flaky
    found = await find_curators(sp, ["Zeuhl"], liked, me="user1")

    assert [c.user_id for c in found] == ["good"]
    assert found[0].shared_tracks == 2  # the readable one still counted


async def test_only_track_ids_are_requested(fake_spotify, client_for):
    """The default payload carries every field of 100 hydrated tracks to
    read one id from each."""
    liked = _library(fake_spotify)
    fake_spotify.add_user("curator")
    fake_spotify.add_playlist("pl", owner="curator", name="Zeuhl x", track_ids=["t1"])

    seen = {}
    original = fake_spotify.handler

    def spy(request):
        if request.url.path == "/v1/playlists/pl/tracks":
            seen.update(dict(request.url.params))
        return original(request)

    fake_spotify.handler = spy
    await find_curators(client_for("user1"), ["Zeuhl"], liked, me="user1")

    assert seen.get("fields") == "items(track(id))"
