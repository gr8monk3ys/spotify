"""Tests for PlaylistManager.

Request/response behavior (pagination, chunking, dedup ordering, DB sync)
runs against the in-memory FakeSpotify backend so the real tekore client
stack is exercised. AsyncMock is used only for narrow unit tests such as
error propagation.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import tekore as tk

from spotifyforge.core.playlist_manager import _CHUNK_SIZE, PlaylistManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _uri(track_id: str) -> str:
    return f"spotify:track:{track_id}"


def _requests_for(fake, method: str, path: str) -> list[tuple[str, str]]:
    return [r for r in fake.requests if r[0] == method and r[1] == path]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_spotify():
    """AsyncMock stand-in, for narrow unit tests (error propagation etc.)."""
    return AsyncMock()


@pytest.fixture()
def mock_manager(mock_spotify):
    return PlaylistManager(mock_spotify)


def _http_error() -> tk.HTTPError:
    return tk.HTTPError("boom", request=MagicMock(), response=MagicMock())


# ===================================================================
# add_tracks (FakeSpotify: chunking and position are request behavior)
# ===================================================================


class TestAddTracks:
    async def test_add_appends_in_order(self, fake_spotify, client_for):
        fake_spotify.add_user("user1")
        fake_spotify.add_playlist("pl", track_ids=["seed"])
        manager = PlaylistManager(client_for())

        snapshot = await manager.add_tracks("pl", [_uri("a"), _uri("b")])

        assert fake_spotify.playlist_tracks["pl"] == ["seed", "a", "b"]
        assert snapshot == fake_spotify.playlists["pl"]["snapshot_id"]

    async def test_add_over_100_chunks_requests(self, fake_spotify, client_for):
        """250 URIs must arrive as 3 POSTs and land in order."""
        fake_spotify.add_user("user1")
        fake_spotify.add_playlist("pl")
        manager = PlaylistManager(client_for())
        ids = [f"t{i:03d}" for i in range(250)]

        await manager.add_tracks("pl", [_uri(i) for i in ids])

        assert fake_spotify.playlist_tracks["pl"] == ids
        posts = _requests_for(fake_spotify, "POST", "/v1/playlists/pl/tracks")
        assert len(posts) == 3

    async def test_add_with_position_across_chunks(self, fake_spotify, client_for):
        """Each chunk's insert position is offset so order is preserved."""
        fake_spotify.add_user("user1")
        fake_spotify.add_playlist("pl", track_ids=["a", "b", "c"])
        manager = PlaylistManager(client_for())
        new_ids = [f"n{i:03d}" for i in range(150)]

        await manager.add_tracks("pl", [_uri(i) for i in new_ids], position=1)

        assert fake_spotify.playlist_tracks["pl"] == ["a", *new_ids, "b", "c"]

    async def test_add_empty_list_makes_no_requests(self, fake_spotify, client_for):
        fake_spotify.add_user("user1")
        fake_spotify.add_playlist("pl")
        manager = PlaylistManager(client_for())

        result = await manager.add_tracks("pl", [])

        assert result == ""
        assert _requests_for(fake_spotify, "POST", "/v1/playlists/pl/tracks") == []

    async def test_add_tracks_api_error_propagates(self, mock_manager, mock_spotify):
        mock_spotify.playlist_add.side_effect = _http_error()

        with pytest.raises(tk.HTTPError):
            await mock_manager.add_tracks("pl1", [_uri("x")])

    async def test_chunk_size_constant(self):
        """Spotify's documented per-request limit."""
        assert _CHUNK_SIZE == 100


# ===================================================================
# remove_tracks
# ===================================================================


class TestRemoveTracks:
    async def test_remove_deletes_every_occurrence(self, fake_spotify, client_for):
        """Spotify's remove-by-URI semantics: all copies of a URI go."""
        fake_spotify.add_user("user1")
        fake_spotify.add_playlist("pl", track_ids=["a", "b", "a", "c"])
        manager = PlaylistManager(client_for())

        await manager.remove_tracks("pl", [_uri("a")])

        assert fake_spotify.playlist_tracks["pl"] == ["b", "c"]

    async def test_remove_over_100_chunks_requests(self, fake_spotify, client_for):
        fake_spotify.add_user("user1")
        ids = [f"t{i:03d}" for i in range(150)]
        fake_spotify.add_playlist("pl", track_ids=ids + ["keep"])
        manager = PlaylistManager(client_for())

        await manager.remove_tracks("pl", [_uri(i) for i in ids])

        assert fake_spotify.playlist_tracks["pl"] == ["keep"]
        deletes = _requests_for(fake_spotify, "DELETE", "/v1/playlists/pl/tracks")
        assert len(deletes) == 2

    async def test_remove_empty_list_makes_no_requests(self, fake_spotify, client_for):
        fake_spotify.add_user("user1")
        fake_spotify.add_playlist("pl", track_ids=["a"])
        manager = PlaylistManager(client_for())

        result = await manager.remove_tracks("pl", [])

        assert result == ""
        assert fake_spotify.playlist_tracks["pl"] == ["a"]

    async def test_remove_tracks_api_error_propagates(self, mock_manager, mock_spotify):
        mock_spotify.playlist_remove.side_effect = _http_error()

        with pytest.raises(tk.HTTPError):
            await mock_manager.remove_tracks("pl1", [_uri("x")])


