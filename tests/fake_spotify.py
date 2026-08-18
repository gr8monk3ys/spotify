"""An in-memory fake of the Spotify Web API + accounts service for tests.

Served through ``httpx.MockTransport``, so requests flow through tekore's
real request-building and response-parsing code — the tests exercise the
same client stack production uses, minus the network.

The fake enforces authentication: any request whose bearer token was not
issued by its own token endpoint (or seeded via ``issue_token``) gets a
401. That property is what proves tokens are decrypted, refreshed, and
attached correctly end to end.
"""

from __future__ import annotations

import json
import re
import secrets
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import tekore as tk

API = "https://api.spotify.com/v1"


def _artist(artist_id: str, name: str) -> dict[str, Any]:
    return {
        "external_urls": {"spotify": f"https://open.spotify.com/artist/{artist_id}"},
        "href": f"{API}/artists/{artist_id}",
        "id": artist_id,
        "name": name,
        "type": "artist",
        "uri": f"spotify:artist:{artist_id}",
    }


def _full_artist(artist_id: str, name: str, popularity: int = 50, genres: list[str] | None = None):
    return {
        **_artist(artist_id, name),
        "followers": {"href": None, "total": 1000},
        "genres": genres if genres is not None else ["indie-rock"],
        "images": [],
        "popularity": popularity,
    }


def _album(
    album_id: str, name: str, artist: dict[str, Any], release_date: str = "2024-01-01"
) -> dict[str, Any]:
    return {
        "album_type": "album",
        "artists": [artist],
        "available_markets": ["US"],
        "external_urls": {"spotify": f"https://open.spotify.com/album/{album_id}"},
        "href": f"{API}/albums/{album_id}",
        "id": album_id,
        "images": [],
        "name": name,
        "release_date": release_date,
        "release_date_precision": "day",
        "total_tracks": 10,
        "type": "album",
        "uri": f"spotify:album:{album_id}",
        "album_group": "album",
    }


