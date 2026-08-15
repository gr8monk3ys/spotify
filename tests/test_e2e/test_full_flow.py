"""End-to-end proof tests: the whole application, minus only the network.

These tests boot the real FastAPI app (lifespan included, so the real
scheduler starts), complete the real OAuth flow against the fake Spotify
accounts service, drive the API with real HTTP requests, restart the app,
and prove scheduled jobs survive and execute.

If any layer regresses — ciphertext sent as a bearer token, a missing
scheduler function, an interface drift between web and core — these tests
fail. That is their entire purpose.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient
from sqlmodel import Session

from spotifyforge.db.engine import get_engine
from spotifyforge.models.models import JobType, Playlist, ScheduledJob, User


def _fresh_app():
    from spotifyforge.web.app import create_app

    return create_app()


def _login(client: TestClient, fake) -> None:
    """Drive the full OAuth dance: login URL -> callback -> session cookie."""
    fake.add_user("user1")
    resp = client.get("/api/auth/login")
    assert resp.status_code == 200
    auth_url = resp.json()["auth_url"]
    state = parse_qs(urlparse(auth_url).query)["state"][0]
    assert "spotifyforge_oauth_state" in client.cookies

    code = fake.issue_code("user1")
    resp = client.get(f"/api/auth/callback?code={code}&state={state}", follow_redirects=False)
    assert resp.status_code == 302, resp.text
    assert "spotifyforge_session" in client.cookies


class TestFullFlow:
    def test_boot_login_playlist_schedule_restart(self, app_env):
        """The one test that would have caught the entire audit.

        Boot -> OAuth login -> create playlist on Spotify -> sync it ->
        schedule a job -> restart the server -> the job is re-registered.
        """
        fake = app_env

        with TestClient(_fresh_app()) as client:
            assert client.get("/health").json()["status"] == "healthy"

            _login(client, fake)

            me = client.get("/api/auth/me")
            assert me.status_code == 200
            assert me.json()["spotify_id"] == "user1"

            # Create a playlist — created on (fake) Spotify AND stored locally.
            resp = client.post("/api/playlists", json={"name": "E2E List", "public": True})
            assert resp.status_code == 201, resp.text
            playlist = resp.json()
            assert playlist["spotify_id"] in fake.playlists
            assert fake.playlists[playlist["spotify_id"]]["name"] == "E2E List"

            # Add tracks by URI, then sync, then verify local rows exist.
            fake.add_track("e2etrack01")
            fake.add_track("e2etrack02")
            resp = client.post(
                f"/api/playlists/{playlist['id']}/tracks",
                json=["spotify:track:e2etrack01", "spotify:track:e2etrack02"],
            )
            assert resp.status_code == 201, resp.text

            resp = client.post(f"/api/playlists/{playlist['id']}/sync")
            assert resp.status_code == 200, resp.text
            assert resp.json()["tracks_synced"] == 2

            # Schedule a sync job with a valid cron.
            resp = client.post(
                "/api/schedules",
                json={
                    "name": "nightly sync",
                    "job_type": "sync",
                    "playlist_id": playlist["id"],
                    "cron_expression": "0 3 * * *",
                },
            )
            assert resp.status_code == 201, resp.text
            job_id = resp.json()["id"]

        # ---- Restart the server (same database) ----
        from spotifyforge.core import scheduler as scheduler_mod

        scheduler_mod._service = None
        with TestClient(_fresh_app()):
            service = scheduler_mod.get_scheduler_service()
            assert service.is_running
            # The persisted job was re-registered on boot — the exact thing
            # that silently never happened before this rewrite.
            assert service.next_run_time(job_id) is not None

    def test_forged_session_cookie_is_rejected(self, app_env):
        """A cookie that is just a user id (the old format) must not work."""
        fake = app_env
        with TestClient(_fresh_app()) as client:
            _login(client, fake)  # user 1 exists and can log in

            forged = TestClient(_fresh_app())
            forged.cookies.set("spotifyforge_session", "1")
            assert forged.get("/api/auth/me").status_code == 401

            forged.cookies.set("spotifyforge_session", "1.9999999999.forgedsig")
            assert forged.get("/api/auth/me").status_code == 401

            # The real cookie still works.
            assert client.get("/api/auth/me").status_code == 200

    def test_callback_rejects_state_mismatch(self, app_env):
        """A callback whose state doesn't match the login cookie is refused."""
        fake = app_env
        fake.add_user("user1")
        with TestClient(_fresh_app()) as client:
            client.get("/api/auth/login")  # sets the state cookie
            code = fake.issue_code("user1")
            resp = client.get(
                f"/api/auth/callback?code={code}&state=attacker-state",
                follow_redirects=False,
            )
            assert resp.status_code == 400
            assert "spotifyforge_session" not in resp.cookies

    def test_callback_without_login_rejected(self, app_env):
        """A callback with no prior login (no state cookie) is refused."""
        fake = app_env
        fake.add_user("user1")
        with TestClient(_fresh_app()) as client:
            code = fake.issue_code("user1")
            resp = client.get(
                f"/api/auth/callback?code={code}&state=whatever", follow_redirects=False
            )
            assert resp.status_code == 400


