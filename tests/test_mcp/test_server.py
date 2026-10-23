"""The MCP server, driven through a real MCP client against the fake Spotify.

``mcp.Client(server)`` speaks the protocol to the server in-process, so
tool discovery, argument validation and result encoding are the SDK's
real paths. Spotify is the same in-memory backend the rest of the suite
uses, and the session comes from an in-memory keyring.
"""

from __future__ import annotations

import pytest
import tekore as tk
from mcp import Client

from spotifyforge import mcp_server
from spotifyforge.mcp_server import server


@pytest.fixture()
def logged_in(app_env, tmp_path, monkeypatch, memory_token_store):
    """A logged-in ``user1``: keyring token plus the current-user marker."""
    from spotifyforge.auth import oauth

    fake = app_env
    fake.add_user("user1")
    monkeypatch.setattr(oauth, "KeyringTokenStore", lambda: memory_token_store)
    memory_token_store.save_token("user1", tk.Token(fake.issue_token("user1"), uses_pkce=False))
    (tmp_path / "current_user").write_text("user1", encoding="utf-8")
    monkeypatch.setattr(mcp_server, "_liked_cache", None)
    return fake


def _like(fake, track_id: str, *, artist: str, genres: list[str], **kwargs) -> None:
    fake.add_track(track_id, artist_id=artist, artist_name=artist.title(), **kwargs)
    fake.set_artist_genres(artist, genres)
    fake.save_track("user1", track_id)


async def _call(tool: str, **arguments):
    async with Client(server) as client:
        result = await client.call_tool(tool, arguments)
    return result


async def _ok(tool: str, **arguments) -> dict:
    result = await _call(tool, **arguments)
    assert not result.is_error, result.content
    return result.structured_content


async def test_lists_the_tools_with_read_only_hints():
    async with Client(server) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}

    assert {"liked_genres", "search_liked", "create_playlist", "plan_catalogue_preview"} <= set(
        tools
    )
    assert tools["search_liked"].annotations.read_only_hint is True
    # Every tool returns text written by other people, local log included.
    assert all(tool.annotations.open_world_hint is not False for tool in tools.values())
    assert tools["create_playlist"].annotations.read_only_hint is False
    # The bulk catalogue writers stay in the CLI.
    assert not {"forge", "reflow", "retire"} & set(tools)


async def test_not_logged_in_tells_the_model_what_the_user_must_do(app_env):
    result = await _call("liked_genres")

    assert result.is_error
    assert "spotifyforge auth login" in result.content[0].text


async def test_liked_genres_counts_by_primary_artist(logged_in):
    fake = logged_in
    for i in range(4):
        _like(fake, f"j{i}", artist="miles", genres=["jazz", "jazz fusion"])
    _like(fake, "r0", artist="stones", genres=["rock"])
    _like(fake, "u0", artist="nobody", genres=[])

    out = await _ok("liked_genres", min_tracks=2)

    assert out["liked_songs"] == 6
    assert out["unclassified_songs"] == 1
    assert out["distinct_genres"] == 3
    assert out["genres"] == [
        {"genre": "jazz", "songs": 4},
        {"genre": "jazz fusion", "songs": 4},
    ]


async def test_search_liked_filters_and_ranks(logged_in):
    fake = logged_in
    _like(
        fake,
        "a",
        artist="miles",
        genres=["jazz fusion"],
        album_id="alb_a",
        name="Shhh",
        popularity=70,
        release_date="1969-07-30",
    )
    _like(
        fake,
        "b",
        artist="miles",
        genres=["jazz fusion"],
        album_id="alb_b",
        name="Spanish Key",
        popularity=40,
        release_date="1970-03-30",
    )
    _like(
        fake,
        "c",
        artist="stones",
        genres=["rock"],
        album_id="alb_c",
        name="Shattered",
        release_date="1978-06-09",
    )

    jazz = await _ok("search_liked", genre="JAZZ")
    assert [t["id"] for t in jazz["tracks"]] == ["a", "b"]  # most popular first
    assert jazz["tracks"][0]["artists"] == ["Miles"]
    assert jazz["tracks"][0]["year"] == 1969

    seventies = await _ok("search_liked", year_from=1970, year_to=1979)
    assert {t["id"] for t in seventies["tracks"]} == {"b", "c"}

    capped = await _ok("search_liked", title="sh", limit=1)
    assert capped["total_matches"] == 3
    assert len(capped["tracks"]) == 1


