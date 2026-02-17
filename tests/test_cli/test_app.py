"""Comprehensive tests for the SpotifyForge Typer CLI application.

Uses ``typer.testing.CliRunner`` and ``unittest.mock.patch`` to exercise
every command group (auth, playlist, discover, schedule, config) without
making real network calls or touching a real database.

The CLI functions use lazy imports of the form::

    from spotifyforge.core.playlist_manager import PlaylistManager

Because ``asyncio.run`` is invoked inside the CLI functions (via ``_run``),
mocked async methods must return real coroutines.  We achieve this by using
``unittest.mock.AsyncMock`` for async methods and patching at the
``spotifyforge.cli.app._run`` level or the lazy-import module level.
"""

from __future__ import annotations

import json
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from spotifyforge.cli.app import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helper: build a fake module that Python's import machinery will accept
# ---------------------------------------------------------------------------

def _fake_module(name: str, **attrs) -> types.ModuleType:
    """Create a real ``ModuleType`` with the given attributes.

    ``patch.dict("sys.modules", ...)`` requires a real module (or at least
    an object whose attribute access works normally) for ``from mod import X``
    to succeed inside the patched block.
    """
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


# =========================================================================
# --version / --help
# =========================================================================


class TestRootApp:
    """Tests for the root ``spotifyforge`` command."""

    def test_version_flag(self):
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert "SpotifyForge" in result.output
        assert "0.1.0" in result.output

    def test_short_version_flag(self):
        result = runner.invoke(app, ["-V"])
        assert result.exit_code == 0
        assert "0.1.0" in result.output

    def test_help_flag(self):
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        # All command groups should appear in help output
        assert "auth" in result.output
        assert "playlist" in result.output
        assert "discover" in result.output
        assert "schedule" in result.output
        assert "config" in result.output

    def test_no_args_shows_help_text(self):
        """With no_args_is_help=True the CLI prints help and exits with code 0."""
        result = runner.invoke(app, [])
        # Typer may exit with 0 or 2 depending on version; the key is that
        # usage information is printed.
        assert "auth" in result.output
        assert "playlist" in result.output


# =========================================================================
# AUTH commands
# =========================================================================


class TestAuthCommands:
    """Tests for the ``auth`` sub-command group."""

    def test_auth_help(self):
        result = runner.invoke(app, ["auth", "--help"])
        assert result.exit_code == 0
        assert "login" in result.output
        assert "status" in result.output
        assert "logout" in result.output

    # -- auth status -------------------------------------------------------

    def test_auth_status_logged_in(self):
        mock_auth_instance = MagicMock()
        mock_auth_instance.status = AsyncMock(return_value={
            "logged_in": True,
            "display_name": "Test User",
            "email": "test@example.com",
            "user_id": "abc123",
            "token_expiry": "2026-12-31T23:59:59",
            "token_valid": True,
        })
        MockSpotifyAuth = MagicMock(return_value=mock_auth_instance)

        fake_mod = _fake_module("spotifyforge.auth.oauth", SpotifyAuth=MockSpotifyAuth)

        with patch.dict("sys.modules", {"spotifyforge.auth.oauth": fake_mod}):
            result = runner.invoke(app, ["auth", "status"])

        assert result.exit_code == 0
        assert "Test User" in result.output
        assert "Auth Status" in result.output

    def test_auth_status_not_logged_in(self):
        mock_auth_instance = MagicMock()
        mock_auth_instance.status = AsyncMock(return_value={"logged_in": False})
        MockSpotifyAuth = MagicMock(return_value=mock_auth_instance)

        fake_mod = _fake_module("spotifyforge.auth.oauth", SpotifyAuth=MockSpotifyAuth)

        with patch.dict("sys.modules", {"spotifyforge.auth.oauth": fake_mod}):
            result = runner.invoke(app, ["auth", "status"])

        assert result.exit_code == 0
        assert "Not logged in" in result.output

    def test_auth_status_exception_shows_error(self):
        mock_auth_instance = MagicMock()
        mock_auth_instance.status = AsyncMock(side_effect=Exception("token expired"))
        MockSpotifyAuth = MagicMock(return_value=mock_auth_instance)

        fake_mod = _fake_module("spotifyforge.auth.oauth", SpotifyAuth=MockSpotifyAuth)

        with patch.dict("sys.modules", {"spotifyforge.auth.oauth": fake_mod}):
            result = runner.invoke(app, ["auth", "status"])

        assert result.exit_code == 1

    # -- auth login --------------------------------------------------------

    def test_auth_login_success(self):
        mock_auth_instance = MagicMock()
        mock_auth_instance.login = AsyncMock(return_value=None)
        MockSpotifyAuth = MagicMock(return_value=mock_auth_instance)

        fake_mod = _fake_module("spotifyforge.auth.oauth", SpotifyAuth=MockSpotifyAuth)

        with patch.dict("sys.modules", {"spotifyforge.auth.oauth": fake_mod}):
            result = runner.invoke(app, ["auth", "login"])

        assert result.exit_code == 0
        assert "Successfully authenticated" in result.output

    def test_auth_login_failure(self):
        mock_auth_instance = MagicMock()
        mock_auth_instance.login = AsyncMock(side_effect=Exception("browser error"))
        MockSpotifyAuth = MagicMock(return_value=mock_auth_instance)

        fake_mod = _fake_module("spotifyforge.auth.oauth", SpotifyAuth=MockSpotifyAuth)

        with patch.dict("sys.modules", {"spotifyforge.auth.oauth": fake_mod}):
            result = runner.invoke(app, ["auth", "login"])

        assert result.exit_code == 1

    # -- auth logout -------------------------------------------------------

    def test_auth_logout_success(self):
        mock_auth_instance = MagicMock()
        mock_auth_instance.logout = AsyncMock(return_value=None)
        MockSpotifyAuth = MagicMock(return_value=mock_auth_instance)

        fake_mod = _fake_module("spotifyforge.auth.oauth", SpotifyAuth=MockSpotifyAuth)

        with patch.dict("sys.modules", {"spotifyforge.auth.oauth": fake_mod}):
            result = runner.invoke(app, ["auth", "logout"])

        assert result.exit_code == 0
        assert "Logged out successfully" in result.output

    def test_auth_logout_failure(self):
        mock_auth_instance = MagicMock()
        mock_auth_instance.logout = AsyncMock(side_effect=Exception("storage error"))
        MockSpotifyAuth = MagicMock(return_value=mock_auth_instance)

        fake_mod = _fake_module("spotifyforge.auth.oauth", SpotifyAuth=MockSpotifyAuth)

        with patch.dict("sys.modules", {"spotifyforge.auth.oauth": fake_mod}):
            result = runner.invoke(app, ["auth", "logout"])

        assert result.exit_code == 1