# ===================================================================
# get_playlist_tracks (pagination)
# ===================================================================


class TestGetPlaylistTracks:
    async def test_paginates_past_100(self, fake_spotify, client_for):
        """250 tracks arrive across 3 pages and come back flat, in order."""
        fake_spotify.add_user("user1")
        ids = [f"t{i:03d}" for i in range(250)]
        for tid in ids:
            fake_spotify.add_track(tid)
        fake_spotify.add_playlist("pl", track_ids=ids)
        manager = PlaylistManager(client_for())

        items = await manager.get_playlist_tracks("pl")

        assert [item.track.id for item in items] == ids
        gets = _requests_for(fake_spotify, "GET", "/v1/playlists/pl/tracks")
        assert len(gets) == 3

    async def test_empty_playlist(self, fake_spotify, client_for):
        fake_spotify.add_user("user1")
        fake_spotify.add_playlist("pl")
        manager = PlaylistManager(client_for())

        assert await manager.get_playlist_tracks("pl") == []

    async def test_api_error_propagates(self, mock_manager, mock_spotify):
        mock_spotify.playlist_items.side_effect = _http_error()

        with pytest.raises(tk.HTTPError):
            await mock_manager.get_playlist_tracks("pl1")

    async def test_pagination_error_propagates(self, mock_manager, mock_spotify):
        page1 = SimpleNamespace(
            items=[SimpleNamespace(track=None, added_at=None, added_by=None)],
            next="https://next",
        )
        mock_spotify.playlist_items.return_value = page1
        mock_spotify.next.side_effect = _http_error()

        with pytest.raises(tk.HTTPError):
            await mock_manager.get_playlist_tracks("pl1")


# ===================================================================
# get_user_playlists
# ===================================================================


class TestGetUserPlaylists:
    async def test_lists_playlists_without_followers_key(self, fake_spotify, client_for):
        """The list endpoint carries no follower counts, so dicts must not either."""
        fake_spotify.add_user("user1")
        fake_spotify.add_track("t1")
        fake_spotify.add_playlist("pl1", name="First", track_ids=["t1"], public=True)
        fake_spotify.add_playlist("pl2", name="Second", public=False)
        manager = PlaylistManager(client_for())

        playlists = await manager.get_user_playlists()

        assert len(playlists) == 2
        by_id = {p["id"]: p for p in playlists}
        assert set(by_id["pl1"]) == {
            "id",
            "name",
            "description",
            "owner_id",
            "track_count",
            "public",
        }
        assert by_id["pl1"]["owner_id"] == "user1"
        assert by_id["pl1"]["name"] == "First"
        assert by_id["pl1"]["track_count"] == 1
        assert by_id["pl1"]["public"] is True
        assert by_id["pl2"]["public"] is False

    async def test_paginates_past_50(self, fake_spotify, client_for):
        fake_spotify.add_user("user1")
        for i in range(60):
            fake_spotify.add_playlist(f"pl{i:02d}", name=f"PL {i}")
        manager = PlaylistManager(client_for())

        playlists = await manager.get_user_playlists()

        assert len(playlists) == 60
        gets = _requests_for(fake_spotify, "GET", "/v1/users/user1/playlists")
        assert len(gets) == 2

    async def test_api_error_propagates(self, mock_manager, mock_spotify):
        mock_spotify.current_user.side_effect = _http_error()

        with pytest.raises(tk.HTTPError):
            await mock_manager.get_user_playlists()


# ===================================================================
# get_playlist_details
# ===================================================================


