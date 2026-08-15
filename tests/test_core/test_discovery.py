"""Comprehensive tests for DiscoveryEngine and its module-level serializers.

Request/response behavior (pagination, batching, search) runs against the
in-memory FakeSpotify backend so the real tekore client stack is exercised;
AsyncMock covers narrow unit tests (limit clamping, query building, error
propagation).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import tekore as tk

from spotifyforge.core.discovery import DiscoveryEngine, artist_to_dict, track_to_dict

# ---------------------------------------------------------------------------
# Helpers -- lightweight stand-ins for Tekore response objects
# ---------------------------------------------------------------------------


def _make_track(
    track_id: str = "t1",
    name: str = "Track One",
    uri: str | None = None,
    popularity: int = 50,
):
    """Return a SimpleNamespace mimicking a Tekore FullTrack."""
    if uri is None:
        uri = f"spotify:track:{track_id}"
    return SimpleNamespace(
        id=track_id,
        name=name,
        uri=uri,
        popularity=popularity,
    )


def _make_simple_track(track_id: str = "st1", name: str = "Simple Track"):
    """Return a SimpleNamespace mimicking a Tekore SimpleTrack (from album_tracks)."""
    return SimpleNamespace(id=track_id, name=name)


def _make_album(album_id: str = "alb1", name: str = "Album One"):
    """Return a SimpleNamespace mimicking a Tekore SimpleAlbum."""
    return SimpleNamespace(id=album_id, name=name)


def _make_artist(artist_id: str = "art1", name: str = "Artist One"):
    """Return a SimpleNamespace mimicking a Tekore FullArtist."""
    return SimpleNamespace(id=artist_id, name=name)


def _make_play_history(track=None, played_at="2025-01-01T12:00:00Z", context=None):
    """Return a SimpleNamespace mimicking a Tekore PlayHistory."""
    if track is None:
        track = _make_track()
    return SimpleNamespace(track=track, played_at=played_at, context=context)


def _make_paging(items, next_url=None, total=None):
    """Return a SimpleNamespace mimicking a Tekore Paging object."""
    return SimpleNamespace(
        items=items,
        next=next_url,
        total=total if total is not None else len(items),
    )


def _make_cursor_paging(items, next_url=None):
    """Return a SimpleNamespace mimicking a Tekore CursorPaging object."""
    return SimpleNamespace(items=items, next=next_url)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_spotify():
    """Return an AsyncMock standing in for a tekore.Spotify client."""
    return AsyncMock()


@pytest.fixture()
def engine(mock_spotify):
    """Return a DiscoveryEngine wired to the mock Spotify client."""
    return DiscoveryEngine(mock_spotify)


# ===================================================================
# get_user_top_tracks
# ===================================================================


class TestGetUserTopTracks:
    """Tests for DiscoveryEngine.get_user_top_tracks."""

    async def test_default_params(self, engine, mock_spotify):
        """Default time_range='medium_term', limit=50."""
        tracks = [_make_track(f"t{i}") for i in range(10)]
        mock_spotify.current_user_top_tracks.return_value = _make_paging(tracks)

        result = await engine.get_user_top_tracks()

        assert len(result) == 10
        mock_spotify.current_user_top_tracks.assert_awaited_once_with(
            time_range="medium_term", limit=50
        )

    async def test_short_term(self, engine, mock_spotify):
        tracks = [_make_track("t1")]
        mock_spotify.current_user_top_tracks.return_value = _make_paging(tracks)

        result = await engine.get_user_top_tracks(time_range="short_term", limit=10)

        mock_spotify.current_user_top_tracks.assert_awaited_once_with(
            time_range="short_term", limit=10
        )
        assert len(result) == 1

    async def test_long_term(self, engine, mock_spotify):
        tracks = [_make_track(f"t{i}") for i in range(50)]
        mock_spotify.current_user_top_tracks.return_value = _make_paging(tracks)

        result = await engine.get_user_top_tracks(time_range="long_term", limit=50)

        mock_spotify.current_user_top_tracks.assert_awaited_once_with(
            time_range="long_term", limit=50
        )
        assert len(result) == 50

    async def test_limit_clamped_high(self, engine, mock_spotify):
        """Limit > 50 should be clamped to 50."""
        mock_spotify.current_user_top_tracks.return_value = _make_paging([])

        await engine.get_user_top_tracks(limit=999)

        mock_spotify.current_user_top_tracks.assert_awaited_once_with(
            time_range="medium_term", limit=50
        )

    async def test_limit_clamped_low(self, engine, mock_spotify):
        """Limit < 1 should be clamped to 1."""
        mock_spotify.current_user_top_tracks.return_value = _make_paging([])

        await engine.get_user_top_tracks(limit=0)

        mock_spotify.current_user_top_tracks.assert_awaited_once_with(
            time_range="medium_term", limit=1
        )

    async def test_empty_result(self, engine, mock_spotify):
        """No top tracks should return an empty list."""
        mock_spotify.current_user_top_tracks.return_value = _make_paging([])

        result = await engine.get_user_top_tracks()

        assert result == []

    async def test_none_items(self, engine, mock_spotify):
        """If paging.items is None, return an empty list."""
        mock_spotify.current_user_top_tracks.return_value = SimpleNamespace(
            items=None, next=None, total=0
        )

        result = await engine.get_user_top_tracks()

        assert result == []

    async def test_api_error(self, engine, mock_spotify):
        mock_spotify.current_user_top_tracks.side_effect = tk.HTTPError(
            "server error", request=MagicMock(), response=MagicMock()
        )

        with pytest.raises(tk.HTTPError):
            await engine.get_user_top_tracks()


# ===================================================================
# get_user_top_artists
# ===================================================================


class TestGetUserTopArtists:
    """Tests for DiscoveryEngine.get_user_top_artists."""

    async def test_default_params(self, engine, mock_spotify):
        artists = [_make_artist(f"a{i}") for i in range(5)]
        mock_spotify.current_user_top_artists.return_value = _make_paging(artists)

        result = await engine.get_user_top_artists()

        assert len(result) == 5
        mock_spotify.current_user_top_artists.assert_awaited_once_with(
            time_range="medium_term", limit=50
        )

    async def test_short_term(self, engine, mock_spotify):
        artists = [_make_artist("a1")]
        mock_spotify.current_user_top_artists.return_value = _make_paging(artists)

        result = await engine.get_user_top_artists(time_range="short_term", limit=5)

        mock_spotify.current_user_top_artists.assert_awaited_once_with(
            time_range="short_term", limit=5
        )
        assert len(result) == 1

    async def test_limit_clamped(self, engine, mock_spotify):
        mock_spotify.current_user_top_artists.return_value = _make_paging([])

        await engine.get_user_top_artists(limit=100)

        mock_spotify.current_user_top_artists.assert_awaited_once_with(
            time_range="medium_term", limit=50
        )

    async def test_empty_result(self, engine, mock_spotify):
        mock_spotify.current_user_top_artists.return_value = _make_paging([])

        result = await engine.get_user_top_artists()

        assert result == []

    async def test_none_items(self, engine, mock_spotify):
        mock_spotify.current_user_top_artists.return_value = SimpleNamespace(
            items=None, next=None, total=0
        )

        result = await engine.get_user_top_artists()

        assert result == []

    async def test_api_error(self, engine, mock_spotify):
        mock_spotify.current_user_top_artists.side_effect = tk.HTTPError(
            "auth error", request=MagicMock(), response=MagicMock()
        )

        with pytest.raises(tk.HTTPError):
            await engine.get_user_top_artists()


# ===================================================================
# get_recently_played
# ===================================================================


class TestGetRecentlyPlayed:
    """Tests for DiscoveryEngine.get_recently_played."""

    async def test_default_limit(self, engine, mock_spotify):
        items = [_make_play_history() for _ in range(20)]
        mock_spotify.playback_recently_played.return_value = _make_cursor_paging(items)

        result = await engine.get_recently_played()

        assert len(result) == 20
        mock_spotify.playback_recently_played.assert_awaited_once_with(limit=50)

    async def test_custom_limit(self, engine, mock_spotify):
        items = [_make_play_history() for _ in range(5)]
        mock_spotify.playback_recently_played.return_value = _make_cursor_paging(items)

        result = await engine.get_recently_played(limit=5)

        mock_spotify.playback_recently_played.assert_awaited_once_with(limit=5)
        assert len(result) == 5

    async def test_limit_clamped_high(self, engine, mock_spotify):
        mock_spotify.playback_recently_played.return_value = _make_cursor_paging([])

        await engine.get_recently_played(limit=200)

        mock_spotify.playback_recently_played.assert_awaited_once_with(limit=50)

    async def test_limit_clamped_low(self, engine, mock_spotify):
        mock_spotify.playback_recently_played.return_value = _make_cursor_paging([])

        await engine.get_recently_played(limit=-5)

        mock_spotify.playback_recently_played.assert_awaited_once_with(limit=1)

    async def test_empty_result(self, engine, mock_spotify):
        mock_spotify.playback_recently_played.return_value = _make_cursor_paging([])

        result = await engine.get_recently_played()

        assert result == []

    async def test_none_items(self, engine, mock_spotify):
        mock_spotify.playback_recently_played.return_value = SimpleNamespace(items=None, next=None)

        result = await engine.get_recently_played()

        assert result == []

    async def test_api_error(self, engine, mock_spotify):
        mock_spotify.playback_recently_played.side_effect = tk.HTTPError(
            "forbidden", request=MagicMock(), response=MagicMock()
        )

        with pytest.raises(tk.HTTPError):
            await engine.get_recently_played()


# ===================================================================
# find_deep_cuts
# ===================================================================


class TestFindDeepCuts:
    """Tests for DiscoveryEngine.find_deep_cuts."""

    async def test_basic_deep_cuts(self, engine, mock_spotify):
        """Tracks below the threshold are returned, sorted by popularity ascending."""
        albums = [_make_album("alb1")]
        album_tracks = [
            _make_simple_track("t1"),
            _make_simple_track("t2"),
            _make_simple_track("t3"),
        ]

        # Artist albums: single page, no next
        mock_spotify.artist_albums.return_value = _make_paging(albums, next_url=None)
        # Album tracks: single page, no next
        mock_spotify.album_tracks.return_value = _make_paging(album_tracks, next_url=None)

        # Full tracks: popularity varies
        full_tracks = [
            _make_track("t1", popularity=10),  # deep cut
            _make_track("t2", popularity=50),  # NOT deep cut
            _make_track("t3", popularity=20),  # deep cut
        ]
        mock_spotify.tracks.return_value = full_tracks

        result = await engine.find_deep_cuts("art1", popularity_threshold=30)

        assert len(result) == 2
        # Sorted by popularity ascending.
        assert result[0].popularity == 10
        assert result[1].popularity == 20

    async def test_no_deep_cuts_found(self, engine, mock_spotify):
        """All tracks above the threshold should yield an empty list."""
        albums = [_make_album("alb1")]
        album_tracks = [_make_simple_track("t1")]

        mock_spotify.artist_albums.return_value = _make_paging(albums, next_url=None)
        mock_spotify.album_tracks.return_value = _make_paging(album_tracks, next_url=None)
        mock_spotify.tracks.return_value = [_make_track("t1", popularity=80)]

        result = await engine.find_deep_cuts("art1", popularity_threshold=30)

        assert result == []

    async def test_custom_threshold(self, engine, mock_spotify):
        """A higher threshold includes more tracks."""
        albums = [_make_album("alb1")]
        album_tracks = [_make_simple_track("t1"), _make_simple_track("t2")]

        mock_spotify.artist_albums.return_value = _make_paging(albums, next_url=None)
        mock_spotify.album_tracks.return_value = _make_paging(album_tracks, next_url=None)
        mock_spotify.tracks.return_value = [
            _make_track("t1", popularity=40),
            _make_track("t2", popularity=60),
        ]

        result = await engine.find_deep_cuts("art1", popularity_threshold=50)

        assert len(result) == 1
        assert result[0].id == "t1"

    async def test_deduplicates_tracks(self, engine, mock_spotify):
        """Tracks appearing in multiple albums should only appear once."""
        albums = [_make_album("alb1"), _make_album("alb2")]
        # Both albums contain the same track.
        album_tracks = [_make_simple_track("t1")]

        mock_spotify.artist_albums.return_value = _make_paging(albums, next_url=None)
        mock_spotify.album_tracks.return_value = _make_paging(album_tracks, next_url=None)
        # tracks() is called twice (once per batch if IDs differ, but here both are "t1")
        mock_spotify.tracks.return_value = [
            _make_track("t1", popularity=10),
            _make_track("t1", popularity=10),  # duplicate
        ]

        result = await engine.find_deep_cuts("art1", popularity_threshold=30)

        assert len(result) == 1

    async def test_paginated_albums(self, engine, mock_spotify):
        """Albums spread across multiple pages are all collected."""
        page1_albums = [_make_album("alb1")]
        page2_albums = [_make_album("alb2")]

        page1 = _make_paging(page1_albums, next_url="https://next")
        page2 = _make_paging(page2_albums, next_url=None)

        mock_spotify.artist_albums.return_value = page1
        mock_spotify.next.side_effect = [
            page2,  # album pagination result
            None,  # extra safety
        ]

        # Each album has one track.
        mock_spotify.album_tracks.return_value = _make_paging(
            [_make_simple_track("t1")], next_url=None
        )
        mock_spotify.tracks.return_value = [_make_track("t1", popularity=5)]

        result = await engine.find_deep_cuts("art1", popularity_threshold=30)

        # album_tracks called for both albums
        assert mock_spotify.album_tracks.await_count == 2
        # But only one unique track
        assert len(result) == 1

    async def test_paginated_album_tracks(self, engine, mock_spotify):
        """Album tracks spread across multiple pages are all collected."""
        albums = [_make_album("alb1")]
        mock_spotify.artist_albums.return_value = _make_paging(albums, next_url=None)

        page1_tracks = [_make_simple_track("t1")]
        page2_tracks = [_make_simple_track("t2")]

        page1 = _make_paging(page1_tracks, next_url="https://next")
        page2 = _make_paging(page2_tracks, next_url=None)

        mock_spotify.album_tracks.return_value = page1
        # The first next() is for album_tracks pagination.
        mock_spotify.next.return_value = page2

        mock_spotify.tracks.return_value = [
            _make_track("t1", popularity=5),
            _make_track("t2", popularity=15),
        ]

        result = await engine.find_deep_cuts("art1", popularity_threshold=30)

        assert len(result) == 2

    async def test_no_albums(self, engine, mock_spotify):
        """An artist with no albums returns an empty list."""
        mock_spotify.artist_albums.return_value = _make_paging([], next_url=None)

        result = await engine.find_deep_cuts("art1")

        assert result == []
        mock_spotify.tracks.assert_not_awaited()

    async def test_tracks_with_none_id_skipped(self, engine, mock_spotify):
        """Simple tracks where id is None should be skipped."""
        albums = [_make_album("alb1")]
        mock_spotify.artist_albums.return_value = _make_paging(albums, next_url=None)

        album_tracks = [
            _make_simple_track("t1"),
            SimpleNamespace(id=None, name="Local File"),
        ]
        mock_spotify.album_tracks.return_value = _make_paging(album_tracks, next_url=None)
        mock_spotify.tracks.return_value = [_make_track("t1", popularity=10)]

        result = await engine.find_deep_cuts("art1", popularity_threshold=30)

        # Only t1 is fetched, local file is skipped.
        mock_spotify.tracks.assert_awaited_once_with(["t1"])
        assert len(result) == 1

    async def test_tracks_batched_at_50(self, engine, mock_spotify):
        """Track IDs are fetched in batches of 50."""
        albums = [_make_album("alb1")]
        mock_spotify.artist_albums.return_value = _make_paging(albums, next_url=None)

        # 75 tracks -> 2 batch calls to self._sp.tracks()
        album_tracks = [_make_simple_track(f"t{i}") for i in range(75)]
        mock_spotify.album_tracks.return_value = _make_paging(album_tracks, next_url=None)

        batch1 = [_make_track(f"t{i}", popularity=5) for i in range(50)]
        batch2 = [_make_track(f"t{i}", popularity=5) for i in range(50, 75)]
        mock_spotify.tracks.side_effect = [batch1, batch2]

        result = await engine.find_deep_cuts("art1", popularity_threshold=30)

        assert mock_spotify.tracks.await_count == 2
        assert len(result) == 75

    async def test_artist_albums_api_error(self, engine, mock_spotify):
        """HTTPError fetching artist albums should propagate."""
        mock_spotify.artist_albums.side_effect = tk.HTTPError(
            "not found", request=MagicMock(), response=MagicMock()
        )

        with pytest.raises(tk.HTTPError):
            await engine.find_deep_cuts("art_bad")

    async def test_album_tracks_error_skips_album(self, engine, mock_spotify):
        """An error fetching tracks for one album should skip it, not abort."""
        albums = [_make_album("alb_good"), _make_album("alb_bad")]
        mock_spotify.artist_albums.return_value = _make_paging(albums, next_url=None)

        def _album_tracks_side_effect(album_id, limit=50):
            if album_id == "alb_bad":
                raise tk.HTTPError("server error", request=MagicMock(), response=MagicMock())
            return _make_paging([_make_simple_track("t1")], next_url=None)

        mock_spotify.album_tracks.side_effect = _album_tracks_side_effect
        mock_spotify.tracks.return_value = [_make_track("t1", popularity=5)]

        result = await engine.find_deep_cuts("art1", popularity_threshold=30)

        # Only the good album's track is returned.
        assert len(result) == 1

    async def test_track_batch_error_continues(self, engine, mock_spotify):
        """An error fetching a batch of full tracks should skip it, not abort."""
        albums = [_make_album("alb1")]
        mock_spotify.artist_albums.return_value = _make_paging(albums, next_url=None)

        album_tracks = [_make_simple_track(f"t{i}") for i in range(75)]
        mock_spotify.album_tracks.return_value = _make_paging(album_tracks, next_url=None)

        # First batch succeeds, second fails.
        batch1 = [_make_track(f"t{i}", popularity=5) for i in range(50)]
        mock_spotify.tracks.side_effect = [
            batch1,
            tk.HTTPError("server error", request=MagicMock(), response=MagicMock()),
        ]

        result = await engine.find_deep_cuts("art1", popularity_threshold=30)

        # Only the first batch's tracks are included.
        assert len(result) == 50

    async def test_popularity_none_treated_as_zero(self, engine, mock_spotify):
        """Tracks with popularity=None that are < threshold are included."""
        albums = [_make_album("alb1")]
        mock_spotify.artist_albums.return_value = _make_paging(albums, next_url=None)
        mock_spotify.album_tracks.return_value = _make_paging(
            [_make_simple_track("t1")], next_url=None
        )

        track_with_none_popularity = SimpleNamespace(
            id="t1", name="T1", uri="spotify:track:t1", popularity=None
        )
        mock_spotify.tracks.return_value = [track_with_none_popularity]

        result = await engine.find_deep_cuts("art1", popularity_threshold=30)

        # popularity is None, which is not < 30, so it should NOT be included.
        assert len(result) == 0


# ===================================================================
# search_tracks
# ===================================================================


class TestSearchTracks:
    """Tests for DiscoveryEngine.search_tracks."""

    async def test_basic_search(self, engine, mock_spotify):
        """A simple query with no filters."""
        tracks = [_make_track("t1")]
        tracks_paging = _make_paging(tracks)
        mock_spotify.search.return_value = (tracks_paging,)

        result = await engine.search_tracks("indie rock")

        assert len(result) == 1
        mock_spotify.search.assert_awaited_once_with("indie rock", types=("track",), limit=50)

    async def test_search_with_year_filter(self, engine, mock_spotify):
        tracks_paging = _make_paging([_make_track("t1")])
        mock_spotify.search.return_value = (tracks_paging,)

        await engine.search_tracks("rock", filters={"year": "2020"})

        mock_spotify.search.assert_awaited_once_with("rock year:2020", types=("track",), limit=50)

    async def test_search_with_genre_filter(self, engine, mock_spotify):
        tracks_paging = _make_paging([_make_track("t1")])
        mock_spotify.search.return_value = (tracks_paging,)

        await engine.search_tracks("chill", filters={"genre": "ambient"})

        mock_spotify.search.assert_awaited_once_with(
            "chill genre:ambient", types=("track",), limit=50
        )

    async def test_search_with_artist_filter(self, engine, mock_spotify):
        tracks_paging = _make_paging([_make_track("t1")])
        mock_spotify.search.return_value = (tracks_paging,)

        await engine.search_tracks("love", filters={"artist": "Radiohead"})

        mock_spotify.search.assert_awaited_once_with(
            "love artist:Radiohead", types=("track",), limit=50
        )

    async def test_search_with_tag_filter(self, engine, mock_spotify):
        tracks_paging = _make_paging([_make_track("t1")])
        mock_spotify.search.return_value = (tracks_paging,)

        await engine.search_tracks("new music", filters={"tag": "hipster"})

        mock_spotify.search.assert_awaited_once_with(
            "new music tag:hipster", types=("track",), limit=50
        )

    async def test_search_with_multiple_filters(self, engine, mock_spotify):
        """Multiple filters should all be appended to the query."""
        tracks_paging = _make_paging([_make_track("t1")])
        mock_spotify.search.return_value = (tracks_paging,)

        await engine.search_tracks(
            "rock",
            filters={"year": "2023", "genre": "indie-rock", "artist": "Arctic Monkeys"},
        )

        call_query = mock_spotify.search.call_args.args[0]
        assert "rock" in call_query
        assert "year:2023" in call_query
        assert "genre:indie-rock" in call_query
        assert "artist:Arctic Monkeys" in call_query

    async def test_unsupported_filter_ignored(self, engine, mock_spotify):
        """Unsupported filter keys should be silently ignored."""
        tracks_paging = _make_paging([_make_track("t1")])
        mock_spotify.search.return_value = (tracks_paging,)

        await engine.search_tracks("test", filters={"mood": "happy", "year": "2024"})

        call_query = mock_spotify.search.call_args.args[0]
        assert "year:2024" in call_query
        assert "mood:" not in call_query

    async def test_search_custom_limit(self, engine, mock_spotify):
        tracks_paging = _make_paging([])
        mock_spotify.search.return_value = (tracks_paging,)

        await engine.search_tracks("test", limit=10)

        mock_spotify.search.assert_awaited_once_with("test", types=("track",), limit=10)

    async def test_search_limit_clamped_high(self, engine, mock_spotify):
        tracks_paging = _make_paging([])
        mock_spotify.search.return_value = (tracks_paging,)

        await engine.search_tracks("test", limit=999)

        mock_spotify.search.assert_awaited_once_with("test", types=("track",), limit=50)

    async def test_search_limit_clamped_low(self, engine, mock_spotify):
        tracks_paging = _make_paging([])
        mock_spotify.search.return_value = (tracks_paging,)

        await engine.search_tracks("test", limit=-1)

        mock_spotify.search.assert_awaited_once_with("test", types=("track",), limit=1)

    async def test_search_empty_results(self, engine, mock_spotify):
        tracks_paging = _make_paging([])
        mock_spotify.search.return_value = (tracks_paging,)

        result = await engine.search_tracks("asdfghjkl")

        assert result == []

    async def test_search_none_items(self, engine, mock_spotify):
        tracks_paging = SimpleNamespace(items=None, next=None, total=0)
        mock_spotify.search.return_value = (tracks_paging,)

        result = await engine.search_tracks("test")

        assert result == []

    async def test_search_api_error(self, engine, mock_spotify):
        mock_spotify.search.side_effect = tk.HTTPError(
            "rate limited", request=MagicMock(), response=MagicMock()
        )

        with pytest.raises(tk.HTTPError):
            await engine.search_tracks("test")

    async def test_search_no_filters(self, engine, mock_spotify):
        """Passing filters=None should use the raw query unchanged."""
        tracks_paging = _make_paging([])
        mock_spotify.search.return_value = (tracks_paging,)

        await engine.search_tracks("hello", filters=None)

        mock_spotify.search.assert_awaited_once_with("hello", types=("track",), limit=50)

    async def test_search_empty_filters(self, engine, mock_spotify):
        """An empty filters dict should use the raw query unchanged."""
        tracks_paging = _make_paging([])
        mock_spotify.search.return_value = (tracks_paging,)

        await engine.search_tracks("hello", filters={})

        mock_spotify.search.assert_awaited_once_with("hello", types=("track",), limit=50)


# ===================================================================
# build_genre_playlist
# ===================================================================


class TestBuildGenrePlaylist:
    """Tests for DiscoveryEngine.build_genre_playlist."""

    async def test_genre_playlist_delegates_to_search(self, engine, mock_spotify):
        """build_genre_playlist should call search_tracks with the genre filter."""
        tracks = [_make_track("t1"), _make_track("t2")]
        tracks_paging = _make_paging(tracks)
        mock_spotify.search.return_value = (tracks_paging,)

        result = await engine.build_genre_playlist("jazz", limit=20)

        assert len(result) == 2
        # The query should include both the genre as free text and as a filter.
        call_query = mock_spotify.search.call_args.args[0]
        assert "jazz" in call_query
        assert "genre:jazz" in call_query
        mock_spotify.search.assert_awaited_once_with("jazz genre:jazz", types=("track",), limit=20)

    async def test_genre_playlist_default_limit(self, engine, mock_spotify):
        tracks_paging = _make_paging([])
        mock_spotify.search.return_value = (tracks_paging,)

        await engine.build_genre_playlist("electronic")

        mock_spotify.search.assert_awaited_once_with(
            "electronic genre:electronic", types=("track",), limit=50
        )

    async def test_genre_playlist_empty_result(self, engine, mock_spotify):
        tracks_paging = _make_paging([])
        mock_spotify.search.return_value = (tracks_paging,)

        result = await engine.build_genre_playlist("nonexistent-genre-xyz")

        assert result == []


# ===================================================================
# build_time_capsule
# ===================================================================


class TestBuildTimeCapsule:
    """Tests for DiscoveryEngine.build_time_capsule."""

    async def test_default_time_range(self, engine, mock_spotify):
        """Default time_range is short_term, limit=50."""
        tracks = [_make_track(f"t{i}") for i in range(50)]
        mock_spotify.current_user_top_tracks.return_value = _make_paging(tracks)

        result = await engine.build_time_capsule()

        assert len(result) == 50
        mock_spotify.current_user_top_tracks.assert_awaited_once_with(
            time_range="short_term", limit=50
        )

    async def test_custom_time_range(self, engine, mock_spotify):
        tracks = [_make_track(f"t{i}") for i in range(30)]
        mock_spotify.current_user_top_tracks.return_value = _make_paging(tracks)

        result = await engine.build_time_capsule(time_range="long_term")

        mock_spotify.current_user_top_tracks.assert_awaited_once_with(
            time_range="long_term", limit=50
        )
        assert len(result) == 30

    async def test_time_capsule_empty(self, engine, mock_spotify):
        mock_spotify.current_user_top_tracks.return_value = _make_paging([])

        result = await engine.build_time_capsule()

        assert result == []

    async def test_time_capsule_api_error(self, engine, mock_spotify):
        """API errors in the underlying call should propagate."""
        mock_spotify.current_user_top_tracks.side_effect = tk.HTTPError(
            "error", request=MagicMock(), response=MagicMock()
        )

        with pytest.raises(tk.HTTPError):
            await engine.build_time_capsule()


# ===================================================================
# Serializers (module-level, shared by web routes and CLI)
# ===================================================================


class TestSerializers:
    """track_to_dict / artist_to_dict convert Tekore models to plain dicts."""

    def test_track_to_dict_full(self):
        track = SimpleNamespace(
            id="t1",
            name="Song",
            artists=[SimpleNamespace(name="A"), SimpleNamespace(name="B")],
            album=SimpleNamespace(id="alb1", name="Album"),
            duration_ms=210_000,
            popularity=63,
        )
        result = track_to_dict(track)
        assert result["spotify_id"] == "t1"
        assert result["name"] == "Song"
        assert result["artist_names"] == ["A", "B"]
        assert result["album_name"] == "Album"
        assert result["album_id"] == "alb1"
        assert result["duration_ms"] == 210_000
        assert result["popularity"] == 63
        assert result["isrc"] is None
        assert isinstance(result["cached_at"], str)

    def test_track_to_dict_handles_missing_fields(self):
        track = SimpleNamespace(
            id=None, name=None, artists=None, album=None, duration_ms=None, popularity=None
        )
        result = track_to_dict(track)
        assert result["spotify_id"] == ""
        assert result["name"] == ""
        assert result["artist_names"] == []
        assert result["album_name"] is None
        assert result["album_id"] is None
        assert result["duration_ms"] == 0

    def test_artist_to_dict_full(self):
        artist = SimpleNamespace(
            id="a1",
            name="Artist",
            genres=["jazz", "bop"],
            popularity=71,
            followers=SimpleNamespace(total=1234),
        )
        result = artist_to_dict(artist)
        assert result == {
            "id": "a1",
            "name": "Artist",
            "genres": ["jazz", "bop"],
            "popularity": 71,
            "followers": 1234,
        }

    def test_artist_to_dict_handles_missing_fields(self):
        artist = SimpleNamespace(id=None, name=None, genres=None, popularity=None, followers=None)
        result = artist_to_dict(artist)
        assert result["id"] == ""
        assert result["name"] == ""
        assert result["genres"] == []
        assert result["followers"] == 0


# ===================================================================
# FakeSpotify-backed tests: real request/response behavior
# ===================================================================


@pytest.fixture()
async def fake_client(fake_spotify):
    """A real async tekore client wired to the fake backend."""
    fake_spotify.add_user("user1")
    client = fake_spotify.async_client("user1")
    yield fake_spotify, client
    await client.close()


class TestAgainstFakeBackend:
    async def test_top_tracks_round_trip(self, fake_client):
        fake, client = fake_client
        for i in range(5):
            fake.add_track(f"top{i}", name=f"Top {i}")
        fake.top_tracks["user1"] = [f"top{i}" for i in range(5)]

        result = await DiscoveryEngine(client).get_user_top_tracks(limit=5)

        assert [t.id for t in result] == [f"top{i}" for i in range(5)]

    async def test_top_artists_round_trip(self, fake_client):
        fake, client = fake_client
        from tests.fake_spotify import _full_artist

        fake.top_artists["user1"] = [_full_artist("a1", "Artist One", genres=["jazz"])]

        result = await DiscoveryEngine(client).get_user_top_artists(limit=10)

        assert len(result) == 1
        assert result[0].id == "a1"
        assert result[0].genres == ["jazz"]

    async def test_search_tracks_matches_by_name(self, fake_client):
        fake, client = fake_client
        fake.add_track("d1", name="Dream On")
        fake.add_track("d2", name="Daydream")
        fake.add_track("x1", name="Something Else")

        result = await DiscoveryEngine(client).search_tracks("dream")

        assert {t.id for t in result} == {"d1", "d2"}

    async def test_build_genre_playlist_respects_limit(self, fake_client):
        fake, client = fake_client
        for i in range(4):
            fake.add_track(f"g{i}", name=f"Jazz Track {i}")

        result = await DiscoveryEngine(client).build_genre_playlist("jazz", limit=2)

        assert len(result) == 2
        assert all(t.uri is not None for t in result)

    async def test_deep_cuts_paginates_and_batches(self, fake_client):
        """55 tracks in one album: 2 album-track pages, 2 /tracks batches,
        filtered below the threshold and sorted ascending."""
        fake, client = fake_client
        ids = []
        for i in range(55):
            tid = f"dc{i:02d}"
            # popularity 0..54; threshold 30 keeps 30 tracks
            fake.add_track(tid, popularity=i, artist_id="art1", album_id="alb1")
            ids.append(tid)
        fake.artist_albums["art1"] = ["alb1"]
        fake.album_tracks["alb1"] = ids

        result = await DiscoveryEngine(client).find_deep_cuts("art1", popularity_threshold=30)

        assert len(result) == 30
        assert [t.popularity for t in result] == sorted(t.popularity for t in result)
        assert all(t.popularity < 30 for t in result)
        # Pagination/batching actually happened on the wire.
        album_gets = [r for r in fake.requests if r == ("GET", "/v1/albums/alb1/tracks")]
        assert len(album_gets) == 2
        batch_gets = [r for r in fake.requests if r == ("GET", "/v1/tracks")]
        assert len(batch_gets) == 2

    async def test_deep_cuts_dedupes_across_albums(self, fake_client):
        fake, client = fake_client
        fake.add_track("shared", popularity=5, artist_id="art1", album_id="alb1")
        # Register the same track under a second album too.
        fake.add_track("only2", popularity=7, artist_id="art1", album_id="alb2")
        fake.artist_albums["art1"] = ["alb1", "alb2"]
        fake.album_tracks["alb1"] = ["shared"]
        fake.album_tracks["alb2"] = ["shared", "only2"]

        result = await DiscoveryEngine(client).find_deep_cuts("art1", popularity_threshold=30)

        assert sorted(t.id for t in result) == ["only2", "shared"]


# ===================================================================
# Edge cases
# ===================================================================


class TestEdgeCases:
    """Miscellaneous edge-case tests for DiscoveryEngine."""

    async def test_engine_stores_spotify_client(self, mock_spotify):
        """The engine should store the client reference."""
        eng = DiscoveryEngine(mock_spotify)
        assert eng._sp is mock_spotify

    async def test_deep_cuts_threshold_zero(self, engine, mock_spotify):
        """A threshold of 0 means no tracks qualify (popularity must be < 0)."""
        albums = [_make_album("alb1")]
        mock_spotify.artist_albums.return_value = _make_paging(albums, next_url=None)
        mock_spotify.album_tracks.return_value = _make_paging(
            [_make_simple_track("t1")], next_url=None
        )
        mock_spotify.tracks.return_value = [_make_track("t1", popularity=0)]

        result = await engine.find_deep_cuts("art1", popularity_threshold=0)

        assert result == []

    async def test_deep_cuts_threshold_100_includes_most(self, engine, mock_spotify):
        """A threshold of 100 includes tracks with popularity 0-99."""
        albums = [_make_album("alb1")]
        mock_spotify.artist_albums.return_value = _make_paging(albums, next_url=None)

        album_tracks = [_make_simple_track("t1"), _make_simple_track("t2")]
        mock_spotify.album_tracks.return_value = _make_paging(album_tracks, next_url=None)
        mock_spotify.tracks.return_value = [
            _make_track("t1", popularity=99),
            _make_track("t2", popularity=100),
        ]

        result = await engine.find_deep_cuts("art1", popularity_threshold=100)

        assert len(result) == 1
        assert result[0].id == "t1"

    async def test_search_all_four_filters(self, engine, mock_spotify):
        """All four supported filter types applied simultaneously."""
        tracks_paging = _make_paging([_make_track("t1")])
        mock_spotify.search.return_value = (tracks_paging,)

        await engine.search_tracks(
            "test",
            filters={"year": "2024", "genre": "pop", "artist": "Taylor Swift", "tag": "new"},
        )

        call_query = mock_spotify.search.call_args.args[0]
        assert "year:2024" in call_query
        assert "genre:pop" in call_query
        assert "artist:Taylor Swift" in call_query
        assert "tag:new" in call_query

    async def test_time_capsule_medium_term(self, engine, mock_spotify):
        """Time capsule with medium_term explicitly set."""
        tracks = [_make_track("t1")]
        mock_spotify.current_user_top_tracks.return_value = _make_paging(tracks)

        result = await engine.build_time_capsule(time_range="medium_term")

        mock_spotify.current_user_top_tracks.assert_awaited_once_with(
            time_range="medium_term", limit=50
        )
        assert len(result) == 1
