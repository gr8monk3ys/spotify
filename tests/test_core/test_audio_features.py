"""Tests for tempo/key lookup and Camelot-wheel distance.

Providers are exercised through ``httpx.MockTransport``, so the real
request-building and JSON parsing run without touching the network.
"""

from __future__ import annotations

import json

import httpx
import pytest

from spotifyforge.core.audio_features import (
    AcousticBrainzProvider,
    AudioFeature,
    DeezerProvider,
    FeatureCache,
    camelot,
    fetch_features,
    key_distance,
    parse_key,
)

MBID = "daa22b27-f253-46a1-9194-525d5f35bd89"


@pytest.fixture(autouse=True)
def _no_musicbrainz_sleep(monkeypatch):
    """Skip the 1s politeness delay so tests stay fast."""
    monkeypatch.setattr("spotifyforge.core.audio_features._MUSICBRAINZ_INTERVAL", 0)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# Key parsing and Camelot distance
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "scale", "expected"),
    [
        ("C", "major", (0, 1)),
        ("A", "minor", (9, 0)),
        ("F", "minor", (5, 0)),
        ("Bb", "major", (10, 1)),  # enharmonic spelling
        ("Db", "minor", (1, 0)),
        (None, "major", (None, None)),
        ("H", "major", (None, None)),  # not a pitch class
    ],
)
def test_parse_key(name, scale, expected):
    assert parse_key(name, scale) == expected


def test_camelot_positions_match_the_wheel():
    assert camelot(AudioFeature(key=9, mode=0)) == (8, "A")  # A minor
    assert camelot(AudioFeature(key=0, mode=1)) == (8, "B")  # C major
    assert camelot(AudioFeature(tempo=120.0)) is None  # no key


def test_key_distance_rates_compatible_mixes_as_near():
    a_minor = AudioFeature(key=9, mode=0)  # 8A
    c_major = AudioFeature(key=0, mode=1)  # 8B — relative major
    e_minor = AudioFeature(key=4, mode=0)  # 9A — one step round the wheel
    f_sharp = AudioFeature(key=6, mode=1)  # 2B — far side

    assert key_distance(a_minor, a_minor) == 0
    assert key_distance(a_minor, c_major) == 1
    assert key_distance(a_minor, e_minor) == 1
    assert key_distance(a_minor, f_sharp) > 1


def test_key_distance_is_neutral_when_a_key_is_unknown():
    known = AudioFeature(key=9, mode=0)
    unknown = AudioFeature(tempo=120.0)
    # Worse than a compatible mix, so known-good pairs still win.
    assert key_distance(known, unknown) > 1


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


async def test_deezer_provider_reads_bpm():
    def handler(request):
        assert request.url.path == "/track/isrc:GBUM71301234"
        return httpx.Response(200, json={"bpm": 132.1, "title": "White Noise"})

    async with _client(handler) as client:
        feature = await DeezerProvider().fetch(client, "GBUM71301234")
    assert feature.tempo == pytest.approx(132.1)
    assert not feature.has_key


async def test_deezer_zero_bpm_means_unanalysed_not_zero_tempo():
    async with _client(lambda r: httpx.Response(200, json={"bpm": 0})) as client:
        assert await DeezerProvider().fetch(client, "X") == AudioFeature()


async def test_deezer_survives_a_missing_track():
    async with _client(lambda r: httpx.Response(404, json={"error": {}})) as client:
        assert await DeezerProvider().fetch(client, "X") == AudioFeature()


async def test_acousticbrainz_provider_reads_key_and_bpm():
    def handler(request):
        if "musicbrainz" in request.url.host:
            return httpx.Response(200, json={"recordings": [{"id": MBID}]})
        assert request.url.path == f"/api/v1/{MBID}/low-level"
        return httpx.Response(
            200,
            json={"tonal": {"key_key": "F", "key_scale": "minor"}, "rhythm": {"bpm": 131.97}},
        )

    async with _client(handler) as client:
        feature = await AcousticBrainzProvider().fetch(client, "GBUM71301234")

    assert feature.tempo == pytest.approx(131.97)
    assert (feature.key, feature.mode) == (5, 0)  # F minor
    assert camelot(feature) == (4, "A")


