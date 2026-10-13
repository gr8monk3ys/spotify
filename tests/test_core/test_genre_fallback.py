"""Genres from MusicBrainz for artists Spotify left untagged.

MusicBrainz is served by ``httpx.MockTransport``; the lookup code under
test builds every request and parses every response.
"""

from __future__ import annotations

from datetime import timedelta

import httpx

from spotifyforge.core import genre_fallback
from spotifyforge.core.curation import CurationEngine, CurationTrack
from spotifyforge.core.genre_fallback import (
    TagCache,
    apply_fallback,
    fetch_artist_tags,
    load_fallback_tags,
)


def _track(track_id: str, artist: str, *, genres=(), isrc: str | None = None) -> CurationTrack:
    return CurationTrack(
        id=track_id,
        uri=f"spotify:track:{track_id}",
        name=track_id,
        artist_ids=(artist,),
        artist_names=(artist.title(),),
        release_year=2020,
        popularity=50,
        isrc=isrc or f"ISRC{track_id}",
        genres=tuple(genres),
    )


class FakeMusicBrainz:
    """Recordings by ISRC and artists by MBID, as the two search endpoints."""

    def __init__(self) -> None:
        self.recordings: dict[str, tuple[str, str]] = {}  # isrc -> (mbid, artist name)
        self.tags: dict[str, list[tuple[str, int]]] = {}  # mbid -> [(tag, votes)]
        self.requests: list[str] = []
        self.fail_artists = False
        self.refuse_next = 0  # answer this many requests with 503 first

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request.url.path)
        if self.refuse_next:
            self.refuse_next -= 1
            return httpx.Response(503)
        query = request.url.params["query"]
        assert "SpotifyForge" in request.headers["User-Agent"]
        if request.url.path.endswith("/recording"):
            wanted = [part.removeprefix("isrc:") for part in query.split(" OR ")]
            return httpx.Response(
                200,
                json={
                    "recordings": [
                        {
                            "id": f"rec-{isrc}",
                            "isrcs": [isrc],
                            "artist-credit": [
                                {
                                    "artist": {
                                        "id": self.recordings[isrc][0],
                                        "name": self.recordings[isrc][1],
                                    }
                                }
                            ],
                        }
                        for isrc in wanted
                        if isrc in self.recordings
                    ]
                },
            )
        if self.fail_artists:
            return httpx.Response(503)
        wanted = [part.removeprefix("arid:") for part in query.split(" OR ")]
        return httpx.Response(
            200,
            json={
                "artists": [
                    {
                        "id": mbid,
                        "tags": [{"name": n, "count": c} for n, c in self.tags.get(mbid, [])],
                    }
                    for mbid in wanted
                ]
            },
        )

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def _no_wait(monkeypatch) -> None:
    monkeypatch.setattr(genre_fallback, "_MUSICBRAINZ_INTERVAL", 0.0)
    monkeypatch.setattr(genre_fallback, "_BACKOFF_SECONDS", 0.0)


async def test_fetch_caches_tags_most_voted_first(tmp_path, monkeypatch):
    _no_wait(monkeypatch)
    mb = FakeMusicBrainz()
    mb.recordings["ISRCa"] = ("mb-1", "Quiet One")
    mb.tags["mb-1"] = [("seen live", 1), ("Dream Pop", 5), ("shoegaze", 3), ("ignored", 0)]
    cache = TagCache(tmp_path / "genres.json")

    report = await fetch_artist_tags([_track("a", "quiet one")], cache, client=mb.client())

    assert (report.artists_asked, report.artists_tagged) == (1, 1)
    assert load_fallback_tags(tmp_path / "genres.json") == {
        "quiet one": ("dream pop", "shoegaze", "seen live")
    }


async def test_only_untagged_artists_are_asked_about(tmp_path, monkeypatch):
    _no_wait(monkeypatch)
    mb = FakeMusicBrainz()
    cache = TagCache(tmp_path / "genres.json")

    report = await fetch_artist_tags(
        [_track("a", "tagged", genres=["rock"])], cache, client=mb.client()
    )

    assert report.artists_asked == 0
    assert mb.requests == []


async def test_an_artist_whose_name_disagrees_is_not_trusted(tmp_path, monkeypatch):
    """The ISRC resolved, but to a differently named act: no tags are taken."""
    _no_wait(monkeypatch)
    mb = FakeMusicBrainz()
    mb.recordings["ISRCa"] = ("mb-9", "Somebody Else")
    mb.tags["mb-9"] = [("techno", 9)]
    cache = TagCache(tmp_path / "genres.json")

    report = await fetch_artist_tags([_track("a", "part time")], cache, client=mb.client())

    assert report.artists_tagged == 0
    assert cache.tags() == {}