# =========================================================================
# PLAYLIST commands
# =========================================================================


class TestPlaylistCommands:
    """Tests for the ``playlist`` sub-command group."""

    def test_playlist_help(self):
        result = runner.invoke(app, ["playlist", "--help"])
        assert result.exit_code == 0
        assert "list" in result.output
        assert "create" in result.output
        assert "deduplicate" in result.output
        assert "export" in result.output

    # -- playlist list -----------------------------------------------------

    def test_playlist_list_with_playlists(self):
        mock_manager = MagicMock()
        mock_manager.get_user_playlists = AsyncMock(return_value=[
            {
                "name": "Chill Vibes",
                "track_count": 42,
                "public": True,
                "followers": 10,
                "id": "pl_001",
            },
            {
                "name": "Workout Mix",
                "track_count": 30,
                "public": False,
                "followers": 0,
                "id": "pl_002",
            },
        ])
        MockPM = MagicMock(return_value=mock_manager)
        fake_mod = _fake_module("spotifyforge.core.playlist_manager", PlaylistManager=MockPM)

        with patch.dict("sys.modules", {"spotifyforge.core.playlist_manager": fake_mod}):
            result = runner.invoke(app, ["playlist", "list"])

        assert result.exit_code == 0
        assert "Chill Vibes" in result.output
        assert "Workout Mix" in result.output
        assert "42" in result.output

    def test_playlist_list_empty(self):
        mock_manager = MagicMock()
        mock_manager.get_user_playlists = AsyncMock(return_value=[])
        MockPM = MagicMock(return_value=mock_manager)
        fake_mod = _fake_module("spotifyforge.core.playlist_manager", PlaylistManager=MockPM)

        with patch.dict("sys.modules", {"spotifyforge.core.playlist_manager": fake_mod}):
            result = runner.invoke(app, ["playlist", "list"])

        assert result.exit_code == 0
        assert "No playlists found" in result.output

    def test_playlist_list_fetch_error(self):
        mock_manager = MagicMock()
        mock_manager.get_user_playlists = AsyncMock(side_effect=Exception("API error"))
        MockPM = MagicMock(return_value=mock_manager)
        fake_mod = _fake_module("spotifyforge.core.playlist_manager", PlaylistManager=MockPM)

        with patch.dict("sys.modules", {"spotifyforge.core.playlist_manager": fake_mod}):
            result = runner.invoke(app, ["playlist", "list"])

        assert result.exit_code == 1

    # -- playlist create ---------------------------------------------------

    def test_playlist_create_success(self):
        mock_manager = MagicMock()
        mock_manager.create_playlist = AsyncMock(return_value={
            "name": "My New Playlist",
            "id": "pl_new_001",
        })
        MockPM = MagicMock(return_value=mock_manager)
        fake_mod = _fake_module("spotifyforge.core.playlist_manager", PlaylistManager=MockPM)

        with patch.dict("sys.modules", {"spotifyforge.core.playlist_manager": fake_mod}):
            result = runner.invoke(
                app, ["playlist", "create", "My New Playlist", "-d", "A great playlist"]
            )

        assert result.exit_code == 0
        assert "Playlist created" in result.output
        assert "My New Playlist" in result.output

    def test_playlist_create_private(self):
        mock_manager = MagicMock()
        mock_manager.create_playlist = AsyncMock(return_value={
            "name": "Secret Mix",
            "id": "pl_priv_001",
        })
        MockPM = MagicMock(return_value=mock_manager)
        fake_mod = _fake_module("spotifyforge.core.playlist_manager", PlaylistManager=MockPM)

        with patch.dict("sys.modules", {"spotifyforge.core.playlist_manager": fake_mod}):
            result = runner.invoke(
                app, ["playlist", "create", "Secret Mix", "--private"]
            )

        assert result.exit_code == 0
        assert "Private" in result.output

    def test_playlist_create_failure(self):
        mock_manager = MagicMock()
        mock_manager.create_playlist = AsyncMock(side_effect=Exception("quota exceeded"))
        MockPM = MagicMock(return_value=mock_manager)
        fake_mod = _fake_module("spotifyforge.core.playlist_manager", PlaylistManager=MockPM)

        with patch.dict("sys.modules", {"spotifyforge.core.playlist_manager": fake_mod}):
            result = runner.invoke(app, ["playlist", "create", "Broken Playlist"])

        assert result.exit_code == 1

    # -- playlist deduplicate ----------------------------------------------

    def test_playlist_deduplicate_found_duplicates(self):
        mock_manager = MagicMock()
        mock_manager.deduplicate = AsyncMock(return_value={
            "removed": 5,
            "remaining": 35,
        })
        MockPM = MagicMock(return_value=mock_manager)
        fake_mod = _fake_module("spotifyforge.core.playlist_manager", PlaylistManager=MockPM)

        with patch.dict("sys.modules", {"spotifyforge.core.playlist_manager": fake_mod}):
            result = runner.invoke(app, ["playlist", "deduplicate", "pl_001"])

        assert result.exit_code == 0
        assert "5" in result.output
        assert "Deduplication complete" in result.output

    def test_playlist_deduplicate_no_duplicates(self):
        mock_manager = MagicMock()
        mock_manager.deduplicate = AsyncMock(return_value={"removed": 0})
        MockPM = MagicMock(return_value=mock_manager)
        fake_mod = _fake_module("spotifyforge.core.playlist_manager", PlaylistManager=MockPM)

        with patch.dict("sys.modules", {"spotifyforge.core.playlist_manager": fake_mod}):
            result = runner.invoke(app, ["playlist", "deduplicate", "pl_clean"])

        assert result.exit_code == 0
        assert "No duplicates found" in result.output

    def test_playlist_deduplicate_failure(self):
        mock_manager = MagicMock()
        mock_manager.deduplicate = AsyncMock(side_effect=Exception("rate limited"))
        MockPM = MagicMock(return_value=mock_manager)
        fake_mod = _fake_module("spotifyforge.core.playlist_manager", PlaylistManager=MockPM)

        with patch.dict("sys.modules", {"spotifyforge.core.playlist_manager": fake_mod}):
            result = runner.invoke(app, ["playlist", "deduplicate", "pl_err"])

        assert result.exit_code == 1

    # -- playlist sync -----------------------------------------------------

    def test_playlist_sync_success(self):
        mock_manager = MagicMock()
        mock_manager.sync_playlist = AsyncMock(return_value={
            "name": "Synced Playlist",
            "tracks_synced": 55,
        })
        MockPM = MagicMock(return_value=mock_manager)
        fake_mod = _fake_module("spotifyforge.core.playlist_manager", PlaylistManager=MockPM)

        with patch.dict("sys.modules", {"spotifyforge.core.playlist_manager": fake_mod}):
            result = runner.invoke(app, ["playlist", "sync", "pl_sync_001"])

        assert result.exit_code == 0
        assert "Sync Complete" in result.output
        assert "55" in result.output

    # -- playlist show -----------------------------------------------------

    def test_playlist_show_success(self):
        mock_manager = MagicMock()
        mock_manager.get_playlist_details = AsyncMock(return_value={
            "meta": {
                "name": "Test Playlist",
                "description": "A test description",
                "owner": "testuser",
                "track_count": 3,
                "followers": 100,
                "public": True,
            },
            "tracks": [
                {
                    "name": "Track One",
                    "artist": "Artist A",
                    "album": "Album X",
                    "duration_ms": 210000,
                },
                {
                    "name": "Track Two",
                    "artist": "Artist B",
                    "album": "Album Y",
                    "duration_ms": 180000,
                },
            ],
        })
        MockPM = MagicMock(return_value=mock_manager)
        fake_mod = _fake_module("spotifyforge.core.playlist_manager", PlaylistManager=MockPM)

        with patch.dict("sys.modules", {"spotifyforge.core.playlist_manager": fake_mod}):
            result = runner.invoke(app, ["playlist", "show", "pl_show_001"])

        assert result.exit_code == 0
        assert "Test Playlist" in result.output
        assert "Track One" in result.output

    def test_playlist_show_empty_tracks(self):
        mock_manager = MagicMock()
        mock_manager.get_playlist_details = AsyncMock(return_value={
            "meta": {
                "name": "Empty Playlist",
                "description": "",
                "owner": "testuser",
                "track_count": 0,
                "followers": 0,
                "public": False,
            },
            "tracks": [],
        })
        MockPM = MagicMock(return_value=mock_manager)
        fake_mod = _fake_module("spotifyforge.core.playlist_manager", PlaylistManager=MockPM)

        with patch.dict("sys.modules", {"spotifyforge.core.playlist_manager": fake_mod}):
            result = runner.invoke(app, ["playlist", "show", "pl_empty_001"])

        assert result.exit_code == 0
        assert "Playlist has no tracks" in result.output

    # -- playlist export ---------------------------------------------------

    def test_playlist_export_json_stdout(self):
        tracks_data = [
            {
                "name": "Song A",
                "artist": "Artist 1",
                "album": "Album 1",
                "duration_ms": 200000,
                "uri": "spotify:track:aaa",
            },
            {
                "name": "Song B",
                "artist": "Artist 2",
                "album": "Album 2",
                "duration_ms": 180000,
                "uri": "spotify:track:bbb",
            },
        ]
        mock_manager = MagicMock()
        mock_manager.get_playlist_details = AsyncMock(return_value={
            "meta": {"name": "Export Playlist"},
            "tracks": tracks_data,
        })
        MockPM = MagicMock(return_value=mock_manager)
        fake_mod = _fake_module("spotifyforge.core.playlist_manager", PlaylistManager=MockPM)

        with patch.dict("sys.modules", {"spotifyforge.core.playlist_manager": fake_mod}):
            result = runner.invoke(
                app, ["playlist", "export", "pl_export_001", "--format", "json"]
            )

        assert result.exit_code == 0
        assert "Song A" in result.output
        assert "Song B" in result.output
        assert "spotify:track:aaa" in result.output

    def test_playlist_export_csv_stdout(self):
        tracks_data = [
            {
                "name": "Song A",
                "artist": "Artist 1",
                "album": "Album 1",
                "duration_ms": 200000,
                "uri": "spotify:track:aaa",
            },
        ]
        mock_manager = MagicMock()
        mock_manager.get_playlist_details = AsyncMock(return_value={
            "meta": {"name": "CSV Export"},
            "tracks": tracks_data,
        })
        MockPM = MagicMock(return_value=mock_manager)
        fake_mod = _fake_module("spotifyforge.core.playlist_manager", PlaylistManager=MockPM)

        with patch.dict("sys.modules", {"spotifyforge.core.playlist_manager": fake_mod}):
            result = runner.invoke(
                app, ["playlist", "export", "pl_export_002", "--format", "csv"]
            )

        assert result.exit_code == 0
        assert "name" in result.output
        assert "Song A" in result.output

    def test_playlist_export_json_to_file(self, tmp_path):
        tracks_data = [
            {
                "name": "File Song",
                "artist": "File Artist",
                "album": "File Album",
                "duration_ms": 300000,
                "uri": "spotify:track:file1",
            },
        ]
        mock_manager = MagicMock()
        mock_manager.get_playlist_details = AsyncMock(return_value={
            "meta": {"name": "File Export"},
            "tracks": tracks_data,
        })
        MockPM = MagicMock(return_value=mock_manager)
        fake_mod = _fake_module("spotifyforge.core.playlist_manager", PlaylistManager=MockPM)

        output_file = tmp_path / "export.json"
        with patch.dict("sys.modules", {"spotifyforge.core.playlist_manager": fake_mod}):
            result = runner.invoke(
                app,
                [
                    "playlist", "export", "pl_file_001",
                    "--format", "json",
                    "--output", str(output_file),
                ],
            )

        assert result.exit_code == 0
        assert "Exported" in result.output
        assert output_file.exists()
        data = json.loads(output_file.read_text())
        assert len(data) == 1
        assert data[0]["name"] == "File Song"

    def test_playlist_export_csv_to_file(self, tmp_path):
        tracks_data = [
            {
                "name": "CSV Song",
                "artist": "CSV Artist",
                "album": "CSV Album",
                "duration_ms": 250000,
                "uri": "spotify:track:csv1",
            },
        ]
        mock_manager = MagicMock()
        mock_manager.get_playlist_details = AsyncMock(return_value={
            "meta": {"name": "CSV File Export"},
            "tracks": tracks_data,
        })
        MockPM = MagicMock(return_value=mock_manager)
        fake_mod = _fake_module("spotifyforge.core.playlist_manager", PlaylistManager=MockPM)

        output_file = tmp_path / "export.csv"
        with patch.dict("sys.modules", {"spotifyforge.core.playlist_manager": fake_mod}):
            result = runner.invoke(
                app,
                [
                    "playlist", "export", "pl_csv_001",
                    "--format", "csv",
                    "--output", str(output_file),
                ],
            )

        assert result.exit_code == 0
        assert "Exported" in result.output
        assert output_file.exists()
        content = output_file.read_text()
        assert "name,artist,album,duration_ms,uri" in content
        assert "CSV Song" in content

    def test_playlist_export_empty_playlist(self):
        mock_manager = MagicMock()
        mock_manager.get_playlist_details = AsyncMock(return_value={
            "meta": {"name": "Empty"},
            "tracks": [],
        })
        MockPM = MagicMock(return_value=mock_manager)
        fake_mod = _fake_module("spotifyforge.core.playlist_manager", PlaylistManager=MockPM)

        with patch.dict("sys.modules", {"spotifyforge.core.playlist_manager": fake_mod}):
            result = runner.invoke(
                app, ["playlist", "export", "pl_empty", "--format", "json"]
            )

        assert result.exit_code == 1


