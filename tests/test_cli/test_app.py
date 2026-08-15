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

Everything else — OAuth code exchange, token storage/refresh, DB rows,
playlist sync, discovery, scheduling — is the production code path.
"""

from __future__ import annotations

import json

import pytest
from sqlmodel import Session, select
from typer.testing import CliRunner

from spotifyforge.cli.app import app
from spotifyforge.db.engine import get_engine
from spotifyforge.models.models import JobType, Playlist, ScheduledJob, User

runner = CliRunner()

STATE = "teststate123"
REDIRECT = "http://localhost:8000/api/auth/callback"


# ---------------------------------------------------------------------------
# Environment fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def cli_env(app_env, tmp_path, monkeypatch, memory_token_store):
    """Full CLI environment: root app_env plus in-memory keyring.

    ``settings.db_path`` is redirected into ``tmp_path`` so the CLI's
    ``current_user`` pointer file lands in the test sandbox (isolated_db
    only patches ``database_url``).
    """
    from spotifyforge.auth import oauth
    from spotifyforge.config import settings

    monkeypatch.setattr(settings, "db_path", tmp_path / "app.db")

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

        # The local DB row exists with encrypted tokens (for scheduled jobs).
        with Session(get_engine()) as session:
            user = session.exec(select(User).where(User.spotify_id == "user1")).first()
            assert user is not None
            assert user.display_name == "Test User"
            assert user.is_premium is True
            assert user.access_token_enc
            assert user.refresh_token_enc
            # Tokens are stored encrypted, never plaintext.
            assert token.access_token not in user.access_token_enc

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
            ["playlist", "sync", "pl1"],
            ["playlist", "deduplicate", "pl1"],
            ["playlist", "export", "pl1"],
            ["discover", "top-tracks"],
            ["discover", "deep-cuts", "art1"],
            ["discover", "genre", "indie-rock"],
            ["discover", "time-capsule"],
            ["schedule", "list"],
            ["schedule", "remove", "1"],
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
    def test_creates_on_spotify_and_locally(self, cli_env):
        fake = cli_env
        _login(fake)

        result = runner.invoke(
            app,
            ["playlist", "create", "My Mix", "--description", "test mix", "--private"],
        )
        assert result.exit_code == 0, result.stderr
        assert "Playlist created!" in result.output
        assert "My Mix" in result.output

        # Created on (fake) Spotify...
        created = [p for p in fake.playlists.values() if p["name"] == "My Mix"]
        assert len(created) == 1
        assert created[0]["public"] is False

        # ...and persisted locally, owned by the logged-in user.
        with Session(get_engine()) as session:
            row = session.exec(select(Playlist).where(Playlist.name == "My Mix")).first()
            assert row is not None
            assert row.spotify_id == created[0]["id"]
            user = session.exec(select(User).where(User.spotify_id == "user1")).first()
            assert row.owner_id == user.id


class TestPlaylistSync:
    def test_sync_caches_tracks(self, cli_env):
        fake = cli_env
        fake.add_track("t1", name="Song A")
        fake.add_track("t2", name="Song B")
        fake.add_playlist("pl1", name="Roadtrip", track_ids=["t1", "t2"])
        _login(fake)

        result = runner.invoke(app, ["playlist", "sync", "pl1"])
        assert result.exit_code == 0, result.stderr
        assert "Playlist synced" in result.output
        assert "Tracks synced: 2" in result.output

        with Session(get_engine()) as session:
            row = session.exec(select(Playlist).where(Playlist.spotify_id == "pl1")).first()
            assert row is not None
            assert row.track_count == 2

    def test_sync_missing_playlist_exits_1(self, cli_env):
        _login(cli_env)
        result = runner.invoke(app, ["playlist", "sync", "ghost"])
        assert result.exit_code == 1
        assert "Sync failed" in result.stderr


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
# Schedule
# ---------------------------------------------------------------------------


class TestScheduleAdd:
    def test_add_sync_job_autosyncs_playlist(self, cli_env):
        fake = cli_env
        fake.add_track("t1")
        fake.add_playlist("schedpl", name="Nightly", track_ids=["t1"])
        _login(fake)

        result = runner.invoke(
            app,
            [
                "schedule",
                "add",
                "--name",
                "nightly sync",
                "--type",
                "sync",
                "--cron",
                "0 3 * * *",
                "--playlist",
                "schedpl",
            ],
        )
        assert result.exit_code == 0, result.stderr
        assert "Job scheduled successfully!" in result.output

        with Session(get_engine()) as session:
            job = session.exec(select(ScheduledJob)).first()
            assert job is not None
            assert job.name == "nightly sync"
            assert job.job_type == JobType.sync
            assert job.cron_expression == "0 3 * * *"
            assert job.enabled is True
            # The playlist FK points at the auto-synced local row.
            pl = session.get(Playlist, job.playlist_id)
            assert pl.spotify_id == "schedpl"

    def test_add_time_capsule_needs_no_playlist(self, cli_env):
        _login(cli_env)
        result = runner.invoke(
            app,
            [
                "schedule",
                "add",
                "--name",
                "monthly capsule",
                "--type",
                "time_capsule",
                "--cron",
                "0 8 1 * *",
                "--time-range",
                "short_term",
            ],
        )
        assert result.exit_code == 0, result.stderr
        with Session(get_engine()) as session:
            job = session.exec(select(ScheduledJob)).first()
            assert job.job_type == JobType.time_capsule
            assert job.playlist_id is None
            assert job.config == {"time_range": "short_term"}

    def test_bad_cron_exits_1(self, cli_env):
        result = runner.invoke(
            app,
            ["schedule", "add", "-n", "x", "-t", "sync", "-c", "not a cron", "-p", "pl1"],
        )
        assert result.exit_code == 1
        assert "Invalid cron expression" in result.stderr

    def test_unknown_type_exits_1(self, cli_env):
        result = runner.invoke(
            app,
            ["schedule", "add", "-n", "x", "-t", "explode", "-c", "0 3 * * *"],
        )
        assert result.exit_code == 1
        assert "Unknown job type" in result.stderr

    def test_sync_requires_playlist(self, cli_env):
        result = runner.invoke(app, ["schedule", "add", "-n", "x", "-t", "sync", "-c", "0 3 * * *"])
        assert result.exit_code == 1
        assert "requires --playlist" in result.stderr

    def test_genre_refresh_requires_genre(self, cli_env):
        result = runner.invoke(
            app,
            [
                "schedule",
                "add",
                "-n",
                "x",
                "-t",
                "genre_refresh",
                "-c",
                "0 3 * * *",
                "-p",
                "pl1",
            ],
        )
        assert result.exit_code == 1
        assert "require --genre" in result.stderr


class TestScheduleListRemove:
    def _add_job(self, fake) -> int:
        fake.add_track("t1")
        fake.add_playlist("schedpl", name="Nightly", track_ids=["t1"])
        result = runner.invoke(
            app,
            [
                "schedule",
                "add",
                "-n",
                "nightly sync",
                "-t",
                "sync",
                "-c",
                "0 3 * * *",
                "-p",
                "schedpl",
            ],
        )
        assert result.exit_code == 0, result.stderr
        with Session(get_engine()) as session:
            return session.exec(select(ScheduledJob)).first().id

    def test_list_empty(self, cli_env):
        _login(cli_env)
        result = runner.invoke(app, ["schedule", "list"])
        assert result.exit_code == 0, result.stderr
        assert "No scheduled jobs" in result.output

    def test_list_shows_jobs(self, cli_env):
        fake = cli_env
        _login(fake)
        self._add_job(fake)

        result = runner.invoke(app, ["schedule", "list"])
        assert result.exit_code == 0, result.stderr
        assert "nightly sync" in result.output
        assert "sync" in result.output
        assert "0 3 * * *" in result.output
        assert "never" in result.output  # has not run yet
        assert "Enabled" in result.output

    def test_remove_job(self, cli_env):
        fake = cli_env
        _login(fake)
        job_id = self._add_job(fake)

        result = runner.invoke(app, ["schedule", "remove", str(job_id)])
        assert result.exit_code == 0, result.stderr
        assert "removed successfully" in result.output

        with Session(get_engine()) as session:
            assert session.get(ScheduledJob, job_id) is None

    def test_remove_missing_job_exits_1(self, cli_env):
        _login(cli_env)
        result = runner.invoke(app, ["schedule", "remove", "999"])
        assert result.exit_code == 1
        assert "not found" in result.stderr


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class TestConfigShow:
    def test_shows_settings_with_secrets_masked(self, cli_env, monkeypatch):
        from spotifyforge.config import settings

        monkeypatch.setattr(settings, "secret_key", "supersecretkey123")

        result = runner.invoke(app, ["config", "show"])
        assert result.exit_code == 0, result.stderr
        assert "spotify_client_id" in result.output
        assert "secret_key" in result.output
        # Secrets never appear in cleartext; only ****<last4>.
        assert "supersecretkey123" not in result.output
        assert "****y123" in result.output
        assert "test-client-secret" not in result.output
        assert "****" in result.output
        # Non-secret values are shown as-is.
        assert "development" in result.output


class TestConfigSet:
    def test_writes_new_key_to_env_file(self, cli_env, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["config", "set", "web_port", "9090"])
        assert result.exit_code == 0, result.stderr
        assert "Configuration updated" in result.output
        assert "SPOTIFYFORGE_WEB_PORT=9090" in (tmp_path / ".env").read_text()

    def test_updates_existing_key_in_place(self, cli_env, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text("SPOTIFYFORGE_WEB_PORT=8000\nSPOTIFYFORGE_LOG_LEVEL=DEBUG\n")
        result = runner.invoke(app, ["config", "set", "web_port", "9090"])
        assert result.exit_code == 0, result.stderr
        content = (tmp_path / ".env").read_text()
        assert content.count("SPOTIFYFORGE_WEB_PORT") == 1
        assert "SPOTIFYFORGE_WEB_PORT=9090" in content
        assert "SPOTIFYFORGE_LOG_LEVEL=DEBUG" in content  # untouched

    def test_secret_value_masked_in_output(self, cli_env, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["config", "set", "secret_key", "hunter2secret"])
        assert result.exit_code == 0, result.stderr
        assert "hunter2secret" not in result.output
        assert "****" in result.output
        # But the real value is written to the file.
        assert "SPOTIFYFORGE_SECRET_KEY=hunter2secret" in (tmp_path / ".env").read_text()

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
    def test_login_create_sync_schedule_logout(self, cli_env):
        """One session: login -> create -> add tracks upstream -> sync ->
        schedule -> logout, with state persisting between commands."""
        fake = cli_env
        _login(fake)

        # Create a playlist through the CLI.
        result = runner.invoke(app, ["playlist", "create", "Flow List"])
        assert result.exit_code == 0, result.stderr
        pid = next(p for p, m in fake.playlists.items() if m["name"] == "Flow List")

        # Tracks get added on Spotify (out of band), then we sync.
        fake.add_track("f1", name="Flow One")
        fake.playlist_tracks[pid].append("f1")
        result = runner.invoke(app, ["playlist", "sync", pid])
        assert result.exit_code == 0, result.stderr
        assert "Tracks synced: 1" in result.output

        # Schedule a job against the (already cached) playlist.
        result = runner.invoke(
            app,
            ["schedule", "add", "-n", "flow job", "-t", "sync", "-c", "0 3 * * *", "-p", pid],
        )
        assert result.exit_code == 0, result.stderr

        result = runner.invoke(app, ["schedule", "list"])
        assert "flow job" in result.output
        assert "Flow List" in result.output

        # Logout ends the session.
        result = runner.invoke(app, ["auth", "logout"])
        assert result.exit_code == 0
        result = runner.invoke(app, ["schedule", "list"])
        assert result.exit_code == 1
        assert "Not logged in" in result.stderr
