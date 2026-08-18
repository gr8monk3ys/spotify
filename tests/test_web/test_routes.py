"""Route-level tests for the SpotifyForge web API, against the fake Spotify.

Unlike the e2e suite (which proves one long happy path plus restart
semantics), these tests cover the breadth of the API surface: every
route, auth guards, request validation, error paths, and ownership
isolation between users.

No mocks and no dependency overrides: the real app boots with its
lifespan (DB init + scheduler), authentication happens through the real
OAuth dance against the fake accounts service, and every Spotify-backed
route talks to the in-memory fake through tekore's real client stack.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from spotifyforge.db.engine import get_engine
from spotifyforge.models.models import PlaylistTrack, Track
from tests.fake_spotify import _full_artist

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def login(client: TestClient, fake, user_id: str = "user1") -> None:
    """Drive the full OAuth dance: login URL -> callback -> session cookie."""
    fake.add_user(user_id)
    resp = client.get("/api/auth/login")
    assert resp.status_code == 200
    state = parse_qs(urlparse(resp.json()["auth_url"]).query)["state"][0]
    code = fake.issue_code(user_id)
    resp = client.get(f"/api/auth/callback?code={code}&state={state}", follow_redirects=False)
    assert resp.status_code == 302, resp.text
    assert "spotifyforge_session" in client.cookies


def create_playlist(client: TestClient, name: str = "My List", **fields) -> dict:
    resp = client.post("/api/playlists", json={"name": name, **fields})
    assert resp.status_code == 201, resp.text
    return resp.json()


def uri(track_id: str) -> str:
    return f"spotify:track:{track_id}"


# ---------------------------------------------------------------------------
# Ops endpoints
# ---------------------------------------------------------------------------


class TestOps:
    def test_health(self, env):
        resp = env.client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "healthy"
        assert "version" in body

    def test_dashboard(self, env):
        resp = env.client.get("/dashboard")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "SpotifyForge" in resp.text


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class TestAuth:
    def test_login_returns_auth_url_and_sets_state_cookie(self, env):
        resp = env.client.get("/api/auth/login")
        assert resp.status_code == 200
        auth_url = resp.json()["auth_url"]
        assert auth_url.startswith("https://accounts.spotify.com/authorize")
        assert "client_id=test-client-id" in auth_url
        state = parse_qs(urlparse(auth_url).query)["state"][0]
        # The state embedded in the URL is the same one stored in the cookie.
        assert env.client.cookies["spotifyforge_oauth_state"] == state

    def test_me_without_cookie_is_401(self, env):
        resp = env.client.get("/api/auth/me")
        assert resp.status_code == 401

    def test_me_after_login(self, env):
        login(env.client, env.fake)
        resp = env.client.get("/api/auth/me")
        assert resp.status_code == 200
        body = resp.json()
        assert body["spotify_id"] == "user1"
        assert body["display_name"] == "Test User"
        assert body["email"] == "user1@example.com"
        assert body["is_premium"] is True

    def test_logout_clears_session_cookie(self, env):
        login(env.client, env.fake)
        assert env.client.get("/api/auth/me").status_code == 200

        resp = env.client.post("/api/auth/logout")
        assert resp.status_code == 200
        # The response instructs the browser to drop the session cookie...
        assert "spotifyforge_session" in resp.headers.get("set-cookie", "")
        # ...and a client honouring it is logged out.
        assert "spotifyforge_session" not in env.client.cookies
        assert env.client.get("/api/auth/me").status_code == 401

    def test_all_protected_routes_require_auth(self, env):
        cases = [
            ("GET", "/api/auth/me", None),
            ("GET", "/api/playlists", None),
            ("POST", "/api/playlists", {"name": "x"}),
            ("GET", "/api/playlists/1", None),
            ("PUT", "/api/playlists/1", {"name": "x"}),
            ("POST", "/api/playlists/1/sync", None),
            ("POST", "/api/playlists/1/deduplicate", None),
            ("POST", "/api/playlists/1/tracks", [uri("abcdefghij")]),
            ("DELETE", "/api/playlists/1/tracks", [uri("abcdefghij")]),
            ("GET", "/api/discover/top-tracks", None),
            ("GET", "/api/discover/top-artists", None),
            ("GET", "/api/discover/deep-cuts/art1", None),
            ("POST", "/api/discover/genre-playlist?genre=rock", None),
            ("POST", "/api/discover/time-capsule", None),
            ("GET", "/api/schedules", None),
            (
                "POST",
                "/api/schedules",
                {"name": "x", "job_type": "time_capsule", "cron_expression": "0 0 * * *"},
            ),
            ("DELETE", "/api/schedules/1", None),
            ("PUT", "/api/schedules/1/toggle", None),
        ]
        for method, path, body in cases:
            resp = env.client.request(method, path, json=body)
            assert resp.status_code == 401, f"{method} {path} -> {resp.status_code}"


# ---------------------------------------------------------------------------
# Playlists
# ---------------------------------------------------------------------------


class TestPlaylists:
    def test_list_empty_then_populated(self, env):
        login(env.client, env.fake)
        assert env.client.get("/api/playlists").json() == []

        created = create_playlist(env.client, "First List")
        listed = env.client.get("/api/playlists").json()
        assert [p["id"] for p in listed] == [created["id"]]
        assert listed[0]["name"] == "First List"

    def test_list_pagination(self, env):
        login(env.client, env.fake)
        ids = {create_playlist(env.client, f"P{i}")["id"] for i in range(3)}

        page1 = env.client.get("/api/playlists?limit=2&offset=0").json()
        page2 = env.client.get("/api/playlists?limit=2&offset=2").json()
        assert len(page1) == 2
        assert len(page2) == 1
        assert {p["id"] for p in page1} | {p["id"] for p in page2} == ids
        assert {p["id"] for p in page1} & {p["id"] for p in page2} == set()

        assert env.client.get("/api/playlists?limit=0").status_code == 422
        assert env.client.get("/api/playlists?offset=-1").status_code == 422

    def test_create_playlist(self, env):
        login(env.client, env.fake)
        body = create_playlist(env.client, "Fresh Cuts", description="new stuff", public=True)

        # Local row is real and readable.
        assert body["owner_id"] == env.client.get("/api/auth/me").json()["id"]
        got = env.client.get(f"/api/playlists/{body['id']}")
        assert got.status_code == 200
        assert got.json()["name"] == "Fresh Cuts"

        # And the playlist exists on (fake) Spotify with matching state.
        sid = body["spotify_id"]
        assert sid in env.fake.playlists
        assert env.fake.playlists[sid]["name"] == "Fresh Cuts"
        assert env.fake.playlists[sid]["description"] == "new stuff"
        assert env.fake.playlists[sid]["public"] is True

    def test_create_collaborative_playlist_is_private_and_collaborative(self, env):
        login(env.client, env.fake)
        body = create_playlist(env.client, "Group Mix", public=True, collaborative=True)

        sid = body["spotify_id"]
        # Spotify requires collaborative playlists to be non-public: the
        # manager creates them private and then flips the collaborative flag.
        assert env.fake.playlists[sid]["public"] is False
        assert env.fake.playlists[sid]["collaborative"] is True
        assert body["collaborative"] is True
        assert body["public"] is False

    def test_get_missing_playlist_404(self, env):
        login(env.client, env.fake)
        resp = env.client.get("/api/playlists/9999")
        assert resp.status_code == 404

    def test_update_playlist_changes_spotify_and_local_row(self, env):
        login(env.client, env.fake)
        body = create_playlist(env.client, "Old Name")
        sid = body["spotify_id"]

        resp = env.client.put(
            f"/api/playlists/{body['id']}",
            json={"name": "New Name", "description": "renamed", "public": False},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["name"] == "New Name"
        assert resp.json()["public"] is False

        # Fake Spotify state changed...
        assert env.fake.playlists[sid]["name"] == "New Name"
        assert env.fake.playlists[sid]["description"] == "renamed"
        assert env.fake.playlists[sid]["public"] is False
        # ...and so did the local row.
        got = env.client.get(f"/api/playlists/{body['id']}").json()
        assert got["name"] == "New Name"
        assert got["public"] is False

        # An empty update is a no-op, not an error.
        resp = env.client.put(f"/api/playlists/{body['id']}", json={})
        assert resp.status_code == 200
        assert resp.json()["name"] == "New Name"

    def test_update_spotify_failure_is_502_and_local_row_unchanged(self, env):
        login(env.client, env.fake)
        body = create_playlist(env.client, "Fragile")

        # Simulate Spotify no longer knowing the playlist: tekore raises
        # tk.NotFound (an HTTPError), which the route maps to 502.
        del env.fake.playlists[body["spotify_id"]]

        resp = env.client.put(f"/api/playlists/{body['id']}", json={"name": "Nope"})
        assert resp.status_code == 502, resp.text

        # The local row was NOT updated: Spotify and local state never diverge.
        got = env.client.get(f"/api/playlists/{body['id']}").json()
        assert got["name"] == "Fragile"

    def test_sync_with_duplicate_tracks(self, env):
        login(env.client, env.fake)
        env.fake.add_track("duptrack01", name="Dup Song")
        env.fake.add_track("solotrck01", name="Solo Song")
        body = create_playlist(env.client, "Dup List")

        resp = env.client.post(
            f"/api/playlists/{body['id']}/tracks",
            json=[uri("duptrack01"), uri("solotrck01"), uri("duptrack01")],
        )
        assert resp.status_code == 201, resp.text

        resp = env.client.post(f"/api/playlists/{body['id']}/sync")
        assert resp.status_code == 200, resp.text
        assert resp.json()["tracks_synced"] == 3

        # The duplicate synced as two association rows (no IntegrityError):
        # same track, different positions, mirroring Spotify exactly.
        with Session(get_engine()) as session:
            rows = session.exec(
                select(PlaylistTrack)
                .where(PlaylistTrack.playlist_id == body["id"])
                .order_by(PlaylistTrack.position)  # type: ignore[arg-type]
            ).all()
            assert [r.position for r in rows] == [0, 1, 2]
            assert rows[0].track_id == rows[2].track_id
            assert rows[0].track_id != rows[1].track_id
            tracks = session.exec(
                select(Track).where(Track.spotify_id.in_(["duptrack01", "solotrck01"]))  # type: ignore[attr-defined]
            ).all()
            assert len(tracks) == 2

    def test_sync_missing_on_spotify_is_502(self, env):
        login(env.client, env.fake)
        body = create_playlist(env.client, "Ghost")
        del env.fake.playlists[body["spotify_id"]]
        resp = env.client.post(f"/api/playlists/{body['id']}/sync")
        assert resp.status_code == 502

    def test_deduplicate_removes_dups_and_keeps_order(self, env):
        login(env.client, env.fake)
        for tid in ("dedtrack01", "dedtrack02", "dedtrack03"):
            env.fake.add_track(tid)
        body = create_playlist(env.client, "Dedup List")
        sid = body["spotify_id"]

        resp = env.client.post(
            f"/api/playlists/{body['id']}/tracks",
            json=[uri("dedtrack01"), uri("dedtrack02"), uri("dedtrack01"), uri("dedtrack03")],
        )
        assert resp.status_code == 201
        assert env.fake.playlist_tracks[sid] == [
            "dedtrack01",
            "dedtrack02",
            "dedtrack01",
            "dedtrack03",
        ]

        resp = env.client.post(f"/api/playlists/{body['id']}/deduplicate")
        assert resp.status_code == 200, resp.text
        assert resp.json()["duplicates_removed"] == 1
        # One copy of each survives, in the original first-seen order.
        assert env.fake.playlist_tracks[sid] == ["dedtrack01", "dedtrack02", "dedtrack03"]

        # Idempotent: a second pass removes nothing.
        resp = env.client.post(f"/api/playlists/{body['id']}/deduplicate")
        assert resp.json()["duplicates_removed"] == 0

    def test_add_and_remove_tracks(self, env):
        login(env.client, env.fake)
        env.fake.add_track("addtrack01")
        env.fake.add_track("addtrack02")
        body = create_playlist(env.client, "Mutable")
        sid = body["spotify_id"]

        resp = env.client.post(
            f"/api/playlists/{body['id']}/tracks",
            json=[uri("addtrack01"), uri("addtrack02")],
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["tracks_added"] == 2
        assert resp.json()["snapshot_id"]
        assert env.fake.playlist_tracks[sid] == ["addtrack01", "addtrack02"]

        resp = env.client.request(
            "DELETE",
            f"/api/playlists/{body['id']}/tracks",
            json=[uri("addtrack01")],
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["tracks_removed"] == 1
        assert env.fake.playlist_tracks[sid] == ["addtrack02"]

    def test_track_uri_validation(self, env):
        login(env.client, env.fake)
        body = create_playlist(env.client, "Validated")
        url = f"/api/playlists/{body['id']}/tracks"

        # Empty list.
        assert env.client.post(url, json=[]).status_code == 422
        assert env.client.request("DELETE", url, json=[]).status_code == 422
        # Malformed URIs.
        assert env.client.post(url, json=["not-a-uri"]).status_code == 422
        assert env.client.post(url, json=["spotify:track:short"]).status_code == 422
        assert env.client.post(url, json=["spotify:album:abcdefghij"]).status_code == 422
        # Too many URIs.
        too_many = [uri(f"track{i:07d}") for i in range(1001)]
        assert env.client.post(url, json=too_many).status_code == 422
        # Nothing leaked into the fake despite all the attempts.
        assert env.fake.playlist_tracks[body["spotify_id"]] == []

    def test_track_ops_on_missing_playlist_404(self, env):
        login(env.client, env.fake)
        resp = env.client.post("/api/playlists/9999/tracks", json=[uri("abcdefghij")])
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


class TestDiscovery:
    def test_top_tracks(self, env):
        login(env.client, env.fake)
        env.fake.add_track("toptrack01", name="Hit One", popularity=90)
        env.fake.add_track("toptrack02", name="Hit Two", popularity=80)
        env.fake.top_tracks["user1"] = ["toptrack01", "toptrack02"]

        resp = env.client.get("/api/discover/top-tracks?time_range=short_term")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert [t["spotify_id"] for t in body] == ["toptrack01", "toptrack02"]
        assert body[0]["name"] == "Hit One"
        assert body[0]["artist_names"] == ["Artist One"]

        # limit is honoured.
        resp = env.client.get("/api/discover/top-tracks?limit=1")
        assert len(resp.json()) == 1

        # Bad params are rejected by validation.
        assert env.client.get("/api/discover/top-tracks?time_range=forever").status_code == 422
        assert env.client.get("/api/discover/top-tracks?limit=0").status_code == 422

    def test_top_artists(self, env):
        login(env.client, env.fake)
        env.fake.top_artists["user1"] = [
            _full_artist("artaaa0001", "Alpha Artist", popularity=70, genres=["shoegaze"]),
            _full_artist("artbbb0002", "Beta Artist", popularity=55, genres=["dream-pop"]),
        ]

        resp = env.client.get("/api/discover/top-artists")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert [a["id"] for a in body] == ["artaaa0001", "artbbb0002"]
        assert body[0]["name"] == "Alpha Artist"
        assert body[0]["genres"] == ["shoegaze"]
        assert body[0]["popularity"] == 70
        assert body[0]["followers"] == 1000

    def test_deep_cuts(self, env):
        login(env.client, env.fake)
        env.fake.add_track(
            "deeptrck01",
            name="Obscure Gem",
            popularity=10,
            artist_id="artdeep",
            artist_name="Deep Artist",
            album_id="albdeep",
            album_name="Deep Album",
        )
        env.fake.add_track(
            "poptrack01",
            name="Radio Hit",
            popularity=90,
            artist_id="artdeep",
            artist_name="Deep Artist",
            album_id="albdeep",
            album_name="Deep Album",
        )
        env.fake.artist_albums["artdeep"] = ["albdeep"]
        env.fake.album_tracks["albdeep"] = ["deeptrck01", "poptrack01"]

        resp = env.client.get("/api/discover/deep-cuts/artdeep?threshold=30")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert [t["spotify_id"] for t in body] == ["deeptrck01"]
        assert body[0]["popularity"] == 10

        # A threshold of 100 lets everything through, sorted by popularity.
        resp = env.client.get("/api/discover/deep-cuts/artdeep?threshold=100")
        assert [t["spotify_id"] for t in resp.json()] == ["deeptrck01", "poptrack01"]

    def test_genre_playlist_is_owned_by_current_user(self, env):
        login(env.client, env.fake)
        env.fake.add_track("genretrk01", name="Indie Anthem")
        env.fake.add_track("genretrk02", name="Indie Ballad")

        resp = env.client.post("/api/discover/genre-playlist?genre=indie-rock&limit=10")
        assert resp.status_code == 201, resp.text
        body = resp.json()

        me_id = env.client.get("/api/auth/me").json()["id"]
        assert body["owner_id"] == me_id
        assert body["name"] == "SpotifyForge: Indie-Rock"

        sid = body["spotify_id"]
        assert sid in env.fake.playlists
        assert set(env.fake.playlist_tracks[sid]) == {"genretrk01", "genretrk02"}

        # The regression this locks in: the playlist is owned by the real
        # user (not owner_id=0), so it shows up in the user's own listing.
        listed = env.client.get("/api/playlists").json()
        assert body["id"] in [p["id"] for p in listed]

        # genre is required.
        assert env.client.post("/api/discover/genre-playlist").status_code == 422

    def test_time_capsule(self, env):
        login(env.client, env.fake)
        env.fake.add_track("capstrck01", name="Memory One")
        env.fake.add_track("capstrck02", name="Memory Two")
        env.fake.top_tracks["user1"] = ["capstrck01", "capstrck02"]

        resp = env.client.post("/api/discover/time-capsule?time_range=short_term")
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert "short_term" in body["name"]
        assert body["public"] is False

        sid = body["spotify_id"]
        assert env.fake.playlists[sid]["public"] is False
        assert env.fake.playlist_tracks[sid] == ["capstrck01", "capstrck02"]

        # Shows up in the user's playlist listing too.
        listed = env.client.get("/api/playlists").json()
        assert body["id"] in [p["id"] for p in listed]

        assert env.client.post("/api/discover/time-capsule?time_range=forever").status_code == 422


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------


class TestSchedules:
    def _schedule_body(self, playlist_id: int, **overrides) -> dict:
        body = {
            "name": "nightly sync",
            "job_type": "sync",
            "playlist_id": playlist_id,
            "cron_expression": "0 3 * * *",
        }
        body.update(overrides)
        return body

    def test_create_valid_schedule_registers_with_scheduler(self, env):
        from spotifyforge.core.scheduler import get_scheduler_service

        login(env.client, env.fake)
        playlist = create_playlist(env.client, "Synced Nightly")

        resp = env.client.post("/api/schedules", json=self._schedule_body(playlist["id"]))
        assert resp.status_code == 201, resp.text
        job = resp.json()
        assert job["job_type"] == "sync"
        assert job["enabled"] is True
        # Registered with the live scheduler, not just stored.
        assert get_scheduler_service().next_run_time(job["id"]) is not None

    def test_invalid_cron_is_422(self, env):
        login(env.client, env.fake)
        playlist = create_playlist(env.client, "P")
        for bad_cron in ("not a cron", "0 3 * *", "99 99 * * *"):
            resp = env.client.post(
                "/api/schedules",
                json=self._schedule_body(playlist["id"], cron_expression=bad_cron),
            )
            assert resp.status_code == 422, f"{bad_cron!r} -> {resp.status_code}"

    def test_playlist_job_types_require_playlist_id(self, env):
        login(env.client, env.fake)
        for job_type in ("sync", "archive", "deduplicate", "genre_refresh"):
            resp = env.client.post(
                "/api/schedules",
                json={
                    "name": f"{job_type} job",
                    "job_type": job_type,
                    "cron_expression": "0 3 * * *",
                },
            )
            assert resp.status_code == 422, f"{job_type} -> {resp.status_code}"

    def test_genre_refresh_requires_genre_config(self, env):
        login(env.client, env.fake)
        playlist = create_playlist(env.client, "Genre Target")
        resp = env.client.post(
            "/api/schedules",
            json=self._schedule_body(playlist["id"], job_type="genre_refresh", config={}),
        )
        assert resp.status_code == 422
        # With the genre supplied it goes through.
        resp = env.client.post(
            "/api/schedules",
            json=self._schedule_body(
                playlist["id"], job_type="genre_refresh", config={"genre": "indie-rock"}
            ),
        )
        assert resp.status_code == 201, resp.text

    def test_archive_requires_source_playlist_config(self, env):
        login(env.client, env.fake)
        playlist = create_playlist(env.client, "Archive Target")
        resp = env.client.post(
            "/api/schedules",
            json=self._schedule_body(playlist["id"], job_type="archive", config={}),
        )
        assert resp.status_code == 422
        resp = env.client.post(
            "/api/schedules",
            json=self._schedule_body(
                playlist["id"], job_type="archive", config={"source_playlist_id": "discweekly"}
            ),
        )
        assert resp.status_code == 201, resp.text

    def test_schedule_for_unowned_playlist_is_404(self, env):
        login(env.client, env.fake)
        resp = env.client.post("/api/schedules", json=self._schedule_body(9999))
        assert resp.status_code == 404

    def test_list_schedules_pagination(self, env):
        login(env.client, env.fake)
        ids = set()
        for i in range(3):
            resp = env.client.post(
                "/api/schedules",
                json={
                    "name": f"capsule {i}",
                    "job_type": "time_capsule",
                    "cron_expression": "0 0 1 * *",
                },
            )
            assert resp.status_code == 201, resp.text
            ids.add(resp.json()["id"])

        page1 = env.client.get("/api/schedules?limit=2&offset=0").json()
        page2 = env.client.get("/api/schedules?limit=2&offset=2").json()
        assert len(page1) == 2
        assert len(page2) == 1
        assert {j["id"] for j in page1} | {j["id"] for j in page2} == ids
        assert {j["id"] for j in page1} & {j["id"] for j in page2} == set()

        assert env.client.get("/api/schedules?limit=0").status_code == 422

    def test_delete_schedule_removes_row_and_unregisters(self, env):
        from spotifyforge.core.scheduler import get_scheduler_service

        login(env.client, env.fake)
        playlist = create_playlist(env.client, "Doomed Sync")
        job = env.client.post("/api/schedules", json=self._schedule_body(playlist["id"])).json()
        assert get_scheduler_service().next_run_time(job["id"]) is not None

        resp = env.client.delete(f"/api/schedules/{job['id']}")
        assert resp.status_code == 204

        assert env.client.get("/api/schedules").json() == []
        assert get_scheduler_service().next_run_time(job["id"]) is None
        # Deleting again is a 404, not a crash.
        assert env.client.delete(f"/api/schedules/{job['id']}").status_code == 404

    def test_toggle_schedule_follows_registration(self, env):
        from spotifyforge.core.scheduler import get_scheduler_service

        login(env.client, env.fake)
        playlist = create_playlist(env.client, "Toggled Sync")
        job = env.client.post("/api/schedules", json=self._schedule_body(playlist["id"])).json()
        service = get_scheduler_service()
        assert service.next_run_time(job["id"]) is not None

        # Disable: row updated AND job unregistered.
        resp = env.client.put(f"/api/schedules/{job['id']}/toggle")
        assert resp.status_code == 200, resp.text
        assert resp.json()["enabled"] is False
        assert service.next_run_time(job["id"]) is None

        # Enable: registration comes back.
        resp = env.client.put(f"/api/schedules/{job['id']}/toggle")
        assert resp.json()["enabled"] is True
        assert service.next_run_time(job["id"]) is not None

    def test_toggle_missing_schedule_404(self, env):
        login(env.client, env.fake)
        assert env.client.put("/api/schedules/9999/toggle").status_code == 404


# ---------------------------------------------------------------------------
# Ownership isolation
# ---------------------------------------------------------------------------


class TestOwnershipIsolation:
    def test_user2_cannot_see_or_modify_user1_resources(self, env):
        # User 1 creates a playlist and a schedule.
        login(env.client, env.fake, "user1")
        playlist = create_playlist(env.client, "User1 Private")
        job = env.client.post(
            "/api/schedules",
            json={
                "name": "user1 sync",
                "job_type": "sync",
                "playlist_id": playlist["id"],
                "cron_expression": "0 3 * * *",
            },
        ).json()

        # User 2 logs in through a separate client sharing the same app + DB.
        with TestClient(env.app) as client2:
            login(client2, env.fake, "user2")
            assert client2.get("/api/auth/me").json()["spotify_id"] == "user2"

            # User 1's playlist is invisible in every way: list, read, write.
            assert client2.get("/api/playlists").json() == []
            pid = playlist["id"]
            assert client2.get(f"/api/playlists/{pid}").status_code == 404
            assert client2.put(f"/api/playlists/{pid}", json={"name": "hax"}).status_code == 404
            assert client2.post(f"/api/playlists/{pid}/sync").status_code == 404
            assert client2.post(f"/api/playlists/{pid}/deduplicate").status_code == 404
            assert (
                client2.post(f"/api/playlists/{pid}/tracks", json=[uri("abcdefghij")]).status_code
                == 404
            )

            # Same for schedules.
            assert client2.get("/api/schedules").json() == []
            assert client2.delete(f"/api/schedules/{job['id']}").status_code == 404
            assert client2.put(f"/api/schedules/{job['id']}/toggle").status_code == 404
            # And user 2 cannot schedule jobs against user 1's playlist.
            resp = client2.post(
                "/api/schedules",
                json={
                    "name": "steal",
                    "job_type": "sync",
                    "playlist_id": pid,
                    "cron_expression": "0 3 * * *",
                },
            )
            assert resp.status_code == 404

            # Nothing was harmed: user 1 still sees everything intact.
            assert [p["id"] for p in env.client.get("/api/playlists").json()] == [pid]
            assert env.client.get(f"/api/playlists/{pid}").json()["name"] == "User1 Private"
            assert [j["id"] for j in env.client.get("/api/schedules").json()] == [job["id"]]
