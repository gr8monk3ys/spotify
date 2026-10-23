"""Integration tests for the SpotifyForge Typer CLI.

These tests run the REAL CLI (``spotifyforge.cli.app``) against the
in-memory :class:`FakeSpotify` backend from ``tests/fake_spotify.py``.
Every request flows through tekore's real request/response machinery via
``httpx.MockTransport`` — no spotifyforge module is replaced with a mock.

Test doubles are limited to the process boundary:

* the OS keyring is replaced with an in-memory ``MemoryTokenStore``;
* ``webbrowser.open`` is stubbed so login never opens a browser;
* the CSRF state generator returns a fixed value so the test can build
  the pasted redirect URL for ``auth login``.

Everything else — OAuth code exchange, token storage/refresh, playlist
writes, discovery, curation — is the production code path.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from spotifyforge.cli.app import app

runner = CliRunner()

STATE = "teststate123"
REDIRECT = "http://localhost:8000/api/auth/callback"


# ---------------------------------------------------------------------------
# Environment fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def cli_env(app_env, tmp_path, monkeypatch, memory_token_store):
    """Full CLI environment: root app_env plus in-memory keyring.

    ``app_env`` already redirects ``settings.data_dir`` into ``tmp_path``,
    so the CLI's ``current_user`` pointer file lands in the test sandbox.
    """
    from spotifyforge.auth import oauth

    # Keyring -> in-memory store (persists across commands within a test).
    monkeypatch.setattr(oauth, "KeyringTokenStore", lambda: memory_token_store)
    # Fixed CSRF state so the test can construct the pasted redirect URL.
    monkeypatch.setattr(oauth, "generate_csrf_state", lambda: STATE)
    # Never open a real browser.
    monkeypatch.setattr("webbrowser.open", lambda url: True)

    return app_env


def _login(fake, user_id: str = "user1"):
    """Drive the real interactive login against the fake accounts service."""
    if user_id not in fake.users:
        fake.add_user(user_id)
    code = fake.issue_code(user_id)
    result = runner.invoke(
        app,
        ["auth", "login", "--no-browser"],
        input=f"{REDIRECT}?code={code}&state={STATE}\n",
    )
    assert result.exit_code == 0, f"login failed:\n{result.output}\n{result.stderr}"
    return result


# ---------------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------------


class TestRoot:
    def test_version_flag(self):
        import spotifyforge

        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert "SpotifyForge" in result.output
        assert spotifyforge.__version__ in result.output

    def test_no_args_shows_help(self):
        result = runner.invoke(app, [])
        assert "playlist" in result.output
        assert "discover" in result.output


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class TestAuthLogin:
    def test_login_success(self, cli_env, memory_token_store, tmp_path):
        fake = cli_env
        result = _login(fake)

        assert "Successfully authenticated" in result.output
        assert "Test User" in result.output

        # The current-user pointer file was written into the sandbox.
        assert (tmp_path / "current_user").read_text() == "user1"

        # The token landed in the (in-memory) keyring and is valid.
        token = memory_token_store.load_token("user1")
        assert token.access_token in fake.valid_tokens

    def test_login_rejects_state_mismatch(self, cli_env, tmp_path):
        fake = cli_env
        fake.add_user("user1")
        code = fake.issue_code("user1")
        result = runner.invoke(
            app,
            ["auth", "login", "--no-browser"],
            input=f"{REDIRECT}?code={code}&state=attacker-state\n",
        )
        assert result.exit_code == 1
        assert "Login failed" in result.stderr
        assert "state mismatch" in result.stderr
        assert not (tmp_path / "current_user").exists()

    def test_login_rejects_bad_code(self, cli_env, tmp_path):
        fake = cli_env
        fake.add_user("user1")
        result = runner.invoke(
            app,
            ["auth", "login", "--no-browser"],
            input=f"{REDIRECT}?code=bogus-code&state={STATE}\n",
        )
        assert result.exit_code == 1
        assert "Login failed" in result.stderr
        assert not (tmp_path / "current_user").exists()


class TestAuthStatus:
    def test_status_not_logged_in(self, cli_env):
        result = runner.invoke(app, ["auth", "status"])
        assert result.exit_code == 0
        assert "Not logged in" in result.output

    def test_status_logged_in(self, cli_env):
        _login(cli_env)
        result = runner.invoke(app, ["auth", "status"])
        assert result.exit_code == 0
        assert "user1" in result.output
        assert "Active" in result.output

    def test_status_with_missing_token(self, cli_env, memory_token_store):
        _login(cli_env)
        memory_token_store.tokens.clear()  # keyring lost the token
        result = runner.invoke(app, ["auth", "status"])
        assert result.exit_code == 0
        assert "no usable" in result.output


class TestAuthLogout:
    def test_logout_removes_everything(self, cli_env, memory_token_store, tmp_path):
        _login(cli_env)
        result = runner.invoke(app, ["auth", "logout"])
        assert result.exit_code == 0
        assert "Logged out successfully" in result.output
        assert not (tmp_path / "current_user").exists()
        assert memory_token_store.tokens == {}

        # Authenticated commands now refuse to run.
        result = runner.invoke(app, ["playlist", "list"])
        assert result.exit_code == 1
        assert "Not logged in" in result.stderr

    def test_logout_when_not_logged_in(self, cli_env):
        result = runner.invoke(app, ["auth", "logout"])
        assert result.exit_code == 0
        assert "nothing to do" in result.output


# ---------------------------------------------------------------------------
# Commands require login
# ---------------------------------------------------------------------------


class TestRequiresLogin:
    @pytest.mark.parametrize(
        "args",
        [
            ["playlist", "list"],
            ["playlist", "show", "pl1"],
            ["playlist", "create", "New List"],
            ["playlist", "deduplicate", "pl1"],
            ["playlist", "export", "pl1"],
            ["discover", "top-tracks"],
            ["discover", "deep-cuts", "art1"],
            ["discover", "genre", "indie-rock"],
            ["discover", "time-capsule"],
        ],
    )
    def test_command_without_login_exits_1(self, cli_env, args):
        result = runner.invoke(app, args)
        assert result.exit_code == 1
        assert "Not logged in" in result.stderr


# ---------------------------------------------------------------------------
# Playlist
# ---------------------------------------------------------------------------


class TestPlaylistList:
    def test_lists_playlists(self, cli_env):
        fake = cli_env
        fake.add_track("t1", name="Song A")
        fake.add_playlist("pl1", name="Roadtrip", track_ids=["t1"])
        fake.add_playlist("pl2", name="Chill", track_ids=[], public=False)
        _login(fake)

        result = runner.invoke(app, ["playlist", "list"])
        assert result.exit_code == 0, result.stderr
        assert "Roadtrip" in result.output
        assert "Chill" in result.output
        assert "Public" in result.output
        assert "Private" in result.output

    def test_empty(self, cli_env):
        _login(cli_env)
        result = runner.invoke(app, ["playlist", "list"])
        assert result.exit_code == 0, result.stderr
        assert "No playlists found" in result.output


class TestPlaylistShow:
    def test_shows_details_and_tracks(self, cli_env):
        fake = cli_env
        fake.add_track("t1", name="Song A", artist_name="Band X")
        fake.add_track("t2", name="Song B", artist_name="Band X")
        fake.add_playlist("pl1", name="Roadtrip", track_ids=["t1", "t2"])
        _login(fake)

        result = runner.invoke(app, ["playlist", "show", "pl1"])
        assert result.exit_code == 0, result.stderr
        assert "Roadtrip" in result.output
        assert "Song A" in result.output
        assert "Song B" in result.output
        assert "Band X" in result.output

    def test_missing_playlist_exits_1(self, cli_env):
        _login(cli_env)
        result = runner.invoke(app, ["playlist", "show", "nope"])
        assert result.exit_code == 1
        assert "Failed to fetch playlist" in result.stderr


class TestPlaylistCreate:
    def test_creates_on_spotify(self, cli_env):
        fake = cli_env
        _login(fake)

        result = runner.invoke(
            app,
            ["playlist", "create", "My Mix", "--description", "test mix", "--private"],
        )
        assert result.exit_code == 0, result.stderr
        assert "Playlist created!" in result.output
        assert "My Mix" in result.output

        created = [p for p in fake.playlists.values() if p["name"] == "My Mix"]
        assert len(created) == 1
        assert created[0]["public"] is False


class TestPlaylistDeduplicate:
    def test_removes_duplicates(self, cli_env):
        fake = cli_env
        fake.add_track("t1", name="Song A")
        fake.add_track("t2", name="Song B")
        fake.add_playlist("pl1", track_ids=["t1", "t2", "t1"])
        _login(fake)

        result = runner.invoke(app, ["playlist", "deduplicate", "pl1"])
        assert result.exit_code == 0, result.stderr
        assert "Deduplication complete" in result.output
        assert "1" in result.output
        # The fake's playlist really lost the duplicate, order preserved.
        assert fake.playlist_tracks["pl1"] == ["t1", "t2"]

    def test_clean_playlist(self, cli_env):
        fake = cli_env
        fake.add_track("t1")
        fake.add_track("t2")
        fake.add_playlist("pl1", track_ids=["t1", "t2"])
        _login(fake)

        result = runner.invoke(app, ["playlist", "deduplicate", "pl1"])
        assert result.exit_code == 0, result.stderr
        assert "No duplicates found" in result.output
        assert fake.playlist_tracks["pl1"] == ["t1", "t2"]


class TestPlaylistExport:
    def test_export_json_to_file(self, cli_env, tmp_path):
        fake = cli_env
        fake.add_track("t1", name="Song A", artist_name="Band X")
        fake.add_playlist("pl1", track_ids=["t1"])
        _login(fake)

        out = tmp_path / "export.json"
        result = runner.invoke(
            app, ["playlist", "export", "pl1", "--format", "json", "-o", str(out)]
        )
        assert result.exit_code == 0, result.stderr
        data = json.loads(out.read_text())
        assert data[0]["name"] == "Song A"
        assert data[0]["artist"] == "Band X"
        assert data[0]["uri"] == "spotify:track:t1"

    def test_export_csv_to_file(self, cli_env, tmp_path):
        fake = cli_env
        fake.add_track("t1", name="Song A", artist_name="Band X", album_name="LP One")
        fake.add_playlist("pl1", track_ids=["t1"])
        _login(fake)

        out = tmp_path / "export.csv"
        result = runner.invoke(
            app, ["playlist", "export", "pl1", "--format", "csv", "-o", str(out)]
        )
        assert result.exit_code == 0, result.stderr
        lines = out.read_text().strip().splitlines()
        assert lines[0] == "name,artist,album,duration_ms,uri"
        assert "Song A,Band X,LP One,200000,spotify:track:t1" in lines[1]

    def test_export_json_to_stdout(self, cli_env):
        fake = cli_env
        fake.add_track("t1", name="SongA")
        fake.add_playlist("pl1", track_ids=["t1"])
        _login(fake)

        result = runner.invoke(app, ["playlist", "export", "pl1"])
        assert result.exit_code == 0, result.stderr
        assert "SongA" in result.output

    def test_export_empty_playlist_exits_1(self, cli_env):
        fake = cli_env
        fake.add_playlist("pl1", track_ids=[])
        _login(fake)

        result = runner.invoke(app, ["playlist", "export", "pl1"])
        assert result.exit_code == 1
        assert "no tracks to export" in result.stderr


# ---------------------------------------------------------------------------
# Discover
# ---------------------------------------------------------------------------


class TestDiscoverTopTracks:
    def test_shows_top_tracks(self, cli_env):
        fake = cli_env
        fake.add_track("t1", name="Hit One", popularity=90)
        fake.add_track("t2", name="Hit Two", popularity=80)
        _login(fake)
        fake.top_tracks["user1"] = ["t1", "t2"]

        result = runner.invoke(app, ["discover", "top-tracks", "-t", "short_term"])
        assert result.exit_code == 0, result.stderr
        assert "Hit One" in result.output
        assert "Hit Two" in result.output
        assert "Last 4 Weeks" in result.output

    def test_respects_limit(self, cli_env):
        fake = cli_env
        fake.add_track("t1", name="Hit One")
        fake.add_track("t2", name="Hit Two")
        _login(fake)
        fake.top_tracks["user1"] = ["t1", "t2"]

        result = runner.invoke(app, ["discover", "top-tracks", "--limit", "1"])
        assert result.exit_code == 0, result.stderr
        assert "Hit One" in result.output
        assert "Hit Two" not in result.output

    def test_empty(self, cli_env):
        _login(cli_env)
        result = runner.invoke(app, ["discover", "top-tracks"])
        assert result.exit_code == 0, result.stderr
        assert "No top tracks found" in result.output


class TestDiscoverDeepCuts:
    def _seed_catalogue(self, fake):
        # One artist, one album, a hit and two deep cuts.
        fake.add_track("hit", name="The Hit", popularity=85, album_id="alb1")
        fake.add_track("cut1", name="Obscure One", popularity=10, album_id="alb1")
        fake.add_track("cut2", name="Obscure Two", popularity=25, album_id="alb1")
        fake.artist_albums["art1"] = ["alb1"]
        fake.album_tracks["alb1"] = ["hit", "cut1", "cut2"]

    def test_finds_deep_cuts_below_threshold(self, cli_env):
        fake = cli_env
        self._seed_catalogue(fake)
        _login(fake)

        result = runner.invoke(app, ["discover", "deep-cuts", "art1", "--threshold", "30"])
        assert result.exit_code == 0, result.stderr
        assert "Found 2 deep cuts" in result.output
        assert "Obscure One" in result.output
        assert "Obscure Two" in result.output
        assert "The Hit" not in result.output

    def test_no_deep_cuts(self, cli_env):
        fake = cli_env
        self._seed_catalogue(fake)
        _login(fake)

        result = runner.invoke(app, ["discover", "deep-cuts", "art1", "--threshold", "5"])
        assert result.exit_code == 0, result.stderr
        assert "No deep cuts found" in result.output


class TestDiscoverGenre:
    def test_creates_genre_playlist(self, cli_env):
        fake = cli_env
        fake.add_track("g1", name="Indie Song")
        fake.add_track("g2", name="Indie Anthem")
        _login(fake)

        result = runner.invoke(app, ["discover", "genre", "indie-rock", "--name", "Indie Finds"])
        assert result.exit_code == 0, result.stderr
        assert "Genre playlist created!" in result.output
        assert "Indie Finds" in result.output

        created = [p for p, m in fake.playlists.items() if m["name"] == "Indie Finds"]
        assert len(created) == 1
        assert sorted(fake.playlist_tracks[created[0]]) == ["g1", "g2"]


class TestDiscoverTimeCapsule:
    def test_creates_private_capsule_playlist(self, cli_env):
        fake = cli_env
        fake.add_track("t1", name="Memory Lane")
        _login(fake)
        fake.top_tracks["user1"] = ["t1"]

        result = runner.invoke(app, ["discover", "time-capsule", "-t", "long_term"])
        assert result.exit_code == 0, result.stderr
        assert "Time capsule created!" in result.output
        assert "Tracks:     1" in result.output

        created = [m for m in fake.playlists.values() if "Time Capsule" in m["name"]]
        assert len(created) == 1
        assert created[0]["public"] is False
        assert fake.playlist_tracks[created[0]["id"]] == ["t1"]


# ---------------------------------------------------------------------------
# Curate
# ---------------------------------------------------------------------------


class TestCurateStats:
    def test_first_run_snapshots_then_second_run_reports_growth(self, cli_env):
        fake = cli_env
        fake.add_playlist("pl1", name="Quiet Corner", followers=2)
        _login(fake)

        first = runner.invoke(app, ["curate", "stats"])
        assert first.exit_code == 0, first.stderr
        assert "First snapshot" in first.output

        fake.playlists["pl1"]["followers"] = 6

        second = runner.invoke(app, ["curate", "stats"])
        assert second.exit_code == 0, second.stderr
        assert "+4" in second.output
        assert "Quiet Corner" in second.output


class TestCurateGenres:
    def test_reports_what_the_fallback_classified(self, cli_env, monkeypatch):
        """MusicBrainz itself is faked one level down (test_genre_fallback);
        here the lookup is stubbed so the command's own counting is what runs."""
        from spotifyforge.core import genre_fallback

        fake = cli_env
        fake.add_track("t1", artist_id="tagged", artist_name="Tagged", album_id="alb1")
        fake.add_track("t2", artist_id="blank", artist_name="Blank", album_id="alb2")
        fake.add_track("t3", artist_id="lost", artist_name="Lost", album_id="alb3")
        fake.set_artist_genres("tagged", ["dream pop"])
        _login(fake)
        for track_id in ("t1", "t2", "t3"):
            fake.save_track("user1", track_id)

        async def fake_fetch(tracks, cache, client=None, progress=None):
            cache.put("blank", ["dream pop", "seen live"], "mb-1")
            cache.put("lost", [], None)
            cache.save()
            return genre_fallback.FetchReport(artists_asked=2, artists_tagged=1)

        monkeypatch.setattr(genre_fallback, "fetch_artist_tags", fake_fetch)

        result = runner.invoke(app, ["curate", "genres"])

        assert result.exit_code == 0, result.stderr
        assert "Songs without a genre:      2 of 3" in result.output
        assert "Songs this run classified:  1" in result.output
        assert "Still without a genre:      1" in result.output
        assert "dream pop (1)" in result.output

        # The cache it wrote is what planning now reads.
        plan = runner.invoke(app, ["curate", "plan", "--min-size", "2"])
        assert plan.exit_code == 0, plan.stderr
        assert "dream pop" in plan.output