async def test_a_second_song_is_tried_when_the_first_is_unknown(tmp_path, monkeypatch):
    _no_wait(monkeypatch)
    mb = FakeMusicBrainz()
    mb.recordings["ISRCb"] = ("mb-1", "Quiet One")
    mb.tags["mb-1"] = [("slowcore", 2)]
    cache = TagCache(tmp_path / "genres.json")

    await fetch_artist_tags(
        [_track("a", "quiet one"), _track("b", "quiet one")], cache, client=mb.client()
    )

    assert cache.tags() == {"quiet one": ("slowcore",)}


async def test_blanks_are_remembered_and_retried_only_when_stale(tmp_path, monkeypatch):
    _no_wait(monkeypatch)
    mb = FakeMusicBrainz()  # knows nothing
    path = tmp_path / "genres.json"

    first = await fetch_artist_tags([_track("a", "obscure")], TagCache(path), client=mb.client())
    assert first.artists_asked == 1

    asked = len(mb.requests)
    again = await fetch_artist_tags([_track("a", "obscure")], TagCache(path), client=mb.client())
    assert again.artists_asked == 0
    assert len(mb.requests) == asked

    real_now = genre_fallback.utc_now
    monkeypatch.setattr(genre_fallback, "utc_now", lambda: real_now() + timedelta(days=31))
    later = await fetch_artist_tags([_track("a", "obscure")], TagCache(path), client=mb.client())
    assert later.artists_asked == 1


async def test_a_failed_artist_lookup_is_not_recorded_as_no_tags(tmp_path, monkeypatch):
    _no_wait(monkeypatch)
    mb = FakeMusicBrainz()
    mb.recordings["ISRCa"] = ("mb-1", "Quiet One")
    mb.tags["mb-1"] = [("dream pop", 5)]
    mb.fail_artists = True
    path = tmp_path / "genres.json"

    await fetch_artist_tags([_track("a", "quiet one")], TagCache(path), client=mb.client())
    assert TagCache(path).due("quiet one")  # a 503 is not an answer... yet

    mb.fail_artists = False
    await fetch_artist_tags([_track("a", "quiet one")], TagCache(path), client=mb.client())
    assert TagCache(path).tags() == {"quiet one": ("dream pop",)}


async def test_a_503_is_retried_before_giving_up(tmp_path, monkeypatch):
    _no_wait(monkeypatch)
    mb = FakeMusicBrainz()
    mb.recordings["ISRCa"] = ("mb-1", "Quiet One")
    mb.tags["mb-1"] = [("dream pop", 5)]
    mb.refuse_next = 2
    cache = TagCache(tmp_path / "genres.json")

    await fetch_artist_tags([_track("a", "quiet one")], cache, client=mb.client())

    assert cache.tags() == {"quiet one": ("dream pop",)}


def test_apply_takes_only_tags_that_are_already_genres_here():
    tracks = [
        _track("known", "tagged", genres=["dream pop", "shoegaze"]),
        _track("blank", "quiet one"),
    ]

    out = apply_fallback(tracks, {"quiet one": ("seen live", "dream pop", "zeuhl")})

    assert out[0].genres == ("dream pop", "shoegaze")
    assert out[1].genres == ("dream pop",)


def test_apply_caps_how_many_genres_a_fallback_can_add():
    vocabulary = ["a", "b", "c", "d", "e"]
    tracks = [_track("known", "tagged", genres=vocabulary), _track("blank", "quiet one")]

    out = apply_fallback(tracks, {"quiet one": tuple(vocabulary)})

    assert out[1].genres == ("a", "b", "c")


def test_apply_never_overrides_spotify():
    tracks = [_track("known", "tagged", genres=["rock"]), _track("other", "also", genres=["jazz"])]

    out = apply_fallback(tracks, {"tagged": ("jazz",)})

    assert out[0].genres == ("rock",)


def test_apply_leaves_a_track_blank_when_no_tag_is_a_known_genre():
    tracks = [_track("known", "tagged", genres=["rock"]), _track("blank", "quiet one")]

    out = apply_fallback(tracks, {"quiet one": ("seen live",)})

    assert out[1].genres == ()


async def test_enrichment_uses_the_cache_on_disk(fake_spotify, client_for, tmp_path):
    """The one place genres are attached reads the fallback, so every
    consumer (plan, export, MCP) sees the same answer."""
    fake = fake_spotify
    fake.add_user("user1")
    fake.add_track("t1", artist_id="tagged", artist_name="Tagged")
    fake.add_track("t2", artist_id="blank", artist_name="Blank")
    fake.set_artist_genres("tagged", ["dream pop"])
    fake.save_track("user1", "t1")
    fake.save_track("user1", "t2")
    engine = CurationEngine(client_for())

    before = await engine.enrich_genres(await engine.fetch_liked())
    assert {t.id: t.genres for t in before} == {"t1": ("dream pop",), "t2": ()}

    cache = TagCache()  # the default path, which tests redirect to tmp_path
    cache.put("blank", ["dream pop", "seen live"], "mb-1")
    cache.save()
    assert cache.path.parent == tmp_path

    after = await engine.enrich_genres(await engine.fetch_liked())
    assert {t.id: t.genres for t in after} == {"t1": ("dream pop",), "t2": ("dream pop",)}