async def test_search_liked_unclassified(logged_in):
    fake = logged_in
    _like(fake, "t", artist="tagged", genres=["rock"])
    _like(fake, "u", artist="untagged", genres=[])

    out = await _ok("search_liked", genre="unclassified")

    assert [t["id"] for t in out["tracks"]] == ["u"]


async def test_liked_library_is_cached_until_refresh(logged_in):
    fake = logged_in
    _like(fake, "a", artist="miles", genres=["jazz"])
    assert (await _ok("search_liked"))["total_matches"] == 1

    _like(fake, "b", artist="miles", genres=["jazz"])
    assert (await _ok("search_liked"))["total_matches"] == 1
    assert (await _ok("search_liked", refresh=True))["total_matches"] == 2


async def test_create_playlist_sequences_liked_tracks_and_is_private(logged_in):
    fake = logged_in
    # Three tracks by one artist and one by another: the arc ordering must
    # not leave the given order (a, b, c, d) standing, because it leads
    # with the most popular track.
    _like(fake, "a", artist="miles", genres=["jazz"], popularity=10)
    _like(fake, "b", artist="miles", genres=["jazz"], popularity=20)
    _like(fake, "c", artist="miles", genres=["jazz"], popularity=30)
    _like(fake, "d", artist="trane", genres=["jazz"], popularity=90)

    out = await _ok(
        "create_playlist",
        name="Late Set",
        description="after hours",
        track_ids=["a", "spotify:track:b", "https://open.spotify.com/track/c?si=x", "d", "a"],
    )

    pid = out["playlist_id"]
    assert out["ordering"] == "arc"
    assert out["tracks"] == 4  # the repeated "a" was dropped
    assert fake.playlists[pid]["name"] == "Late Set"
    assert fake.playlists[pid]["description"] == "after hours"
    assert fake.playlists[pid]["public"] is False
    assert sorted(fake.playlist_tracks[pid]) == ["a", "b", "c", "d"]
    assert fake.playlist_tracks[pid][0] == "d"


async def test_create_playlist_keeps_given_order_when_a_track_is_not_liked(logged_in):
    fake = logged_in
    _like(fake, "a", artist="miles", genres=["jazz"], popularity=10)
    _like(fake, "b", artist="miles", genres=["jazz"], popularity=90)
    fake.add_track("stranger")

    out = await _ok("create_playlist", name="Mixed", track_ids=["a", "stranger", "b"])

    assert out["ordering"] == "given"
    assert "1 track(s) are not in the user's liked songs" in out["note"]
    assert fake.playlist_tracks[out["playlist_id"]] == ["a", "stranger", "b"]
    assert fake.playlists[out["playlist_id"]]["public"] is False


async def test_create_playlist_cannot_be_asked_to_publish(logged_in):
    fake = logged_in
    _like(fake, "a", artist="miles", genres=["jazz"])

    result = await _call("create_playlist", name="Loud", track_ids=["a"], public=True)

    # Either the unknown argument is rejected or it is ignored; what must
    # not happen is a public playlist.
    assert all(p["public"] is False for p in fake.playlists.values())
    assert result.is_error or result.structured_content["public"] is False


async def test_create_playlist_refuses_an_empty_track_list(logged_in):
    result = await _call("create_playlist", name="Nothing", track_ids=[" "])

    assert result.is_error
    assert "track_ids is empty" in result.content[0].text
    assert not any(p["name"] == "Nothing" for p in logged_in.playlists.values())


async def test_list_and_get_playlist(logged_in):
    fake = logged_in
    fake.add_user("other")
    fake.add_track("t1", name="Song A")
    fake.add_playlist("mine", name="Mine", track_ids=["t1"])
    fake.add_playlist("theirs", owner="other", name="Followed")
    fake.follow_playlist("user1", "theirs")

    listed = await _ok("list_playlists")
    assert [p["id"] for p in listed["playlists"]] == ["mine"]
    everything = await _ok("list_playlists", owned_only=False)
    assert {p["id"] for p in everything["playlists"]} == {"mine", "theirs"}

    got = await _ok("get_playlist", playlist_id="mine")
    assert got["name"] == "Mine"
    assert [t["name"] for t in got["tracks"]] == ["Song A"]