class TestCurateFeatures:
    def test_looks_up_pinned_expansion_isrcs_too(self, cli_env, monkeypatch):
        from spotifyforge.core.curation import CurationTrack
        from spotifyforge.core.expansion import save_expansions

        fake = cli_env
        fake.add_track("t1")
        fake.save_track("user1", "t1")
        _login(fake)
        save_expansions(
            {
                ("coldwave", None): [
                    CurationTrack(
                        id="x",
                        uri="spotify:track:x",
                        name="X",
                        artist_ids=("a",),
                        artist_names=("A",),
                        release_year=None,
                        popularity=1,
                        isrc="ISRCPINNED",
                        genres=("coldwave",),
                    )
                ]
            }
        )

        captured: dict[str, list[str]] = {}

        async def fake_gather(isrcs, deep=False, progress=None):
            captured["isrcs"] = list(isrcs)
            return {}, 0

        monkeypatch.setattr("spotifyforge.core.audio_features.gather_features", fake_gather)

        result = runner.invoke(app, ["curate", "features"])
        assert result.exit_code == 0, result.stderr
        # Pinned tracks sit in the same playlists as liked songs, so the
        # lookup must cover both.
        assert "ISRCPINNED" in captured["isrcs"]
        assert "ISRCT1" in captured["isrcs"]


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


