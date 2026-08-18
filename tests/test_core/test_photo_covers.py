"""Tests for photo cover art (core/photo_covers.py).

Pexels is faked with an httpx.MockTransport; Spotify uploads go through
the FakeSpotify backend's new images endpoint.
"""

from __future__ import annotations

import io
import json

import httpx
import pytest
from PIL import Image

from spotifyforge.core.photo_covers import (
    PexelsSource,
    RateLimitedError,
    apply_photo_covers,
    choose_photo,
    load_picks,
    save_picks,
    scene_query,
    to_cover,
)


def _photo_payload(photo_id: int, name: str = "Ana Lens") -> dict:
    return {
        "id": photo_id,
        "photographer": name,
        "url": f"https://www.pexels.com/photo/{photo_id}/",
        "src": {"large2x": f"https://images.pexels.com/{photo_id}.jpg"},
    }


def _jpeg_bytes(width: int = 800, height: int = 500) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (180, 40, 90)).save(buffer, format="JPEG")
    return buffer.getvalue()


def _pexels(responses: dict[str, list[dict]], limit_after: int | None = None):
    """A PexelsSource whose search results come from *responses*."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.pexels.com":
            calls["n"] += 1
            if limit_after is not None and calls["n"] > limit_after:
                return httpx.Response(429)
            query = request.url.params["query"]
            return httpx.Response(200, json={"photos": responses.get(query, [])})
        return httpx.Response(200, content=_jpeg_bytes())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return PexelsSource("key", client=client), calls


# ---------------------------------------------------------------------------
# Pure pieces
# ---------------------------------------------------------------------------


def test_scene_query_maps_families_and_defaults():
    assert "saxophone" in scene_query("latin jazz")
    assert "neon" in scene_query("hard techno")
    assert scene_query("zeuhl") == "abstract film grain texture"


def test_to_cover_squares_grades_and_fits_limit():
    cover = to_cover(_jpeg_bytes(801, 333))
    image = Image.open(io.BytesIO(cover))
    assert image.size == (640, 640)
    assert len(cover) < 256 * 1024


def test_picks_roundtrip(tmp_path):
    path = tmp_path / "photo_covers.json"
    save_picks({"strictly coldwave": {"photo_id": 7, "photographer": "Ana Lens"}}, path)
    assert load_picks(path)["strictly coldwave"]["photo_id"] == 7
    assert load_picks(tmp_path / "missing.json") == {}


# ---------------------------------------------------------------------------
# Choosing
# ---------------------------------------------------------------------------


async def test_choose_photo_is_stable_and_falls_back_to_scene():
    photos = [_photo_payload(i) for i in range(5)]
    source, _ = _pexels({"saxophone smoke dark stage": photos})
    try:
        # "bebop" itself finds nothing; the jazz scene fallback does.
        first = await choose_photo(source, "blue hours, hard bop", "bebop")
        again = await choose_photo(source, "blue hours, hard bop", "bebop")
        other = await choose_photo(source, "the bebop index", "bebop")
    finally:
        await source.close()
    assert first is not None and first["query"] == "saxophone smoke dark stage"
    assert first == again  # hashed from the title → stable
    assert other is not None  # same query pool, own pick


async def test_choose_photo_none_when_nothing_matches():
    source, _ = _pexels({})
    try:
        assert await choose_photo(source, "t", "zeuhl") is None
    finally:
        await source.close()


# ---------------------------------------------------------------------------
# Applying (through the fake Spotify backend)
# ---------------------------------------------------------------------------


async def test_apply_uploads_skips_pinned_and_records_attribution(
    fake_spotify, client_for, tmp_path
):
    fake_spotify.add_user("user1")
    fake_spotify.add_playlist("pl1", name="strictly coldwave")
    fake_spotify.add_playlist("pl2", name="marble rooms")
    path = tmp_path / "photo_covers.json"
    sp = client_for("user1")
    source, calls = _pexels(
        {
            "coldwave": [_photo_payload(1)],
            "marble rooms": [_photo_payload(2, name="Béla Stone")],
        }
    )
    targets = [("strictly coldwave", "pl1", "coldwave"), ("marble rooms", "pl2", "marble rooms")]
    try:
        covered, failed, limited = await apply_photo_covers(sp, targets, source, path=path, delay=0)
        assert (covered, failed, limited) == (["strictly coldwave", "marble rooms"], [], False)
        assert fake_spotify.playlists["pl1"]["cover_image"]
        picks = json.loads(path.read_text())
        assert picks["marble rooms"]["photographer"] == "Béla Stone"

        # Second run: everything pinned, zero API calls, zero uploads.
        calls["n"] = 0
        covered, failed, limited = await apply_photo_covers(sp, targets, source, path=path, delay=0)
        assert (covered, failed, limited) == ([], [], False)
        assert calls["n"] == 0
    finally:
        await source.close()


async def test_apply_pauses_on_rate_limit_and_resumes(fake_spotify, client_for, tmp_path):
    fake_spotify.add_user("user1")
    fake_spotify.add_playlist("pl1", name="a")
    fake_spotify.add_playlist("pl2", name="b")
    path = tmp_path / "photo_covers.json"
    sp = client_for("user1")
    # Enough quota for the first playlist's search only ("a" then 429).
    source, _ = _pexels({"va": [_photo_payload(1)], "vb": [_photo_payload(2)]}, limit_after=1)
    targets = [("a", "pl1", "va"), ("b", "pl2", "vb")]
    try:
        covered, failed, limited = await apply_photo_covers(sp, targets, source, path=path, delay=0)
        assert (covered, limited) == (["a"], True)
        assert "a" in load_picks(path)

        fresh, _ = _pexels({"vb": [_photo_payload(2)]})
        try:
            covered, failed, limited = await apply_photo_covers(
                sp, targets, fresh, path=path, delay=0
            )
        finally:
            await fresh.close()
        assert (covered, failed, limited) == (["b"], [], False)
    finally:
        await source.close()


async def test_apply_tolerates_a_bad_photo(fake_spotify, client_for, tmp_path):
    fake_spotify.add_user("user1")
    fake_spotify.add_playlist("pl1", name="a")
    fake_spotify.add_playlist("pl2", name="b")
    sp = client_for("user1")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.pexels.com":
            query = request.url.params["query"]
            return httpx.Response(200, json={"photos": [_photo_payload(1 if query == "va" else 2)]})
        if "1.jpg" in str(request.url):
            return httpx.Response(200, content=b"not an image")
        return httpx.Response(200, content=_jpeg_bytes())

    source = PexelsSource("key", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        covered, failed, limited = await apply_photo_covers(
            sp,
            [("a", "pl1", "va"), ("b", "pl2", "vb")],
            source,
            path=tmp_path / "p.json",
            delay=0,
        )
    finally:
        await source.close()
    assert (covered, failed, limited) == (["b"], ["a"], False)


async def test_source_raises_rate_limited():
    source, _ = _pexels({}, limit_after=0)
    try:
        with pytest.raises(RateLimitedError):
            await source.search("anything")
    finally:
        await source.close()