async def test_acousticbrainz_returns_nothing_when_isrc_is_unknown():
    def handler(request):
        return httpx.Response(200, json={"recordings": []})

    async with _client(handler) as client:
        assert await AcousticBrainzProvider().fetch(client, "X") == AudioFeature()


async def test_acousticbrainz_tries_the_next_recording_when_one_has_no_analysis():
    seen = []

    def handler(request):
        if "musicbrainz" in request.url.host:
            return httpx.Response(200, json={"recordings": [{"id": "first"}, {"id": "second"}]})
        seen.append(request.url.path)
        if "first" in request.url.path:
            return httpx.Response(404, json={})
        return httpx.Response(
            200, json={"tonal": {"key_key": "C", "key_scale": "major"}, "rhythm": {"bpm": 90}}
        )

    async with _client(handler) as client:
        feature = await AcousticBrainzProvider().fetch(client, "X")

    assert len(seen) == 2
    assert (feature.key, feature.mode) == (0, 1)


# ---------------------------------------------------------------------------
# Cache + orchestration
# ---------------------------------------------------------------------------


async def test_fetch_features_caches_and_does_not_refetch(tmp_path):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"bpm": 120})

    cache = FeatureCache(tmp_path / "f.json")
    async with _client(handler) as client:
        first = await fetch_features(["A", "B"], [DeezerProvider()], cache, client=client)
        assert len(calls) == 2
        assert first["A"].tempo == 120

        # A second run over the same ISRCs must hit no network at all.
        reloaded = FeatureCache(tmp_path / "f.json")
        again = await fetch_features(["A", "B"], [DeezerProvider()], reloaded, client=client)

    assert len(calls) == 2
    assert again["A"].tempo == 120


async def test_a_miss_is_remembered_so_it_is_not_retried(tmp_path):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"bpm": 0})  # Deezer has no analysis

    path = tmp_path / "f.json"
    async with _client(handler) as client:
        await fetch_features(["A"], [DeezerProvider()], FeatureCache(path), client=client)
        await fetch_features(["A"], [DeezerProvider()], FeatureCache(path), client=client)

    assert len(calls) == 1


async def test_a_later_deep_run_tops_up_entries_a_quick_run_created(tmp_path):
    """The bug this guards: a tempo-only pass must not block a key pass."""

    def handler(request):
        if "deezer" in request.url.host:
            return httpx.Response(200, json={"bpm": 128})
        if "musicbrainz" in request.url.host:
            return httpx.Response(200, json={"recordings": [{"id": MBID}]})
        return httpx.Response(
            200, json={"tonal": {"key_key": "A", "key_scale": "minor"}, "rhythm": {"bpm": 127.5}}
        )

    path = tmp_path / "f.json"
    async with _client(handler) as client:
        quick = await fetch_features(["A"], [DeezerProvider()], FeatureCache(path), client=client)
        assert quick["A"].tempo == 128
        assert not quick["A"].has_key

        deep = await fetch_features(
            ["A"],
            [DeezerProvider(), AcousticBrainzProvider()],
            FeatureCache(path),
            client=client,
        )

    assert deep["A"].has_key
    assert (deep["A"].key, deep["A"].mode) == (9, 0)
    # The tempo Deezer already supplied is kept, not overwritten.
    assert deep["A"].tempo == 128


async def test_provider_failure_does_not_lose_the_other_results(tmp_path):
    def handler(request):
        if request.url.path.endswith("BAD"):
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json={"bpm": 100})

    cache = FeatureCache(tmp_path / "f.json")
    async with _client(handler) as client:
        features = await fetch_features(["GOOD", "BAD"], [DeezerProvider()], cache, client=client)

    assert features["GOOD"].tempo == 100
    assert features["BAD"] == AudioFeature()


def test_cache_survives_a_corrupt_file(tmp_path):
    path = tmp_path / "f.json"
    path.write_text("{not json")
    cache = FeatureCache(path)
    assert len(cache) == 0
    cache.put("A", AudioFeature(tempo=99.0), "deezer")
    cache.save()
    assert json.loads(path.read_text())["A"]["tempo"] == 99.0