class TestHistory:
    def test_sync_then_show(self, cli_env, tmp_path):
        from datetime import UTC, datetime, timedelta

        fake = cli_env
        fake.add_track("t1", name="Night Drive", artist_name="Alpha")
        _login(fake)
        now = datetime.now(UTC)
        for minutes in (30, 20):
            fake.play("user1", "t1", (now - timedelta(minutes=minutes)).isoformat())

        result = runner.invoke(app, ["history", "sync"])
        assert result.exit_code == 0, result.stderr
        assert "Added 2 play(s)" in result.output
        assert (tmp_path / "listening_history.jsonl").exists()

        # Nothing new: a second run adds nothing and loses nothing.
        result = runner.invoke(app, ["history", "sync"])
        assert "Added 0 play(s); the history now holds 2" in result.output

        result = runner.invoke(app, ["history", "show", "--days", "1"])
        assert result.exit_code == 0, result.stderr
        assert "Night Drive" in result.output
        assert "Alpha" in result.output

    def test_show_prints_markup_in_a_track_name_literally(self, cli_env):
        from datetime import UTC, datetime, timedelta

        fake = cli_env
        fake.add_track(
            "t1", name="[link=https://evil.example]Free[/link]", artist_name="[red]X[/red]"
        )
        _login(fake)
        fake.play("user1", "t1", (datetime.now(UTC) - timedelta(minutes=5)).isoformat())
        runner.invoke(app, ["history", "sync"])

        result = runner.invoke(app, ["history", "show", "--days", "1"])

        assert result.exit_code == 0, result.stderr
        assert "[link=https://evil.example]Free[/link]" in result.output
        assert "[red]X[/red]" in result.output

    def test_playlists_reports_usage_and_warns_while_the_log_is_young(self, cli_env):
        from datetime import UTC, datetime, timedelta

        fake = cli_env
        fake.add_track("t1")
        _login(fake)
        fake.add_playlist("mine1", name="Late Set", track_ids=["t1"])
        fake.add_playlist("mine2", name="Never Opened", track_ids=["t1"])
        now = datetime.now(UTC)
        fake.play("user1", "t1", (now - timedelta(minutes=9)).isoformat(), "spotify:playlist:mine1")
        fake.play("user1", "t1", (now - timedelta(minutes=5)).isoformat(), "spotify:playlist:zzz")
        runner.invoke(app, ["history", "sync"])

        result = runner.invoke(app, ["history", "playlists"])

        assert result.exit_code == 0, result.stderr
        assert "From your own playlists: 1" in result.output
        assert "From other playlists:    1" in result.output
        assert "Your playlists played:   1 of 2" in result.output
        assert "Late Set" in result.output
        assert "The log covers 1 day(s)" in result.output

    def test_show_with_no_history_says_how_to_start(self, cli_env):
        result = runner.invoke(app, ["history", "show"])
        assert result.exit_code == 0
        assert "history sync" in result.output

    def test_sync_requires_login(self, cli_env):
        result = runner.invoke(app, ["history", "sync"])
        assert result.exit_code == 1
        assert "Not logged in" in result.stderr


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class TestConfigShow:
    def test_shows_settings_with_secrets_masked(self, cli_env, monkeypatch):
        from spotifyforge.config import settings

        monkeypatch.setattr(settings, "pexels_api_key", "supersecretkey123")

        result = runner.invoke(app, ["config", "show"])
        assert result.exit_code == 0, result.stderr
        assert "spotify_client_id" in result.output
        assert "pexels_api_key" in result.output
        # Secrets never appear in cleartext; only ****<last4>.
        assert "supersecretkey123" not in result.output
        assert "****y123" in result.output
        assert "test-client-secret" not in result.output
        assert "****" in result.output
        # Non-secret values are shown as-is.
        assert "INFO" in result.output