async def test_deduplicate_playlist(logged_in):
    fake = logged_in
    fake.add_track("t1")
    fake.add_track("t2")
    fake.add_playlist("pl", name="Dupes", track_ids=["t1", "t2", "t1"])

    out = await _ok("deduplicate_playlist", playlist_id="pl")

    assert out["duplicates_removed"] == 1
    assert fake.playlist_tracks["pl"] == ["t1", "t2"]


async def test_deduplicate_refuses_a_playlist_owned_by_someone_else(logged_in):
    fake = logged_in
    fake.add_user("other")
    fake.add_track("t1")
    fake.add_playlist("theirs", owner="other", name="Theirs", track_ids=["t1", "t1"])

    result = await _call("deduplicate_playlist", playlist_id="theirs")

    assert result.is_error
    assert "belongs to someone else" in result.content[0].text
    assert fake.playlist_tracks["theirs"] == ["t1", "t1"]


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("get_playlist", {"playlist_id": "../me/tracks"}),
        ("deduplicate_playlist", {"playlist_id": "pl?fields=x"}),
        ("create_playlist", {"name": "X", "track_ids": ["a", "../../me"]}),
    ],
)
async def test_ids_that_are_not_spotify_ids_never_reach_a_request(logged_in, tool, arguments):
    fake = logged_in
    before = len(fake.requests)

    result = await _call(tool, **arguments)

    assert result.is_error
    assert "is not a Spotify" in result.content[0].text
    assert len(fake.requests) == before
    assert fake.playlists == {}


async def test_a_spotify_error_reaches_the_model_with_its_message(logged_in):
    result = await _call("get_playlist", playlist_id="ghost")

    assert result.is_error
    assert "Spotify request failed" in result.content[0].text


async def test_plan_catalogue_preview_marks_what_already_exists(logged_in):
    fake = logged_in
    for i in range(12):
        _like(fake, f"c{i}", artist=f"cold{i % 4}", genres=["coldwave"])

    plan = await _ok("plan_catalogue_preview", min_size=10)

    assert plan["liked_songs"] == 12
    assert plan["playlists_planned"] == 1
    spec = plan["playlists"][0]
    assert spec["genre"] == "coldwave"
    assert spec["tracks"] == 12
    assert spec["on_spotify"] is False
    assert fake.playlists == {}  # a preview creates nothing

    fake.add_playlist("existing", name=spec["title"])
    again = await _ok("plan_catalogue_preview", min_size=10)
    assert again["playlists"][0]["on_spotify"] is True


async def test_top_tracks(logged_in):
    fake = logged_in
    fake.add_track("t1", name="Most Played")
    fake.top_tracks["user1"] = ["t1"]

    out = await _ok("top_tracks", time_range="short_term", limit=5)

    assert out["tracks"] == [
        {"id": "t1", "name": "Most Played", "artists": ["Artist One"], "album": "Album One"}
    ]


async def test_listening_history_reads_the_local_log_without_calling_spotify(logged_in, tmp_path):
    from datetime import UTC, datetime, timedelta

    from spotifyforge.core.history import sync_history

    fake = logged_in
    fake.add_track("t1", name="Night Drive", artist_name="Alpha")
    now = datetime.now(UTC)
    fake.play("user1", "t1", (now - timedelta(hours=2)).isoformat(), "spotify:playlist:pl1")
    fake.play("user1", "t1", (now - timedelta(hours=1)).isoformat(), "spotify:playlist:pl1")
    client = fake.async_client("user1")
    await sync_history(client)
    await client.close()
    before = len(fake.requests)

    out = await _ok("listening_history", days=7)

    assert len(fake.requests) == before
    assert out["plays"] == 2
    assert out["top_tracks"] == [{"name": "Night Drive", "artists": "Alpha", "plays": 2}]
    assert out["top_contexts"] == [{"uri": "spotify:playlist:pl1", "plays": 2}]


async def test_listening_history_with_no_log_says_how_it_gets_filled(logged_in):
    out = await _ok("listening_history")

    assert out["plays"] == 0
    assert "history sync" in out["note"]