class FakeSpotify:
    """In-memory Spotify state plus an httpx request handler."""

    def __init__(self) -> None:
        self.users: dict[str, dict[str, Any]] = {}
        self.playlists: dict[str, dict[str, Any]] = {}
        # playlist_id -> ordered list of track ids (duplicates allowed)
        self.playlist_tracks: dict[str, list[str]] = {}
        self.tracks: dict[str, dict[str, Any]] = {}
        self.top_tracks: dict[str, list[str]] = {}  # user -> track ids
        self.top_artists: dict[str, list[dict[str, Any]]] = {}
        self.artist_albums: dict[str, list[str]] = {}  # artist -> album ids
        self.album_tracks: dict[str, list[str]] = {}  # album -> track ids
        self.albums: dict[str, dict[str, Any]] = {}
        self.saved_tracks: dict[str, list[str]] = {}  # user -> liked track ids
        self.artist_genres: dict[str, list[str]] = {}
        self.artist_names: dict[str, str] = {}

        self.valid_tokens: set[str] = set()
        self.valid_codes: dict[str, str] = {}  # auth code -> spotify user id
        self.refresh_tokens: dict[str, str] = {}  # refresh token -> user id
        self.token_owner: dict[str, str] = {}  # access token -> user id
        self._counter = 0

        # Request log for assertions.
        self.requests: list[tuple[str, str]] = []

    # -- state builders -------------------------------------------------

    def add_user(self, user_id: str = "user1", display_name: str = "Test User") -> str:
        self.users[user_id] = {
            "id": user_id,
            "account_id": user_id,
            "display_name": display_name,
            "email": f"{user_id}@example.com",
            "product": "premium",
            "external_urls": {"spotify": f"https://open.spotify.com/user/{user_id}"},
            "href": f"{API}/users/{user_id}",
            "type": "user",
            "uri": f"spotify:user:{user_id}",
            "country": "US",
            "explicit_content": {"filter_enabled": False, "filter_locked": False},
            "followers": {"href": None, "total": 3},
            "images": [],
        }
        self.top_tracks.setdefault(user_id, [])
        self.top_artists.setdefault(user_id, [])
        return user_id

    def add_track(
        self,
        track_id: str,
        name: str | None = None,
        popularity: int = 50,
        artist_id: str = "art1",
        artist_name: str = "Artist One",
        album_id: str = "alb1",
        album_name: str = "Album One",
        release_date: str = "2024-01-01",
    ) -> str:
        artist = _artist(artist_id, artist_name)
        self.artist_names[artist_id] = artist_name
        self.albums.setdefault(album_id, _album(album_id, album_name, artist, release_date))
        self.tracks[track_id] = {
            "album": self.albums[album_id],
            "artists": [artist],
            "available_markets": ["US"],
            "disc_number": 1,
            "duration_ms": 200_000,
            "explicit": False,
            "external_ids": {"isrc": f"ISRC{track_id[:8].upper()}"},
            "external_urls": {"spotify": f"https://open.spotify.com/track/{track_id}"},
            "href": f"{API}/tracks/{track_id}",
            "id": track_id,
            "is_local": False,
            "name": name or f"Track {track_id}",
            "popularity": popularity,
            "preview_url": None,
            "track_number": 1,
            "type": "track",
            "uri": f"spotify:track:{track_id}",
        }
        return track_id

    def add_playlist(
        self,
        playlist_id: str,
        owner: str = "user1",
        name: str | None = None,
        track_ids: list[str] | None = None,
        public: bool = True,
        followers: int = 7,
    ) -> str:
        self.playlists[playlist_id] = {
            "id": playlist_id,
            "name": name or f"Playlist {playlist_id}",
            "description": "",
            "public": public,
            "collaborative": False,
            "owner": owner,
            "followers": followers,
            "snapshot_id": f"snap_{playlist_id}_0",
        }
        self.playlist_tracks[playlist_id] = list(track_ids or [])
        return playlist_id

    def issue_token(self, user_id: str) -> dict[str, Any]:
        """Mint a valid access/refresh token pair for *user_id*."""
        access = f"access_{user_id}_{secrets.token_hex(8)}"
        refresh = f"refresh_{user_id}_{secrets.token_hex(8)}"
        self.valid_tokens.add(access)
        self.token_owner[access] = user_id
        self.refresh_tokens[refresh] = user_id
        return {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": 3600,
            "refresh_token": refresh,
            "scope": "playlist-modify-public playlist-modify-private",
        }

    def save_track(self, user_id: str, track_id: str) -> None:
        """Add *track_id* to *user_id*'s liked songs."""
        self.saved_tracks.setdefault(user_id, []).append(track_id)

    def set_artist_genres(self, artist_id: str, genres: list[str]) -> None:
        """Set the genre list returned for *artist_id* by ``GET /v1/artists``."""
        self.artist_genres[artist_id] = list(genres)

    def issue_code(self, user_id: str) -> str:
        """Mint a one-time authorization code for *user_id*."""
        code = f"code_{secrets.token_hex(8)}"
        self.valid_codes[code] = user_id
        return code

    # -- transports ------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def async_client(self, user_id: str = "user1") -> tk.Spotify:
        """An async tekore client wired to this fake, as production builds it."""
        token = self.issue_token(user_id)["access_token"]
        sender = tk.RetryingSender(
            retries=2, sender=tk.AsyncSender(client=httpx.AsyncClient(transport=self.transport()))
        )
        return tk.Spotify(token, sender=sender)

    # -- helpers ---------------------------------------------------------

    def _snapshot_bump(self, playlist_id: str) -> str:
        self._counter += 1
        snap = f"snap_{playlist_id}_{self._counter}"
        self.playlists[playlist_id]["snapshot_id"] = snap
        return snap

    def _playlist_track_item(self, track_id: str) -> dict[str, Any]:
        return {
            "added_at": "2024-01-01T00:00:00Z",
            "added_by": {
                "external_urls": {},
                "href": f"{API}/users/user1",
                "id": "user1",
                "type": "user",
                "uri": "spotify:user:user1",
            },
            "is_local": False,
            "primary_color": None,
            "track": {**self.tracks[track_id], "episode": False, "track": True},
            "video_thumbnail": {"url": None},
        }

    def _paging(
        self, base_url: str, items: list[Any], limit: int, offset: int, total: int
    ) -> dict[str, Any]:
        next_url = (
            f"{base_url}?offset={offset + limit}&limit={limit}" if offset + limit < total else None
        )
        prev_url = (
            f"{base_url}?offset={max(offset - limit, 0)}&limit={limit}" if offset > 0 else None
        )
        return {
            "href": f"{base_url}?offset={offset}&limit={limit}",
            "items": items,
            "limit": limit,
            "next": next_url,
            "offset": offset,
            "previous": prev_url,
            "total": total,
        }

    def _playlist_payload(self, playlist_id: str) -> dict[str, Any]:
        meta = self.playlists[playlist_id]
        track_ids = self.playlist_tracks[playlist_id]
        items = [self._playlist_track_item(t) for t in track_ids[:100]]
        return {
            "collaborative": meta["collaborative"],
            "description": meta["description"],
            "external_urls": {"spotify": f"https://open.spotify.com/playlist/{playlist_id}"},
            "followers": {"href": None, "total": meta.get("followers", 7)},
            "href": f"{API}/playlists/{playlist_id}",
            "id": playlist_id,
            "images": [],
            "name": meta["name"],
            "owner": self._owner_payload(meta["owner"]),
            "primary_color": None,
            "public": meta["public"],
            "snapshot_id": meta["snapshot_id"],
            # tekore 6 exposes the same paging under both names; sharing the
            # dict is fine — it is serialized to JSON immediately.
            "tracks": (
                paging := self._paging(
                    f"{API}/playlists/{playlist_id}/tracks", items, 100, 0, len(track_ids)
                )
            ),
            "items": paging,
            "type": "playlist",
            "uri": f"spotify:playlist:{playlist_id}",
        }

    def _simple_playlist_payload(self, playlist_id: str) -> dict[str, Any]:
        meta = self.playlists[playlist_id]
        stub = {
            "href": f"{API}/playlists/{playlist_id}/tracks",
            "total": len(self.playlist_tracks[playlist_id]),
        }
        return {
            "collaborative": meta["collaborative"],
            "description": meta["description"],
            "external_urls": {"spotify": f"https://open.spotify.com/playlist/{playlist_id}"},
            "href": f"{API}/playlists/{playlist_id}",
            "id": playlist_id,
            "images": [],
            "name": meta["name"],
            "owner": self._owner_payload(meta["owner"]),
            "primary_color": None,
            "public": meta["public"],
            "snapshot_id": meta["snapshot_id"],
            "tracks": stub,
            "items": stub,  # tekore 6 aliases the {href,total} stub
            "type": "playlist",
            "uri": f"spotify:playlist:{playlist_id}",
        }

    def _owner_payload(self, user_id: str) -> dict[str, Any]:
        return {
            "display_name": self.users.get(user_id, {}).get("display_name", user_id),
            "external_urls": {},
            "href": f"{API}/users/{user_id}",
            "id": user_id,
            "type": "user",
            "uri": f"spotify:user:{user_id}",
        }

    # -- the HTTP handler ------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:  # noqa: PLR0911
        url = urlparse(str(request.url))
        path = url.path.rstrip("/") or "/"
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        self.requests.append((request.method, path))

        # ---- accounts.spotify.com: token endpoint ----
        if url.netloc == "accounts.spotify.com":
            if path == "/api/token":
                form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
                grant = form.get("grant_type")
                if grant == "authorization_code":
                    user_id = self.valid_codes.pop(form.get("code", ""), None)
                    if user_id is None:
                        return httpx.Response(400, json={"error": "invalid_grant"})
                    return httpx.Response(200, json=self.issue_token(user_id))
                if grant == "refresh_token":
                    user_id = self.refresh_tokens.get(form.get("refresh_token", ""))
                    if user_id is None:
                        return httpx.Response(400, json={"error": "invalid_grant"})
                    payload = self.issue_token(user_id)
                    payload.pop("refresh_token")  # Spotify may omit it on refresh
                    return httpx.Response(200, json=payload)
                return httpx.Response(400, json={"error": "unsupported_grant_type"})
            return httpx.Response(404, json={"error": "not found"})

        # ---- api.spotify.com: bearer auth required ----
        auth = request.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ").strip()
        if token not in self.valid_tokens:
            return httpx.Response(
                401, json={"error": {"status": 401, "message": "Invalid access token"}}
            )
        me = self.token_owner[token]

        limit = int(query.get("limit", 20))
        offset = int(query.get("offset", 0))

        if path == "/v1/me":
            return httpx.Response(200, json=self.users[me])

        if path.startswith("/v1/users/") and path.endswith("/playlists"):
            user_id = path.split("/")[3]
            if request.method == "POST":
                body = json.loads(request.content)
                self._counter += 1
                pid = f"pl_created_{self._counter}"
                self.add_playlist(
                    pid,
                    owner=user_id,
                    name=body["name"],
                    public=body.get("public", True),
                )
                self.playlists[pid]["description"] = body.get("description", "")
                return httpx.Response(201, json=self._playlist_payload(pid))
            owned = [p for p, m in self.playlists.items() if m["owner"] == user_id]
            items = [self._simple_playlist_payload(p) for p in owned[offset : offset + limit]]
            return httpx.Response(
                200,
                json=self._paging(
                    f"{API}/users/{user_id}/playlists", items, limit, offset, len(owned)
                ),
            )

        if path.startswith("/v1/playlists/"):
            parts = path.split("/")
            playlist_id = parts[3]
            if playlist_id not in self.playlists:
                return httpx.Response(404, json={"error": {"status": 404, "message": "Not found"}})

            if len(parts) == 4:
                if request.method == "PUT":  # change details
                    body = json.loads(request.content)
                    meta = self.playlists[playlist_id]
                    for key in ("name", "description", "public", "collaborative"):
                        if key in body and body[key] is not None:
                            meta[key] = body[key]
                    return httpx.Response(200)
                return httpx.Response(200, json=self._playlist_payload(playlist_id))

            if parts[4] == "tracks":
                track_ids = self.playlist_tracks[playlist_id]
                if request.method == "GET":
                    items = [
                        self._playlist_track_item(t) for t in track_ids[offset : offset + limit]
                    ]
                    return httpx.Response(
                        200,
                        json=self._paging(
                            f"{API}/playlists/{playlist_id}/tracks",
                            items,
                            limit,
                            offset,
                            len(track_ids),
                        ),
                    )
                if request.method == "PUT":  # replace the whole track list
                    body = json.loads(request.content)
                    self.playlist_tracks[playlist_id] = [
                        u.rsplit(":", 1)[-1] for u in body.get("uris", [])
                    ]
                    return httpx.Response(
                        201, json={"snapshot_id": self._snapshot_bump(playlist_id)}
                    )
                if request.method == "POST":  # add
                    body = json.loads(request.content)
                    uris = body.get("uris", [])
                    new_ids = [u.rsplit(":", 1)[-1] for u in uris]
                    # tekore passes position as a query param; accept body too
                    position = body.get("position")
                    if position is None and "position" in query:
                        position = int(query["position"])
                    if position is None:
                        track_ids.extend(new_ids)
                    else:
                        track_ids[position:position] = new_ids
                    return httpx.Response(
                        201, json={"snapshot_id": self._snapshot_bump(playlist_id)}
                    )
                if request.method == "DELETE":  # remove all occurrences per URI
                    body = json.loads(request.content)
                    remove_ids = {t["uri"].rsplit(":", 1)[-1] for t in body.get("tracks", [])}
                    self.playlist_tracks[playlist_id] = [
                        t for t in track_ids if t not in remove_ids
                    ]
                    return httpx.Response(
                        200, json={"snapshot_id": self._snapshot_bump(playlist_id)}
                    )

        if path == "/v1/me/tracks":
            ids = self.saved_tracks.get(me, [])
            items = [
                {"added_at": "2024-01-01T00:00:00Z", "track": self.tracks[t]}
                for t in ids[offset : offset + limit]
            ]
            return httpx.Response(
                200, json=self._paging(f"{API}/me/tracks", items, limit, offset, len(ids))
            )

        if path == "/v1/artists":
            ids = query.get("ids", "").split(",")
            artists = [
                _full_artist(
                    aid,
                    self.artist_names.get(aid, f"Artist {aid}"),
                    genres=self.artist_genres.get(aid, []),
                )
                for aid in ids
                if aid
            ]
            return httpx.Response(200, json={"artists": artists})

        if path == "/v1/me/top/tracks":
            ids = self.top_tracks.get(me, [])
            items = [self.tracks[t] for t in ids[offset : offset + limit]]
            return httpx.Response(
                200, json=self._paging(f"{API}/me/top/tracks", items, limit, offset, len(ids))
            )

        if path == "/v1/me/top/artists":
            artists = self.top_artists.get(me, [])
            items = artists[offset : offset + limit]
            return httpx.Response(
                200,
                json=self._paging(f"{API}/me/top/artists", items, limit, offset, len(artists)),
            )

        if path == "/v1/search":
            # Match on the words of the query's free-text head ("indie-rock
            # genre:indie-rock" -> {"indie", "rock"}); filters are ignored —
            # track names are the fake's search corpus.
            q = query.get("q", "")
            head = q.split()[0].lower() if q.split() else ""
            words = [w for w in re.split(r"[^a-z0-9]+", head) if w]
            types = query.get("type", "track").split(",")

            if "playlist" in types:
                hits = [
                    p
                    for p, meta in self.playlists.items()
                    if words and any(w in meta["name"].lower() for w in words)
                ]
                items = [self._simple_playlist_payload(p) for p in hits[offset : offset + limit]]
                return httpx.Response(
                    200,
                    json={
                        "playlists": self._paging(f"{API}/search", items, limit, offset, len(hits))
                    },
                )

            matches = [
                t
                for t in self.tracks.values()
                if words and any(w in t["name"].lower() for w in words)
            ]
            items = matches[offset : offset + limit]
            return httpx.Response(
                200,
                json={"tracks": self._paging(f"{API}/search", items, limit, offset, len(matches))},
            )

        if path.startswith("/v1/artists/") and path.endswith("/albums"):
            artist_id = path.split("/")[3]
            album_ids = self.artist_albums.get(artist_id, [])
            items = [self.albums[a] for a in album_ids[offset : offset + limit]]
            return httpx.Response(
                200,
                json=self._paging(
                    f"{API}/artists/{artist_id}/albums", items, limit, offset, len(album_ids)
                ),
            )

        if path.startswith("/v1/albums/") and path.endswith("/tracks"):
            album_id = path.split("/")[3]
            track_ids = self.album_tracks.get(album_id, [])
            items = []
            for t in track_ids[offset : offset + limit]:
                simple = dict(self.tracks[t])
                simple.pop("album", None)
                simple.pop("external_ids", None)
                simple.pop("popularity", None)
                items.append(simple)
            return httpx.Response(
                200,
                json=self._paging(
                    f"{API}/albums/{album_id}/tracks", items, limit, offset, len(track_ids)
                ),
            )

        if path == "/v1/tracks":
            ids = query.get("ids", "").split(",")
            return httpx.Response(
                200, json={"tracks": [self.tracks[i] for i in ids if i in self.tracks]}
            )

        return httpx.Response(
            404, json={"error": {"status": 404, "message": f"No fake for {path}"}}
        )