# =========================================================================
# DISCOVER commands
# =========================================================================


class TestDiscoverCommands:
    """Tests for the ``discover`` sub-command group."""

    def test_discover_help(self):
        result = runner.invoke(app, ["discover", "--help"])
        assert result.exit_code == 0
        assert "top-tracks" in result.output
        assert "deep-cuts" in result.output
        assert "genre" in result.output
        assert "time-capsule" in result.output

    # -- top-tracks --------------------------------------------------------

    def test_discover_top_tracks_success(self):
        mock_discovery = MagicMock()
        mock_discovery.get_top_tracks = AsyncMock(return_value=[
            {
                "name": "Hit Song",
                "artist": "Pop Star",
                "album": "Greatest Hits",
                "popularity": 95,
            },
            {
                "name": "Another Hit",
                "artist": "Rock Band",
                "album": "Rock Album",
                "popularity": 80,
            },
        ])
        MockDiscoveryEngine = MagicMock(return_value=mock_discovery)
        fake_mod = _fake_module("spotifyforge.core.discovery", DiscoveryEngine=MockDiscoveryEngine)

        with patch.dict("sys.modules", {"spotifyforge.core.discovery": fake_mod}):
            result = runner.invoke(app, ["discover", "top-tracks"])

        assert result.exit_code == 0
        assert "Hit Song" in result.output
        assert "Pop Star" in result.output
        assert "95" in result.output

    def test_discover_top_tracks_with_options(self):
        mock_discovery = MagicMock()
        mock_discovery.get_top_tracks = AsyncMock(return_value=[
            {"name": "Recent Track", "artist": "New Artist", "album": "New Album", "popularity": 70},
        ])
        MockDiscoveryEngine = MagicMock(return_value=mock_discovery)
        fake_mod = _fake_module("spotifyforge.core.discovery", DiscoveryEngine=MockDiscoveryEngine)

        with patch.dict("sys.modules", {"spotifyforge.core.discovery": fake_mod}):
            result = runner.invoke(
                app, ["discover", "top-tracks", "--time-range", "short_term", "--limit", "5"]
            )

        assert result.exit_code == 0
        assert "Recent Track" in result.output
        assert "Last 4 Weeks" in result.output
        mock_discovery.get_top_tracks.assert_called_once()

    def test_discover_top_tracks_empty(self):
        mock_discovery = MagicMock()
        mock_discovery.get_top_tracks = AsyncMock(return_value=[])
        MockDiscoveryEngine = MagicMock(return_value=mock_discovery)
        fake_mod = _fake_module("spotifyforge.core.discovery", DiscoveryEngine=MockDiscoveryEngine)

        with patch.dict("sys.modules", {"spotifyforge.core.discovery": fake_mod}):
            result = runner.invoke(app, ["discover", "top-tracks"])

        assert result.exit_code == 0
        assert "No top tracks found" in result.output

    def test_discover_top_tracks_error(self):
        mock_discovery = MagicMock()
        mock_discovery.get_top_tracks = AsyncMock(side_effect=Exception("API failure"))
        MockDiscoveryEngine = MagicMock(return_value=mock_discovery)
        fake_mod = _fake_module("spotifyforge.core.discovery", DiscoveryEngine=MockDiscoveryEngine)

        with patch.dict("sys.modules", {"spotifyforge.core.discovery": fake_mod}):
            result = runner.invoke(app, ["discover", "top-tracks"])

        assert result.exit_code == 1

    # -- deep-cuts ---------------------------------------------------------

    def test_discover_deep_cuts_success(self):
        mock_discovery = MagicMock()
        mock_discovery.find_deep_cuts = AsyncMock(return_value={
            "artist_name": "Indie Band",
            "tracks": [
                {"name": "Hidden Gem", "album": "Obscure Album", "popularity": 12, "duration_ms": 240000},
                {"name": "B-Side Track", "album": "Rare Vinyl", "popularity": 5, "duration_ms": 195000},
            ],
        })
        MockDiscoveryEngine = MagicMock(return_value=mock_discovery)
        fake_mod = _fake_module("spotifyforge.core.discovery", DiscoveryEngine=MockDiscoveryEngine)

        with patch.dict("sys.modules", {"spotifyforge.core.discovery": fake_mod}):
            result = runner.invoke(app, ["discover", "deep-cuts", "Indie Band"])

        assert result.exit_code == 0
        assert "Hidden Gem" in result.output
        assert "Indie Band" in result.output

    def test_discover_deep_cuts_none_found(self):
        mock_discovery = MagicMock()
        mock_discovery.find_deep_cuts = AsyncMock(return_value={
            "artist_name": "Famous Singer",
            "tracks": [],
        })
        MockDiscoveryEngine = MagicMock(return_value=mock_discovery)
        fake_mod = _fake_module("spotifyforge.core.discovery", DiscoveryEngine=MockDiscoveryEngine)

        with patch.dict("sys.modules", {"spotifyforge.core.discovery": fake_mod}):
            result = runner.invoke(app, ["discover", "deep-cuts", "Famous Singer"])

        assert result.exit_code == 0
        assert "No deep cuts found" in result.output

    def test_discover_deep_cuts_with_threshold(self):
        mock_discovery = MagicMock()
        mock_discovery.find_deep_cuts = AsyncMock(return_value={
            "artist_name": "Some Artist",
            "tracks": [
                {"name": "Ultra Deep", "album": "Rare", "popularity": 2, "duration_ms": 180000},
            ],
        })
        MockDiscoveryEngine = MagicMock(return_value=mock_discovery)
        fake_mod = _fake_module("spotifyforge.core.discovery", DiscoveryEngine=MockDiscoveryEngine)

        with patch.dict("sys.modules", {"spotifyforge.core.discovery": fake_mod}):
            result = runner.invoke(
                app, ["discover", "deep-cuts", "Some Artist", "--threshold", "10"]
            )

        assert result.exit_code == 0
        assert "Ultra Deep" in result.output

    # -- genre -------------------------------------------------------------

    def test_discover_genre_success(self):
        mock_discovery = MagicMock()
        mock_discovery.build_genre_playlist = AsyncMock(return_value={
            "playlist": {"name": "Indie Rock Vibes", "id": "pl_genre_001"},
            "tracks": [
                {"name": "Indie Song 1", "artist": "Band A"},
                {"name": "Indie Song 2", "artist": "Band B"},
            ],
        })
        MockDiscoveryEngine = MagicMock(return_value=mock_discovery)
        fake_mod = _fake_module("spotifyforge.core.discovery", DiscoveryEngine=MockDiscoveryEngine)

        with patch.dict("sys.modules", {"spotifyforge.core.discovery": fake_mod}):
            result = runner.invoke(app, ["discover", "genre", "indie-rock"])

        assert result.exit_code == 0
        assert "Genre playlist created" in result.output
        assert "Indie Rock Vibes" in result.output

    def test_discover_genre_error(self):
        mock_discovery = MagicMock()
        mock_discovery.build_genre_playlist = AsyncMock(side_effect=Exception("bad genre"))
        MockDiscoveryEngine = MagicMock(return_value=mock_discovery)
        fake_mod = _fake_module("spotifyforge.core.discovery", DiscoveryEngine=MockDiscoveryEngine)

        with patch.dict("sys.modules", {"spotifyforge.core.discovery": fake_mod}):
            result = runner.invoke(app, ["discover", "genre", "nonexistent-genre"])

        assert result.exit_code == 1

    # -- time-capsule ------------------------------------------------------

    def test_discover_time_capsule_success(self):
        mock_discovery = MagicMock()
        mock_discovery.create_time_capsule = AsyncMock(return_value={
            "playlist": {"name": "Time Capsule 2024", "id": "pl_tc_001"},
            "track_count": 25,
        })
        MockDiscoveryEngine = MagicMock(return_value=mock_discovery)
        fake_mod = _fake_module("spotifyforge.core.discovery", DiscoveryEngine=MockDiscoveryEngine)

        with patch.dict("sys.modules", {"spotifyforge.core.discovery": fake_mod}):
            result = runner.invoke(app, ["discover", "time-capsule"])

        assert result.exit_code == 0
        assert "Time capsule created" in result.output
        assert "25" in result.output

    def test_discover_time_capsule_with_time_range(self):
        mock_discovery = MagicMock()
        mock_discovery.create_time_capsule = AsyncMock(return_value={
            "playlist": {"name": "Short Term Capsule", "id": "pl_tc_002"},
            "track_count": 15,
        })
        MockDiscoveryEngine = MagicMock(return_value=mock_discovery)
        fake_mod = _fake_module("spotifyforge.core.discovery", DiscoveryEngine=MockDiscoveryEngine)

        with patch.dict("sys.modules", {"spotifyforge.core.discovery": fake_mod}):
            result = runner.invoke(
                app, ["discover", "time-capsule", "--time-range", "short_term"]
            )

        assert result.exit_code == 0
        assert "Last 4 Weeks" in result.output