class TestScheduledJobExecution:
    async def test_sync_job_executes_against_spotify(self, app_env):
        """A persisted job, fired by the scheduler, does real work."""
        fake = app_env
        fake.add_user("user1")
        fake.add_track("jobtrack1")
        fake.add_track("jobtrack2")
        fake.add_playlist("jobpl", track_ids=["jobtrack1", "jobtrack2"])

        # Seed a user with (encrypted) tokens the way the callback would.
        from spotifyforge.security import encrypt_token

        token = fake.issue_token("user1")
        with Session(get_engine()) as session:
            user = User(
                spotify_id="user1",
                access_token_enc=encrypt_token(token["access_token"]),
                refresh_token_enc=encrypt_token(token["refresh_token"]),
                token_expiry=None,
            )
            session.add(user)
            session.commit()
            session.refresh(user)
            playlist = Playlist(spotify_id="jobpl", owner_id=user.id, name="Job PL")
            session.add(playlist)
            session.commit()
            session.refresh(playlist)
            job = ScheduledJob(
                user_id=user.id,
                name="sync jobpl",
                job_type=JobType.sync,
                playlist_id=playlist.id,
                cron_expression="0 * * * *",
            )
            session.add(job)
            session.commit()
            session.refresh(job)
            job_id, playlist_id = job.id, playlist.id

        from spotifyforge.core.scheduler import SchedulerService

        service = SchedulerService()
        await service._execute_job(job_id)

        with Session(get_engine()) as session:
            job_row = session.get(ScheduledJob, job_id)
            assert job_row.last_run_at is not None
            assert job_row.failure_count == 0
            pl_row = session.get(Playlist, playlist_id)
            assert pl_row.track_count == 2

    async def test_failing_job_records_error(self, app_env):
        """A job whose playlist vanished records the failure, not silence."""
        fake = app_env
        fake.add_user("user1")

        from spotifyforge.security import encrypt_token

        token = fake.issue_token("user1")
        with Session(get_engine()) as session:
            user = User(
                spotify_id="user1",
                access_token_enc=encrypt_token(token["access_token"]),
                refresh_token_enc=encrypt_token(token["refresh_token"]),
            )
            session.add(user)
            session.commit()
            session.refresh(user)
            playlist = Playlist(spotify_id="ghost", owner_id=user.id, name="Ghost")
            session.add(playlist)
            session.commit()
            session.refresh(playlist)
            job = ScheduledJob(
                user_id=user.id,
                name="doomed",
                job_type=JobType.sync,
                playlist_id=playlist.id,
                cron_expression="0 * * * *",
            )
            session.add(job)
            session.commit()
            session.refresh(job)
            job_id = job.id

        from spotifyforge.core.scheduler import SchedulerService

        service = SchedulerService()
        await service._execute_job(job_id)

        with Session(get_engine()) as session:
            job_row = session.get(ScheduledJob, job_id)
            assert job_row.failure_count == 1
            assert job_row.last_error


class TestTokenRefreshFlow:
    async def test_expired_token_refreshes_and_persists(self, app_env):
        """An expired stored token is refreshed via the (fake) accounts API."""
        from datetime import UTC, datetime, timedelta

        fake = app_env
        fake.add_user("user1")

        from sqlalchemy.ext.asyncio import AsyncSession

        from spotifyforge.core.clients import spotify_client_for_user
        from spotifyforge.db.engine import _get_async_engine
        from spotifyforge.security import decrypt_token, encrypt_token

        token = fake.issue_token("user1")
        old_access = token["access_token"]
        with Session(get_engine()) as session:
            user = User(
                spotify_id="user1",
                access_token_enc=encrypt_token(old_access),
                refresh_token_enc=encrypt_token(token["refresh_token"]),
                token_expiry=datetime.now(UTC) - timedelta(hours=1),  # expired
            )
            session.add(user)
            session.commit()
            session.refresh(user)
            user_id = user.id

        async with AsyncSession(_get_async_engine()) as adb:
            db_user = await adb.get(User, user_id)
            client = await spotify_client_for_user(db_user, adb)
            # The refreshed token works against the fake API...
            me = await client.current_user()
            assert me.id == "user1"
            await client.close()
            # ...and the *new* token was persisted, encrypted.
            await adb.refresh(db_user)
            assert decrypt_token(db_user.access_token_enc) != old_access