class TestGetPlaylistDetails:
    async def test_meta_and_tracks(self, fake_spotify, client_for):
        fake_spotify.add_user("user1")
        fake_spotify.add_track("t1", name="Song One")
        fake_spotify.add_track("t2", name="Song Two")
        fake_spotify.add_playlist("pl", name="Detail PL", track_ids=["t1", "t2"])
        manager = PlaylistManager(client_for())

        details = await manager.get_playlist_details("pl")

        assert details["meta"]["name"] == "Detail PL"
        assert details["meta"]["track_count"] == 2
        assert [t["name"] for t in details["tracks"]] == ["Song One", "Song Two"]
        assert details["tracks"][0]["uri"] == _uri("t1")

    async def test_api_error_propagates(self, mock_manager, mock_spotify):
        mock_spotify.playlist.side_effect = _http_error()

        with pytest.raises(tk.HTTPError):
            await mock_manager.get_playlist_details("pl1")


# ===================================================================
# deduplicate
# ===================================================================


class TestDeduplicate:
    async def test_no_duplicates_is_a_noop(self, fake_spotify, client_for):
        fake_spotify.add_user("user1")
        for tid in ("a", "b", "c"):
            fake_spotify.add_track(tid)
        fake_spotify.add_playlist("pl", track_ids=["a", "b", "c"])
        manager = PlaylistManager(client_for())

        removed = await manager.deduplicate("pl")

        assert removed == 0
        assert fake_spotify.playlist_tracks["pl"] == ["a", "b", "c"]
        assert _requests_for(fake_spotify, "DELETE", "/v1/playlists/pl/tracks") == []
        assert _requests_for(fake_spotify, "POST", "/v1/playlists/pl/tracks") == []

    async def test_removes_all_copies_then_reinserts_in_place(self, fake_spotify, client_for):
        """Order-preserving: the final playlist is the original sequence with
        2nd+ occurrences dropped, and the count is duplicate *occurrences*."""
        fake_spotify.add_user("user1")
        for tid in ("a", "b", "c"):
            fake_spotify.add_track(tid)
        fake_spotify.add_playlist("pl", track_ids=["a", "b", "a", "c", "b", "a"])
        manager = PlaylistManager(client_for())

        removed = await manager.deduplicate("pl")

        assert removed == 3  # extra a (x2) + extra b (x1)
        assert fake_spotify.playlist_tracks["pl"] == ["a", "b", "c"]

    async def test_remove_happens_before_reinsert(self, fake_spotify, client_for):
        """Remove-by-URI kills every copy, so the kept copy must be re-added
        afterwards — DELETE first, then POST."""
        fake_spotify.add_user("user1")
        for tid in ("x", "a"):
            fake_spotify.add_track(tid)
        fake_spotify.add_playlist("pl", track_ids=["x", "a", "a"])
        manager = PlaylistManager(client_for())

        removed = await manager.deduplicate("pl")

        assert removed == 1
        assert fake_spotify.playlist_tracks["pl"] == ["x", "a"]
        mutations = [
            m
            for m, p in fake_spotify.requests
            if p == "/v1/playlists/pl/tracks" and m in ("POST", "DELETE")
        ]
        assert mutations == ["DELETE", "POST"]

    async def test_duplicate_of_first_track_keeps_position_zero(self, fake_spotify, client_for):
        fake_spotify.add_user("user1")
        for tid in ("a", "b"):
            fake_spotify.add_track(tid)
        fake_spotify.add_playlist("pl", track_ids=["a", "a", "b"])
        manager = PlaylistManager(client_for())

        removed = await manager.deduplicate("pl")

        assert removed == 1
        assert fake_spotify.playlist_tracks["pl"] == ["a", "b"]

    async def test_over_100_duplicated_uris_chunk_the_removal(self, fake_spotify, client_for):
        ids = [f"t{i:03d}" for i in range(120)]
        fake_spotify.add_user("user1")
        for tid in ids:
            fake_spotify.add_track(tid)
        fake_spotify.add_playlist("pl", track_ids=ids + ids)  # every track duplicated
        manager = PlaylistManager(client_for())

        removed = await manager.deduplicate("pl")

        assert removed == 120
        assert fake_spotify.playlist_tracks["pl"] == ids
        deletes = _requests_for(fake_spotify, "DELETE", "/v1/playlists/pl/tracks")
        assert len(deletes) == 2

    async def test_skips_items_without_track_or_uri(self, mock_manager, mock_spotify):
        """Local/unavailable items must not count as duplicates of None."""
        track = SimpleNamespace(uri=_uri("a"))
        items = [
            SimpleNamespace(track=track, added_at=None, added_by=None),
            SimpleNamespace(track=None, added_at=None, added_by=None),
            SimpleNamespace(track=SimpleNamespace(uri=None), added_at=None, added_by=None),
        ]
        mock_spotify.playlist.return_value = SimpleNamespace(snapshot_id="snap")
        mock_spotify.playlist_items.return_value = SimpleNamespace(items=items, next=None)

        removed = await mock_manager.deduplicate("pl1")

        assert removed == 0
        mock_spotify.playlist_remove.assert_not_awaited()

    async def test_api_error_propagates(self, mock_manager, mock_spotify):
        mock_spotify.playlist.side_effect = _http_error()

        with pytest.raises(tk.HTTPError):
            await mock_manager.deduplicate("pl1")