# =========================================================================
# SCHEDULE commands
# =========================================================================


class TestScheduleCommands:
    """Tests for the ``schedule`` sub-command group."""

    def test_schedule_help(self):
        result = runner.invoke(app, ["schedule", "--help"])
        assert result.exit_code == 0
        assert "list" in result.output
        assert "add" in result.output
        assert "remove" in result.output
        assert "run" in result.output

    # -- schedule list -----------------------------------------------------

    def test_schedule_list_with_jobs(self):
        mock_scheduler = MagicMock()
        mock_scheduler.list_jobs = AsyncMock(return_value=[
            {
                "id": "job_001",
                "name": "Weekly Sync",
                "type": "sync",
                "playlist_id": "pl_001",
                "cron": "0 8 * * 1",
                "next_run": "2026-02-23 08:00",
                "status": "active",
            },
            {
                "id": "job_002",
                "name": "Daily Dedup",
                "type": "deduplicate",
                "playlist_id": "pl_002",
                "cron": "0 0 * * *",
                "next_run": "2026-02-17 00:00",
                "status": "paused",
            },
        ])
        MockScheduler = MagicMock(return_value=mock_scheduler)
        fake_mod = _fake_module("spotifyforge.core.scheduler", Scheduler=MockScheduler)

        with patch.dict("sys.modules", {"spotifyforge.core.scheduler": fake_mod}):
            result = runner.invoke(app, ["schedule", "list"])

        assert result.exit_code == 0
        # Rich tables may wrap long cell values across lines, so check for
        # individual words rather than full phrases.
        assert "Weekly" in result.output
        assert "Sync" in result.output
        assert "Daily" in result.output
        assert "Dedup" in result.output
        assert "job_001" in result.output

    def test_schedule_list_empty(self):
        mock_scheduler = MagicMock()
        mock_scheduler.list_jobs = AsyncMock(return_value=[])
        MockScheduler = MagicMock(return_value=mock_scheduler)
        fake_mod = _fake_module("spotifyforge.core.scheduler", Scheduler=MockScheduler)

        with patch.dict("sys.modules", {"spotifyforge.core.scheduler": fake_mod}):
            result = runner.invoke(app, ["schedule", "list"])

        assert result.exit_code == 0
        assert "No scheduled jobs" in result.output

    def test_schedule_list_error(self):
        mock_scheduler = MagicMock()
        mock_scheduler.list_jobs = AsyncMock(side_effect=Exception("db error"))
        MockScheduler = MagicMock(return_value=mock_scheduler)
        fake_mod = _fake_module("spotifyforge.core.scheduler", Scheduler=MockScheduler)

        with patch.dict("sys.modules", {"spotifyforge.core.scheduler": fake_mod}):
            result = runner.invoke(app, ["schedule", "list"])

        assert result.exit_code == 1

    # -- schedule add ------------------------------------------------------

    def test_schedule_add_success(self):
        mock_scheduler = MagicMock()
        mock_scheduler.add_job = AsyncMock(return_value={
            "id": "job_new_001",
            "next_run": "2026-02-23 08:00",
        })
        MockScheduler = MagicMock(return_value=mock_scheduler)
        fake_mod = _fake_module("spotifyforge.core.scheduler", Scheduler=MockScheduler)

        with patch.dict("sys.modules", {"spotifyforge.core.scheduler": fake_mod}):
            result = runner.invoke(
                app,
                [
                    "schedule", "add",
                    "--name", "My Sync Job",
                    "--type", "sync",
                    "--playlist", "pl_001",
                    "--cron", "0 8 * * 1",
                ],
            )

        assert result.exit_code == 0
        assert "Job scheduled successfully" in result.output
        assert "My Sync Job" in result.output

    def test_schedule_add_failure(self):
        mock_scheduler = MagicMock()
        mock_scheduler.add_job = AsyncMock(side_effect=Exception("invalid cron"))
        MockScheduler = MagicMock(return_value=mock_scheduler)
        fake_mod = _fake_module("spotifyforge.core.scheduler", Scheduler=MockScheduler)

        with patch.dict("sys.modules", {"spotifyforge.core.scheduler": fake_mod}):
            result = runner.invoke(
                app,
                [
                    "schedule", "add",
                    "--name", "Bad Job",
                    "--type", "sync",
                    "--playlist", "pl_001",
                    "--cron", "invalid",
                ],
            )

        assert result.exit_code == 1

    # -- schedule remove ---------------------------------------------------

    def test_schedule_remove_success(self):
        mock_scheduler = MagicMock()
        mock_scheduler.remove_job = AsyncMock(return_value=None)
        MockScheduler = MagicMock(return_value=mock_scheduler)
        fake_mod = _fake_module("spotifyforge.core.scheduler", Scheduler=MockScheduler)

        with patch.dict("sys.modules", {"spotifyforge.core.scheduler": fake_mod}):
            result = runner.invoke(app, ["schedule", "remove", "job_001"])

        assert result.exit_code == 0
        assert "job_001" in result.output
        assert "removed successfully" in result.output

    def test_schedule_remove_failure(self):
        mock_scheduler = MagicMock()
        mock_scheduler.remove_job = AsyncMock(side_effect=Exception("not found"))
        MockScheduler = MagicMock(return_value=mock_scheduler)
        fake_mod = _fake_module("spotifyforge.core.scheduler", Scheduler=MockScheduler)

        with patch.dict("sys.modules", {"spotifyforge.core.scheduler": fake_mod}):
            result = runner.invoke(app, ["schedule", "remove", "job_missing"])

        assert result.exit_code == 1