async def test_recently_played(logged_in):
    fake = logged_in
    fake.add_track("t1", name="Just Now")
    fake.play("user1", "t1", "2026-10-01T12:00:00Z")

    out = await _ok("recently_played", limit=5)

    assert [p["name"] for p in out["plays"]] == ["Just Now"]
    assert out["plays"][0]["played_at"].startswith("2026-10-01T12:00:00")


async def test_playlist_usage_report(logged_in):
    from datetime import UTC, datetime, timedelta

    from spotifyforge.core.history import sync_history

    fake = logged_in
    fake.add_track("t1")
    fake.add_playlist("mine1", name="Late Set", track_ids=["t1"])
    fake.add_playlist("mine2", name="Never Opened", track_ids=["t1"])
    fake.play(
        "user1",
        "t1",
        (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
        "spotify:playlist:mine1",
    )
    client = fake.async_client("user1")
    await sync_history(client)
    await client.close()

    out = await _ok("playlist_usage_report")

    assert out["sources"]["own_playlists"] == 1
    assert out["played"] == [{"id": "mine1", "name": "Late Set", "plays": 1}]
    assert out["unplayed"] == [{"id": "mine2", "name": "Never Opened"}]
    assert out["long_enough_to_judge"] is False


async def test_upcoming_shows_is_read_only_and_open_world():
    async with Client(server) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    hints = tools["upcoming_shows"].annotations
    assert hints.read_only_hint is True
    assert hints.open_world_hint is True


async def test_upcoming_shows_without_a_key_says_how_to_get_one(logged_in, monkeypatch):
    from spotifyforge.config import settings
    from spotifyforge.core import shows

    monkeypatch.setattr(settings, "ticketmaster_api_key", "")
    result = await _call("upcoming_shows")
    assert result.is_error
    assert result.content[0].text.endswith(shows.HOW_TO_GET_A_KEY)


async def test_upcoming_shows_without_a_home_says_how_to_set_one(logged_in, monkeypatch):
    from spotifyforge.config import settings

    monkeypatch.setattr(settings, "ticketmaster_api_key", "k")
    monkeypatch.setattr(settings, "home_location", "")
    result = await _call("upcoming_shows")
    assert result.is_error
    assert "SPOTIFYFORGE_HOME_LOCATION" in result.content[0].text


async def test_upcoming_shows_reports_shows_and_how_sure_each_match_is(logged_in, monkeypatch):
    import httpx

    from spotifyforge.config import settings
    from spotifyforge.core import shows
    from tests.fake_spotify import _full_artist

    fake = logged_in
    fake.top_artists["user1"] = [_full_artist("spPB", "Phoebe Bridgers")]
    monkeypatch.setattr(settings, "ticketmaster_api_key", "k")
    monkeypatch.setattr(settings, "home_location", "34.0522,-118.2437")
    monkeypatch.setattr(shows, "_TICKETMASTER_INTERVAL", 0)

    def ticketmaster(request):
        if request.url.path.endswith("attractions.json"):
            attraction = {
                "id": "PB",
                "name": "Phoebe Bridgers",
                "externalLinks": {"spotify": [{"url": "https://open.spotify.com/artist/spPB"}]},
            }
            return httpx.Response(200, json={"_embedded": {"attractions": [attraction]}})
        event = {
            "id": "E1",
            "name": "Phoebe Bridgers",
            "url": "javascript:alert(1)",
            "dates": {"start": {"localDate": "2026-11-14", "localTime": "20:00:00"}},
            "_embedded": {"venues": [{"name": "Hollywood Bowl", "distance": 7.34}]},
        }
        return httpx.Response(200, json={"_embedded": {"events": [event]}, "page": {}})

    real_find = shows.find_shows

    async def find_with_mock(*args, **kwargs):
        async with httpx.AsyncClient(transport=httpx.MockTransport(ticketmaster)) as client:
            return await real_find(*args, **{**kwargs, "client": client})

    monkeypatch.setattr(shows, "find_shows", find_with_mock)

    out = await _ok("upcoming_shows", days=60)

    assert out["where"] == "within 50 miles of 34.0522,-118.244"
    (show,) = out["shows"]
    assert show["artists"] == ["Phoebe Bridgers"]
    assert show["confidence"] == "linked"
    assert show["distance_miles"] == 7.3
    assert show["url"] is None  # the model never sees a link that is not a ticket page
    assert out["matched_by_name_only"] == []
