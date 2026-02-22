"""Comprehensive tests for PlaylistManager.

All Tekore Spotify client interactions and database sessions are mocked so
these tests run entirely in-process with no network or filesystem I/O.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import tekore as tk

from spotifyforge.core.playlist_manager import _CHUNK_SIZE, PlaylistManager

# ---------------------------------------------------------------------------
# Helpers – lightweight stand-ins for Tekore response objects
# ---------------------------------------------------------------------------


def _make_track(
    track_id: str = "t1",
    name: str = "Track One",
    uri: str | None = None,
    popularity: int = 50,
    duration_ms: int = 200_000,
    artist_names: list[str] | None = None,
    album_name: str = "Album",
    album_id: str = "alb1",
):
    """Return a SimpleNamespace that looks like a Tekore FullTrack."""
    if uri is None:
        uri = f"spotify:track:{track_id}"
    artists = [SimpleNamespace(name=n) for n in (artist_names or ["Artist"])]
    album = SimpleNamespace(name=album_name, id=album_id)
    external_ids = SimpleNamespace(isrc="USRC12345678")
    return SimpleNamespace(
        id=track_id,
        name=name,
        uri=uri,
        popularity=popularity,
        duration_ms=duration_ms,
        artists=artists,
        album=album,
        external_ids=external_ids,
    )


def _make_playlist_item(track=None, added_at=None, added_by=None):
    """Return a SimpleNamespace that looks like a Tekore PlaylistTrack item."""
    if track is None:
        track = _make_track()
    added_by_ns = SimpleNamespace(id=added_by) if added_by else None
    return SimpleNamespace(track=track, added_at=added_at, added_by=added_by_ns)


def _make_paging(items, next_url=None, total=None):
    """Return a SimpleNamespace that looks like a Tekore Paging object."""
    return SimpleNamespace(
        items=items,
        next=next_url,
        total=total if total is not None else len(items),
    )


def _make_spotify_playlist(
    playlist_id: str = "pl1",
    name: str = "My Playlist",
    description: str = "desc",
    public: bool = True,
    collaborative: bool = False,
    snapshot_id: str = "snap_abc",
    total_tracks: int = 5,
):
    """Return a SimpleNamespace mimicking a Tekore FullPlaylist."""
    return SimpleNamespace(
        id=playlist_id,
        name=name,
        description=description,
        public=public,
        collaborative=collaborative,
        snapshot_id=snapshot_id,
        tracks=SimpleNamespace(total=total_tracks),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_spotify():
    """Return an AsyncMock standing in for a tekore.Spotify client."""
    sp = AsyncMock()
    return sp


@pytest.fixture()
def manager(mock_spotify):
    """Return a PlaylistManager wired to the mock Spotify client."""
    return PlaylistManager(mock_spotify)


# ===================================================================
# add_tracks
# ===================================================================


class TestAddTracks:
    """Tests for PlaylistManager.add_tracks."""

    async def test_add_single_chunk(self, manager, mock_spotify):
        """Tracks <= 100 should produce exactly one API call."""
        uris = [f"spotify:track:{i}" for i in range(50)]
        mock_spotify.playlist_add.return_value = "snap_1"

        result = await manager.add_tracks("pl1", uris)

        assert result == "snap_1"
        mock_spotify.playlist_add.assert_awaited_once_with("pl1", uris, position=None)

    async def test_add_exactly_100_tracks(self, manager, mock_spotify):
        """Exactly _CHUNK_SIZE tracks should produce one API call."""
        uris = [f"spotify:track:{i}" for i in range(100)]
        mock_spotify.playlist_add.return_value = "snap_1"

        await manager.add_tracks("pl1", uris)

        assert mock_spotify.playlist_add.await_count == 1

    async def test_add_multiple_chunks(self, manager, mock_spotify):
        """250 tracks should produce ceil(250/100) == 3 API calls."""
        uris = [f"spotify:track:{i}" for i in range(250)]
        mock_spotify.playlist_add.return_value = "snap_final"

        result = await manager.add_tracks("pl1", uris)

        assert result == "snap_final"
        assert mock_spotify.playlist_add.await_count == 3

        # Verify chunk sizes: 100, 100, 50.
        calls = mock_spotify.playlist_add.call_args_list
        assert len(calls[0].args[1]) == 100
        assert len(calls[1].args[1]) == 100
        assert len(calls[2].args[1]) == 50

    async def test_add_tracks_with_position(self, manager, mock_spotify):
        """When *position* is given, each chunk offset should be added to it."""
        uris = [f"spotify:track:{i}" for i in range(150)]
        mock_spotify.playlist_add.return_value = "snap_x"

        await manager.add_tracks("pl1", uris, position=10)

        calls = mock_spotify.playlist_add.call_args_list
        assert calls[0].kwargs["position"] == 10  # first chunk at 10
        assert calls[1].kwargs["position"] == 110  # second chunk at 10+100

    async def test_add_tracks_empty_list(self, manager, mock_spotify):
        """An empty URI list should make zero API calls and return ''."""
        result = await manager.add_tracks("pl1", [])

        assert result == ""
        mock_spotify.playlist_add.assert_not_awaited()

    async def test_add_tracks_api_error_propagates(self, manager, mock_spotify):
        """An HTTPError from Tekore should propagate to the caller."""
        mock_spotify.playlist_add.side_effect = tk.HTTPError(
            "server error", request=MagicMock(), response=MagicMock()
        )

        with pytest.raises(tk.HTTPError):
            await manager.add_tracks("pl1", ["spotify:track:1"])


# ===================================================================
# remove_tracks
# ===================================================================


class TestRemoveTracks:
    """Tests for PlaylistManager.remove_tracks."""

    async def test_remove_single_chunk(self, manager, mock_spotify):
        uris = [f"spotify:track:{i}" for i in range(30)]
        mock_spotify.playlist_remove.return_value = "snap_r1"

        result = await manager.remove_tracks("pl1", uris)

        assert result == "snap_r1"
        mock_spotify.playlist_remove.assert_awaited_once_with("pl1", uris)

    async def test_remove_multiple_chunks(self, manager, mock_spotify):
        """200 tracks requires 2 chunked API calls."""
        uris = [f"spotify:track:{i}" for i in range(200)]
        mock_spotify.playlist_remove.return_value = "snap_r2"

        result = await manager.remove_tracks("pl1", uris)

        assert result == "snap_r2"
        assert mock_spotify.playlist_remove.await_count == 2

        calls = mock_spotify.playlist_remove.call_args_list
        assert len(calls[0].args[1]) == 100
        assert len(calls[1].args[1]) == 100

    async def test_remove_empty_list(self, manager, mock_spotify):
        result = await manager.remove_tracks("pl1", [])

        assert result == ""
        mock_spotify.playlist_remove.assert_not_awaited()

    async def test_remove_tracks_api_error(self, manager, mock_spotify):
        mock_spotify.playlist_remove.side_effect = tk.HTTPError(
            "bad request", request=MagicMock(), response=MagicMock()
        )

        with pytest.raises(tk.HTTPError):
            await manager.remove_tracks("pl1", ["spotify:track:1"])


# ===================================================================
# get_playlist_tracks (pagination)
# ===================================================================


class TestGetPlaylistTracks:
    """Tests for PlaylistManager.get_playlist_tracks."""

    async def test_single_page(self, manager, mock_spotify):
        """When next is None, only the first page is returned."""
        items = [_make_playlist_item(_make_track(f"t{i}")) for i in range(3)]
        mock_spotify.playlist_items.return_value = _make_paging(items, next_url=None)

        result = await manager.get_playlist_tracks("pl1")

        assert len(result) == 3
        mock_spotify.playlist_items.assert_awaited_once_with("pl1", limit=100)
        mock_spotify.next.assert_not_awaited()

    async def test_multiple_pages(self, manager, mock_spotify):
        """Multiple pages should be auto-fetched until next is None."""
        page1_items = [_make_playlist_item(_make_track(f"t{i}")) for i in range(100)]
        page2_items = [_make_playlist_item(_make_track(f"t{i}")) for i in range(100, 150)]

        page1 = _make_paging(page1_items, next_url="https://api.spotify.com/next")
        page2 = _make_paging(page2_items, next_url=None)

        mock_spotify.playlist_items.return_value = page1
        mock_spotify.next.return_value = page2

        result = await manager.get_playlist_tracks("pl1")

        assert len(result) == 150
        mock_spotify.next.assert_awaited_once_with(page1)

    async def test_three_pages(self, manager, mock_spotify):
        """Three pages of results are concatenated correctly."""
        page1_items = [_make_playlist_item(_make_track(f"p1_{i}")) for i in range(100)]
        page2_items = [_make_playlist_item(_make_track(f"p2_{i}")) for i in range(100)]
        page3_items = [_make_playlist_item(_make_track(f"p3_{i}")) for i in range(42)]

        page1 = _make_paging(page1_items, next_url="https://next1")
        page2 = _make_paging(page2_items, next_url="https://next2")
        page3 = _make_paging(page3_items, next_url=None)

        mock_spotify.playlist_items.return_value = page1
        mock_spotify.next.side_effect = [page2, page3]

        result = await manager.get_playlist_tracks("pl1")

        assert len(result) == 242
        assert mock_spotify.next.await_count == 2

    async def test_empty_playlist(self, manager, mock_spotify):
        """An empty playlist returns an empty list."""
        mock_spotify.playlist_items.return_value = _make_paging([], next_url=None)

        result = await manager.get_playlist_tracks("pl1")

        assert result == []

    async def test_none_items_in_page(self, manager, mock_spotify):
        """If page.items is None, treat as empty."""
        mock_spotify.playlist_items.return_value = SimpleNamespace(items=None, next=None, total=0)

        result = await manager.get_playlist_tracks("pl1")

        assert result == []

    async def test_pagination_returns_none(self, manager, mock_spotify):
        """If self._sp.next() returns None, pagination should stop."""
        page1_items = [_make_playlist_item(_make_track("t1"))]
        page1 = _make_paging(page1_items, next_url="https://next")

        mock_spotify.playlist_items.return_value = page1
        mock_spotify.next.return_value = None

        result = await manager.get_playlist_tracks("pl1")

        assert len(result) == 1

    async def test_get_tracks_api_error(self, manager, mock_spotify):
        mock_spotify.playlist_items.side_effect = tk.HTTPError(
            "not found", request=MagicMock(), response=MagicMock()
        )

        with pytest.raises(tk.HTTPError):
            await manager.get_playlist_tracks("pl1")

    async def test_pagination_api_error(self, manager, mock_spotify):
        """An error during pagination should propagate."""
        page1 = _make_paging(
            [_make_playlist_item(_make_track("t1"))],
            next_url="https://next",
        )
        mock_spotify.playlist_items.return_value = page1
        mock_spotify.next.side_effect = tk.HTTPError(
            "server error", request=MagicMock(), response=MagicMock()
        )

        with pytest.raises(tk.HTTPError):
            await manager.get_playlist_tracks("pl1")


# ===================================================================
# deduplicate
# ===================================================================


class TestDeduplicate:
    """Tests for PlaylistManager.deduplicate."""

    async def test_no_duplicates(self, manager, mock_spotify):
        """When there are no duplicates, no remove call is made."""
        items = [
            _make_playlist_item(_make_track("t1", uri="spotify:track:t1")),
            _make_playlist_item(_make_track("t2", uri="spotify:track:t2")),
            _make_playlist_item(_make_track("t3", uri="spotify:track:t3")),
        ]
        mock_spotify.playlist_items.return_value = _make_paging(items, next_url=None)

        removed = await manager.deduplicate("pl1")

        assert removed == 0
        mock_spotify.playlist_remove.assert_not_awaited()

    async def test_with_duplicates(self, manager, mock_spotify):
        """Duplicate URIs should be removed, keeping only the first occurrence."""
        items = [
            _make_playlist_item(_make_track("t1", uri="spotify:track:t1")),
            _make_playlist_item(_make_track("t2", uri="spotify:track:t2")),
            _make_playlist_item(_make_track("t1_dup", uri="spotify:track:t1")),  # dup of t1
            _make_playlist_item(_make_track("t3", uri="spotify:track:t3")),
            _make_playlist_item(_make_track("t2_dup", uri="spotify:track:t2")),  # dup of t2
        ]
        mock_spotify.playlist_items.return_value = _make_paging(items, next_url=None)
        mock_spotify.playlist_remove.return_value = "snap_dedup"

        removed = await manager.deduplicate("pl1")

        assert removed == 2
        # The remove call should contain exactly the duplicate URIs.
        mock_spotify.playlist_remove.assert_awaited_once()
        call_uris = mock_spotify.playlist_remove.call_args.args[1]
        assert call_uris == ["spotify:track:t1", "spotify:track:t2"]

    async def test_all_duplicates(self, manager, mock_spotify):
        """A playlist where every track is repeated once."""
        items = [
            _make_playlist_item(_make_track("t1", uri="spotify:track:t1")),
            _make_playlist_item(_make_track("t1_dup", uri="spotify:track:t1")),
        ]
        mock_spotify.playlist_items.return_value = _make_paging(items, next_url=None)
        mock_spotify.playlist_remove.return_value = "snap_x"

        removed = await manager.deduplicate("pl1")

        assert removed == 1

    async def test_skips_none_track(self, manager, mock_spotify):
        """Items with track=None should be silently skipped."""
        items = [
            _make_playlist_item(_make_track("t1", uri="spotify:track:t1")),
            SimpleNamespace(track=None, added_at=None, added_by=None),
            _make_playlist_item(_make_track("t1_dup", uri="spotify:track:t1")),
        ]
        mock_spotify.playlist_items.return_value = _make_paging(items, next_url=None)
        mock_spotify.playlist_remove.return_value = "snap_y"

        removed = await manager.deduplicate("pl1")

        assert removed == 1

    async def test_skips_none_uri(self, manager, mock_spotify):
        """Items where track.uri is None should be skipped."""
        items = [
            _make_playlist_item(_make_track("t1", uri=None)),
            _make_playlist_item(_make_track("t2", uri="spotify:track:t2")),
        ]
        mock_spotify.playlist_items.return_value = _make_paging(items, next_url=None)

        removed = await manager.deduplicate("pl1")

        assert removed == 0


# ===================================================================
# snapshot_check
# ===================================================================


class TestSnapshotCheck:
    """Tests for PlaylistManager.snapshot_check."""

    async def test_snapshot_changed(self, manager, mock_spotify):
        """Returns True when the remote snapshot differs from the known one."""
        mock_spotify.playlist.return_value = SimpleNamespace(snapshot_id="snap_new")

        changed = await manager.snapshot_check("pl1", "snap_old")

        assert changed is True
        mock_spotify.playlist.assert_awaited_once_with("pl1", fields="snapshot_id")

    async def test_snapshot_unchanged(self, manager, mock_spotify):
        """Returns False when the remote snapshot matches the known one."""
        mock_spotify.playlist.return_value = SimpleNamespace(snapshot_id="snap_same")

        changed = await manager.snapshot_check("pl1", "snap_same")

        assert changed is False

    async def test_snapshot_check_api_error(self, manager, mock_spotify):
        mock_spotify.playlist.side_effect = tk.HTTPError(
            "not found", request=MagicMock(), response=MagicMock()
        )

        with pytest.raises(tk.HTTPError):
            await manager.snapshot_check("pl1", "snap_old")


# ===================================================================
# create_playlist
# ===================================================================


class TestCreatePlaylist:
    """Tests for PlaylistManager.create_playlist."""

    @patch("spotifyforge.core.playlist_manager.get_async_session")
    async def test_create_playlist_basic(self, mock_get_session, manager, mock_spotify):
        """Verify the API is called with correct params and a DB row is created."""
        # Mock the Spotify API responses.
        mock_spotify.current_user.return_value = SimpleNamespace(id="user123")
        mock_spotify.playlist_create.return_value = SimpleNamespace(
            id="sp_new_pl",
            name="Test Playlist",
            snapshot_id="snap_new",
        )

        # Mock the async session context manager.
        mock_session = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_get_session.return_value = mock_ctx

        # Make session.refresh populate the model with an id.
        async def _fake_refresh(obj):
            obj.id = 42

        mock_session.refresh.side_effect = _fake_refresh

        result = await manager.create_playlist(
            name="Test Playlist",
            description="A test",
            public=False,
        )

        # Verify Spotify API calls.
        mock_spotify.current_user.assert_awaited_once()
        mock_spotify.playlist_create.assert_awaited_once_with(
            "user123",
            "Test Playlist",
            public=False,
            description="A test",
        )

        # Verify the returned object.
        assert result.spotify_id == "sp_new_pl"
        assert result.name == "Test Playlist"
        assert result.public is False

    @patch("spotifyforge.core.playlist_manager.get_async_session")
    async def test_create_playlist_default_params(self, mock_get_session, manager, mock_spotify):
        """Defaults: public=True, description=''."""
        mock_spotify.current_user.return_value = SimpleNamespace(id="user1")
        mock_spotify.playlist_create.return_value = SimpleNamespace(
            id="sp_pl2", name="PL", snapshot_id="snap2"
        )

        mock_session = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_get_session.return_value = mock_ctx

        async def _fake_refresh(obj):
            obj.id = 99

        mock_session.refresh.side_effect = _fake_refresh

        result = await manager.create_playlist(name="PL")

        mock_spotify.playlist_create.assert_awaited_once_with(
            "user1", "PL", public=True, description=""
        )
        assert result.public is True
        assert result.description == ""

    async def test_create_playlist_api_error(self, manager, mock_spotify):
        """HTTPError during user lookup should propagate."""
        mock_spotify.current_user.side_effect = tk.HTTPError(
            "auth error", request=MagicMock(), response=MagicMock()
        )

        with pytest.raises(tk.HTTPError):
            await manager.create_playlist(name="Fail")

    async def test_create_playlist_api_error_on_create(self, manager, mock_spotify):
        """HTTPError during playlist_create should propagate."""
        mock_spotify.current_user.return_value = SimpleNamespace(id="user1")
        mock_spotify.playlist_create.side_effect = tk.HTTPError(
            "server error", request=MagicMock(), response=MagicMock()
        )

        with pytest.raises(tk.HTTPError):
            await manager.create_playlist(name="Fail")


# ===================================================================
# reorder_tracks
# ===================================================================


class TestReorderTracks:
    """Tests for PlaylistManager.reorder_tracks."""

    async def test_reorder_default_range(self, manager, mock_spotify):
        """Reorder a single track (range_length=1, the default)."""
        mock_spotify.playlist_reorder.return_value = "snap_reorder"

        result = await manager.reorder_tracks("pl1", range_start=2, insert_before=5)

        assert result == "snap_reorder"
        mock_spotify.playlist_reorder.assert_awaited_once_with(
            "pl1", range_start=2, insert_before=5, range_length=1
        )

    async def test_reorder_block(self, manager, mock_spotify):
        """Reorder a block of 3 tracks."""
        mock_spotify.playlist_reorder.return_value = "snap_r2"

        result = await manager.reorder_tracks(
            "pl1", range_start=0, insert_before=10, range_length=3
        )

        assert result == "snap_r2"
        mock_spotify.playlist_reorder.assert_awaited_once_with(
            "pl1", range_start=0, insert_before=10, range_length=3
        )

    async def test_reorder_api_error(self, manager, mock_spotify):
        mock_spotify.playlist_reorder.side_effect = tk.HTTPError(
            "bad request", request=MagicMock(), response=MagicMock()
        )

        with pytest.raises(tk.HTTPError):
            await manager.reorder_tracks("pl1", range_start=0, insert_before=5)


# ===================================================================
# Edge cases & integration-style scenarios
# ===================================================================


class TestEdgeCases:
    """Miscellaneous edge-case tests."""

    async def test_add_tracks_101_produces_two_chunks(self, manager, mock_spotify):
        """101 tracks => 2 API calls (100 + 1)."""
        uris = [f"spotify:track:{i}" for i in range(101)]
        mock_spotify.playlist_add.return_value = "snap"

        await manager.add_tracks("pl1", uris)

        assert mock_spotify.playlist_add.await_count == 2
        calls = mock_spotify.playlist_add.call_args_list
        assert len(calls[0].args[1]) == 100
        assert len(calls[1].args[1]) == 1

    async def test_chunk_size_constant(self):
        """Verify the chunk size constant is 100 (Spotify's documented limit)."""
        assert _CHUNK_SIZE == 100

    async def test_remove_tracks_large_batch(self, manager, mock_spotify):
        """350 tracks => 4 API calls (100 + 100 + 100 + 50)."""
        uris = [f"spotify:track:{i}" for i in range(350)]
        mock_spotify.playlist_remove.return_value = "snap"

        await manager.remove_tracks("pl1", uris)

        assert mock_spotify.playlist_remove.await_count == 4
        calls = mock_spotify.playlist_remove.call_args_list
        assert len(calls[0].args[1]) == 100
        assert len(calls[1].args[1]) == 100
        assert len(calls[2].args[1]) == 100
        assert len(calls[3].args[1]) == 50

    async def test_deduplicate_with_many_dupes_chunking(self, manager, mock_spotify):
        """If dedup produces >100 duplicates, remove_tracks should chunk them."""
        # Build 150 unique items, then 150 duplicates of the first ones.
        unique_items = [
            _make_playlist_item(_make_track(f"t{i}", uri=f"spotify:track:t{i}")) for i in range(150)
        ]
        duplicate_items = [
            _make_playlist_item(_make_track(f"t{i}_dup", uri=f"spotify:track:t{i}"))
            for i in range(150)
        ]
        all_items = unique_items + duplicate_items

        mock_spotify.playlist_items.return_value = _make_paging(all_items, next_url=None)
        mock_spotify.playlist_remove.return_value = "snap_d"

        removed = await manager.deduplicate("pl1")

        assert removed == 150
        # 150 duplicates / 100 chunk size = 2 calls
        assert mock_spotify.playlist_remove.await_count == 2

    async def test_add_tracks_position_none(self, manager, mock_spotify):
        """When position is None, every chunk should pass position=None."""
        uris = [f"spotify:track:{i}" for i in range(150)]
        mock_spotify.playlist_add.return_value = "snap"

        await manager.add_tracks("pl1", uris, position=None)

        for call in mock_spotify.playlist_add.call_args_list:
            assert call.kwargs["position"] is None

    async def test_snapshot_check_identical_snapshots(self, manager, mock_spotify):
        """Even very long snapshot IDs should compare correctly."""
        long_snap = "a" * 200
        mock_spotify.playlist.return_value = SimpleNamespace(snapshot_id=long_snap)

        changed = await manager.snapshot_check("pl1", long_snap)

        assert changed is False