# =========================================================================
# CONFIG commands
# =========================================================================


class TestConfigCommands:
    """Tests for the ``config`` sub-command group."""

    def test_config_help(self):
        result = runner.invoke(app, ["config", "--help"])
        assert result.exit_code == 0
        assert "show" in result.output
        assert "set" in result.output

    def test_config_show(self):
        result = runner.invoke(app, ["config", "show"])
        assert result.exit_code == 0
        assert "SpotifyForge Configuration" in result.output
        # Should list known config fields
        assert "spotify_client_id" in result.output
        assert "db_path" in result.output
        assert "scheduler_enabled" in result.output
        assert "web_host" in result.output
        assert "web_port" in result.output

    def test_config_show_contains_source_column(self):
        """The config show table should include a Source column."""
        result = runner.invoke(app, ["config", "show"])
        assert result.exit_code == 0
        # The Source column shows "env" or "default"
        assert "default" in result.output or "env" in result.output

    def test_config_set_invalid_key(self):
        """Setting an unknown config key produces an error."""
        result = runner.invoke(app, ["config", "set", "nonexistent_key", "value"])
        assert result.exit_code == 1

    def test_config_set_valid_key(self, tmp_path, monkeypatch):
        """Setting a valid config key writes to the .env file."""
        # Change working directory to tmp_path so the .env file is written there
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["config", "set", "web_port", "9000"])

        assert result.exit_code == 0
        assert "Configuration updated" in result.output
        assert "web_port" in result.output
        assert "9000" in result.output

        # Verify the .env file was created with the correct content
        env_file = tmp_path / ".env"
        assert env_file.exists()
        content = env_file.read_text()
        assert "SPOTIFYFORGE_WEB_PORT=9000" in content

    def test_config_set_updates_existing_env(self, tmp_path, monkeypatch):
        """Setting a key that already exists in .env updates (not duplicates) it."""
        monkeypatch.chdir(tmp_path)
        env_file = tmp_path / ".env"
        env_file.write_text("SPOTIFYFORGE_WEB_PORT=8000\nSPOTIFYFORGE_WEB_HOST=0.0.0.0\n")

        result = runner.invoke(app, ["config", "set", "web_port", "9000"])

        assert result.exit_code == 0
        content = env_file.read_text()
        assert "SPOTIFYFORGE_WEB_PORT=9000" in content
        assert "SPOTIFYFORGE_WEB_HOST=0.0.0.0" in content
        # No duplicate
        assert content.count("SPOTIFYFORGE_WEB_PORT") == 1