# ===================================================================
# reorder_tracks (no fake endpoint — unit tests against the mock)
# ===================================================================


class TestReorderTracks:
    async def test_reorder_default_range(self, mock_manager, mock_spotify):
        mock_spotify.playlist_reorder.return_value = "snap_reorder"

        result = await mock_manager.reorder_tracks("pl1", range_start=2, insert_before=5)

        assert result == "snap_reorder"
        mock_spotify.playlist_reorder.assert_awaited_once_with(
            "pl1", range_start=2, insert_before=5, range_length=1
        )

    async def test_reorder_block(self, mock_manager, mock_spotify):
        mock_spotify.playlist_reorder.return_value = "snap_r2"

        result = await mock_manager.reorder_tracks(
            "pl1", range_start=0, insert_before=10, range_length=3
        )

        assert result == "snap_r2"
        mock_spotify.playlist_reorder.assert_awaited_once_with(
            "pl1", range_start=0, insert_before=10, range_length=3
        )

    async def test_reorder_api_error(self, mock_manager, mock_spotify):
        mock_spotify.playlist_reorder.side_effect = _http_error()

        with pytest.raises(tk.HTTPError):
            await mock_manager.reorder_tracks("pl1", range_start=0, insert_before=5)


# ===================================================================
# create_playlist
# ===================================================================


class TestCreatePlaylist:
    async def test_creates_on_spotify(self, fake_spotify, client_for):
        fake_spotify.add_user("user1")
        manager = PlaylistManager(client_for())

        created = await manager.create_playlist(
            name="Fresh Cuts", description="new stuff", public=True
        )

        assert created.spotify_id in fake_spotify.playlists
        meta = fake_spotify.playlists[created.spotify_id]
        assert meta["name"] == "Fresh Cuts"
        assert meta["description"] == "new stuff"
        assert meta["public"] is True
        assert created.name == "Fresh Cuts"

    async def test_collaborative_created_private_then_flipped(self, fake_spotify, client_for):
        """Spotify requires collaborative playlists to be non-public; the
        create endpoint has no collaborative flag so it is set via a
        follow-up playlist_change_details call."""
        fake_spotify.add_user("user1")
        manager = PlaylistManager(client_for())

        created = await manager.create_playlist(name="Shared", public=True, collaborative=True)

        meta = fake_spotify.playlists[created.spotify_id]
        assert meta["public"] is False  # forced private
        assert meta["collaborative"] is True  # flipped by the PUT

    async def test_api_error_propagates(self, mock_manager, mock_spotify):
        mock_spotify.current_user.side_effect = _http_error()

        with pytest.raises(tk.HTTPError):
            await mock_manager.create_playlist(name="Fail")


class TestTimeoutRetryingSender:
    """Transport failures get no response, so tekore's RetryingSender
    never sees them — this is what keeps a bulk run alive."""

    async def test_retries_a_transport_error_then_succeeds(self):
        import httpx

        from spotifyforge.core.clients import TimeoutRetryingSender

        class Flaky:
            is_async = True

            def __init__(self):
                self.calls = 0

            async def send(self, request):
                self.calls += 1
                if self.calls == 1:
                    raise httpx.ReadTimeout("")
                return "response"

            def close(self):
                return None

        inner = Flaky()
        sender = TimeoutRetryingSender(inner, retries=2)
        sender.retries = 2
        with patch("asyncio.sleep", new=AsyncMock()):
            assert await sender.send("req") == "response"
        assert inner.calls == 2

    async def test_gives_up_after_the_retry_budget(self):
        import httpx

        from spotifyforge.core.clients import TimeoutRetryingSender

        class Dead:
            is_async = True

            def __init__(self):
                self.calls = 0

            async def send(self, request):
                self.calls += 1
                raise httpx.ConnectError("down")

            def close(self):
                return None

        inner = Dead()
        sender = TimeoutRetryingSender(inner, retries=1)
        with patch("asyncio.sleep", new=AsyncMock()), pytest.raises(httpx.ConnectError):
            await sender.send("req")
        assert inner.calls == 2