class TestConfigSet:
    def test_writes_new_key_to_env_file(self, cli_env, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["config", "set", "log_level", "WARNING"])
        assert result.exit_code == 0, result.stderr
        assert "Configuration updated" in result.output
        assert "SPOTIFYFORGE_LOG_LEVEL=WARNING" in (tmp_path / ".env").read_text()

    def test_updates_existing_key_in_place(self, cli_env, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text("SPOTIFYFORGE_LOG_LEVEL=INFO\nSPOTIFYFORGE_MUSIC_DIR=/m\n")
        result = runner.invoke(app, ["config", "set", "log_level", "WARNING"])
        assert result.exit_code == 0, result.stderr
        content = (tmp_path / ".env").read_text()
        assert content.count("SPOTIFYFORGE_LOG_LEVEL") == 1
        assert "SPOTIFYFORGE_LOG_LEVEL=WARNING" in content
        assert "SPOTIFYFORGE_MUSIC_DIR=/m" in content  # untouched

    def test_secret_value_masked_in_output(self, cli_env, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["config", "set", "pexels_api_key", "hunter2secret"])
        assert result.exit_code == 0, result.stderr
        assert "hunter2secret" not in result.output
        assert "****" in result.output
        # But the real value is written to the file.
        assert "SPOTIFYFORGE_PEXELS_API_KEY=hunter2secret" in (tmp_path / ".env").read_text()

    def test_unknown_key_exits_1(self, cli_env, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["config", "set", "no_such_key", "value"])
        assert result.exit_code == 1
        assert "Unknown configuration key" in result.stderr
        assert not (tmp_path / ".env").exists()


# ---------------------------------------------------------------------------
# Cross-command flow
# ---------------------------------------------------------------------------


class TestEndToEndFlow:
    def test_login_create_dedupe_logout(self, cli_env):
        """One session: login -> create -> tracks land upstream ->
        deduplicate -> logout, with state persisting between commands."""
        fake = cli_env
        _login(fake)

        result = runner.invoke(app, ["playlist", "create", "Flow List"])
        assert result.exit_code == 0, result.stderr
        pid = next(p for p, m in fake.playlists.items() if m["name"] == "Flow List")

        # Tracks get added on Spotify (out of band), one of them twice.
        fake.add_track("f1", name="Flow One")
        fake.playlist_tracks[pid].extend(["f1", "f1"])
        result = runner.invoke(app, ["playlist", "deduplicate", pid])
        assert result.exit_code == 0, result.stderr
        assert fake.playlist_tracks[pid] == ["f1"]

        # Logout ends the session.
        result = runner.invoke(app, ["auth", "logout"])
        assert result.exit_code == 0
        result = runner.invoke(app, ["playlist", "list"])
        assert result.exit_code == 1
        assert "Not logged in" in result.stderr


class TestScopeErrors:
    def test_a_scope_the_saved_token_lacks_says_to_re_authorise(self, cli_env, monkeypatch):
        """A saved token carries the scopes it was granted, so adding one
        to REQUIRED_SCOPES leaves every existing token short. Spotify
        answers with the offending URL and nothing actionable, which is
        what `curate follow-artists` showed on the real account."""
        fake = cli_env
        _login(fake)
        # Two liked songs by one artist, so there is a candidate to check
        # and the command actually reaches /v1/me/following.
        for i in range(2):
            fake.add_track(f"t{i}", artist_id="stott", artist_name="Andy Stott")
            fake.save_track("user1", f"t{i}")

        import httpx

        original = fake.handler

        def deny_following(request: httpx.Request):
            if "/v1/me/following" in str(request.url):
                return httpx.Response(
                    403,
                    json={"error": {"status": 403, "message": "Insufficient client scope"}},
                )
            return original(request)

        monkeypatch.setattr(fake, "handler", deny_following)

        result = runner.invoke(app, ["curate", "follow-artists"])

        assert result.exit_code == 1
        assert "auth login" in result.stderr
        assert "Re-authorisation needed" in result.stderr


# ---------------------------------------------------------------------------
# Library
# ---------------------------------------------------------------------------


class TestLibrarySave:
    @staticmethod
    def _discogs_file(tmp_path):
        from tests.test_core.test_library import FIXTURE

        path = tmp_path / "discogs.json"
        path.write_text(json.dumps(FIXTURE), encoding="utf-8")
        return path

    def _seed(self, fake):
        _login(fake)
        fake.add_album("alb_utero", "In Utero", "nirv", "Nirvana")
        fake.saved_albums["user1"] = []
        # Kid A is deliberately absent: one match, one "no match".

    def test_dry_run_lists_matches_and_saves_nothing(self, cli_env, tmp_path):
        fake = cli_env
        self._seed(fake)

        result = runner.invoke(
            app, ["library", "save", "--from-discogs", "--file", str(self._discogs_file(tmp_path))]
        )

        assert result.exit_code == 0, result.stderr
        assert "In Utero" in result.output
        assert "no match" in result.output
        assert "would save" in result.output
        assert fake.saved_albums["user1"] == []
        assert not [p for m, p in fake.requests if m == "PUT" and p == "/v1/me/albums"]

    def test_apply_saves_the_unsaved_matches(self, cli_env, tmp_path):
        fake = cli_env
        self._seed(fake)

        result = runner.invoke(
            app,
            [
                "library",
                "save",
                "--from-discogs",
                "--file",
                str(self._discogs_file(tmp_path)),
                "--apply",
            ],
        )

        assert result.exit_code == 0, result.stderr
        assert fake.saved_albums["user1"] == ["alb_utero"]
        assert "Saved 1" in result.output

    def test_already_saved_albums_are_reported_not_resaved(self, cli_env, tmp_path):
        fake = cli_env
        self._seed(fake)
        fake.saved_albums["user1"] = ["alb_utero"]

        result = runner.invoke(
            app,
            [
                "library",
                "save",
                "--from-discogs",
                "--file",
                str(self._discogs_file(tmp_path)),
                "--apply",
            ],
        )

        assert result.exit_code == 0, result.stderr
        assert "already saved" in result.output
        assert not [p for m, p in fake.requests if m == "PUT" and p == "/v1/me/albums"]

    def test_missing_file_exits_2_and_names_discogs_export(self, cli_env, tmp_path):
        fake = cli_env
        _login(fake)

        result = runner.invoke(
            app, ["library", "save", "--from-discogs", "--file", str(tmp_path / "none.json")]
        )

        assert result.exit_code == 2
        assert "discogs export" in result.stderr

    def test_default_file_is_discogs_json_in_music_dir(self, cli_env, tmp_path, monkeypatch):
        from spotifyforge.config import settings

        fake = cli_env
        self._seed(fake)
        monkeypatch.setattr(settings, "music_dir", tmp_path)
        self._discogs_file(tmp_path)

        result = runner.invoke(app, ["library", "save", "--from-discogs"])

        assert result.exit_code == 0, result.stderr
        assert "In Utero" in result.output


class TestLibraryRecords:
    def test_joins_the_shelves_reads_only_and_escapes_markup(self, cli_env, tmp_path):
        from tests.test_core.test_library import FIXTURE

        fake = cli_env
        _login(fake)
        for i in range(3):
            fake.add_track(f"u{i}", artist_name="Nirvana", album_name="In Utero (Remastered)")
            fake.save_track("user1", f"u{i}")
        for i in range(3):
            fake.add_track(
                f"x{i}", artist_id="a2", artist_name="[bold]Loud[/bold]", album_name="[red]LP"
            )
            fake.save_track("user1", f"x{i}")
        doc = {**FIXTURE, "wantlist": [{**FIXTURE["collection"][0], "release_id": 7}]}
        path = tmp_path / "discogs.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        before = len(fake.requests)

        result = runner.invoke(
            app, ["library", "records", "--file", str(path)], env={"COLUMNS": "200"}
        )

        assert result.exit_code == 0, result.stderr
        assert "Wanted and already loved (1)" in result.output
        assert "Owned but not streamed (1)" in result.output
        assert "Kid A" in result.output
        assert "[bold]Loud[/bold]" in result.output  # shown as text, not applied
        assert "collection 1 of 2" in result.output
        assert "wantlist 1 of 1" in result.output
        assert {method for method, _ in fake.requests[before:]} == {"GET"}

    def test_missing_file_exits_2(self, cli_env, tmp_path):
        result = runner.invoke(app, ["library", "records", "--file", str(tmp_path / "no.json")])

        assert result.exit_code == 2
        assert "discogs export" in result.stderr


class TestLibrarySoundtracks:
    @pytest.fixture()
    def films(self, cli_env, tmp_path):
        """Two favourites, one with a soundtrack on Spotify and one without."""
        fake = cli_env
        fake.add_album(
            "ost", "Aftersun (Original Motion Picture Soundtrack)", release_date="2023-01-01"
        )
        for track_id in ("o1", "o2", "o3"):
            fake.add_track(track_id, album_id="ost")
        fake.album_tracks["ost"] = ["o1", "o2", "o3"]
        path = tmp_path / "letterboxd.json"
        path.write_text(
            json.dumps(
                {
                    "schema": "letterboxd/1",
                    "generated_at": "2026-08-30T00:00:00+00:00",
                    "username": "someone",
                    "films": [
                        {"title": "Aftersun", "year": 2022, "rating": 4.5},
                        {"title": "[red]Obscure[/red]", "year": 1970, "rating": 5.0},
                        {"title": "Merely Fine", "year": 2020, "rating": 3.0},
                    ],
                }
            ),
            encoding="utf-8",
        )
        _login(fake)
        return path

    def test_dry_run_lists_matches_and_writes_nothing(self, cli_env, films):
        result = runner.invoke(app, ["library", "soundtracks", "--file", str(films)])

        assert result.exit_code == 0, result.stderr
        # The table wraps at the test terminal's 80 columns, so assert on pieces.
        assert "Aftersun (2022)" in result.output
        assert "Artist One" in result.output
        assert "[red]Obscure[/red]" in result.output  # printed, not interpreted
        assert "Merely Fine" not in result.output
        assert "1 of 2 films" in result.output
        assert cli_env.playlists == {}

    def test_apply_writes_one_private_playlist_and_updates_it_in_place(self, cli_env, films):
        fake = cli_env
        args = ["library", "soundtracks", "--file", str(films), "--apply"]

        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.stderr
        assert "Created" in result.output
        ((pid, meta),) = fake.playlists.items()
        assert meta["name"] == "Soundtracks: films I rated 4.5+"
        assert meta["public"] is False
        assert fake.playlist_tracks[pid] == ["o1", "o2"]

        result = runner.invoke(app, [*args, "--per-film", "3"])
        assert "Updated" in result.output
        assert len(fake.playlists) == 1
        assert fake.playlist_tracks[pid] == ["o1", "o2", "o3"]

    def test_missing_file_exits_2(self, cli_env, tmp_path):
        _login(cli_env)
        result = runner.invoke(
            app, ["library", "soundtracks", "--file", str(tmp_path / "missing.json")]
        )
        assert result.exit_code == 2
        assert "letterboxd export" in result.stderr


class TestExportPage:
    def test_writes_one_html_file_and_publishes_nothing(self, cli_env, tmp_path):
        fake = cli_env
        for i in range(4):
            fake.add_track(f"t{i}", artist_id="a1", artist_name="Quiet One", album_id="alb1")
            fake.save_track("user1", f"t{i}")
        fake.set_artist_genres("a1", ["dream pop"])
        _login(fake)
        out = tmp_path / "site" / "taste.html"
        before = len(fake.requests)

        result = runner.invoke(app, ["export", "page", "--out", str(out), "--name", "Someone"])

        assert result.exit_code == 0, result.stderr
        assert "Nothing was published" in result.output
        html = out.read_text(encoding="utf-8")
        assert "Someone’s library, by genre and decade" in html
        assert "dream pop" in html
        assert "Quiet One" in html
        # Reads only: the library walk and the profile, never a write.
        assert {method for method, _ in fake.requests[before:]} == {"GET"}

    def test_defaults_to_the_spotify_display_name(self, cli_env, tmp_path):
        fake = cli_env
        fake.add_track("t1")
        fake.save_track("user1", "t1")
        _login(fake)
        out = tmp_path / "taste.html"

        result = runner.invoke(app, ["export", "page", "--out", str(out)])

        assert result.exit_code == 0, result.stderr
        assert "Test User’s library" in out.read_text(encoding="utf-8")