# =========================================================================
# Error handling and edge cases
# =========================================================================


class TestErrorHandling:
    """Tests for generic error handling across CLI commands."""

    def test_import_error_auth(self):
        """When the auth module cannot be imported, an error panel is shown."""
        with patch.dict("sys.modules", {"spotifyforge.auth.oauth": None}):
            result = runner.invoke(app, ["auth", "status"])

        assert result.exit_code == 1

    def test_import_error_playlist_manager(self):
        """When the playlist manager cannot be imported, an error panel is shown."""
        with patch.dict("sys.modules", {"spotifyforge.core.playlist_manager": None}):
            result = runner.invoke(app, ["playlist", "list"])

        assert result.exit_code == 1

    def test_import_error_discovery(self):
        """When the discovery module cannot be imported, an error panel is shown."""
        with patch.dict("sys.modules", {"spotifyforge.core.discovery": None}):
            result = runner.invoke(app, ["discover", "top-tracks"])

        assert result.exit_code == 1

    def test_import_error_scheduler(self):
        """When the scheduler module cannot be imported, an error panel is shown."""
        with patch.dict("sys.modules", {"spotifyforge.core.scheduler": None}):
            result = runner.invoke(app, ["schedule", "list"])

        assert result.exit_code == 1

    def test_playlist_show_missing_argument(self):
        """Omitting a required argument produces a usage error."""
        result = runner.invoke(app, ["playlist", "show"])
        assert result.exit_code != 0

    def test_discover_deep_cuts_missing_argument(self):
        """Omitting the artist argument produces a usage error."""
        result = runner.invoke(app, ["discover", "deep-cuts"])
        assert result.exit_code != 0

    def test_schedule_add_missing_required_options(self):
        """Omitting required options for schedule add produces an error."""
        result = runner.invoke(app, ["schedule", "add"])
        assert result.exit_code != 0

    def test_unknown_command(self):
        """Invoking a nonexistent command produces an error."""
        result = runner.invoke(app, ["foobar"])
        assert result.exit_code != 0
