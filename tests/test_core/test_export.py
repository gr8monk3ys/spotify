"""Tests for the interchange export (core/export.py).

The shape asserted here is a contract two other repos read, so these
tests are deliberately about structure and ordering, not just values.
"""

from __future__ import annotations

import json

import pytest

from spotifyforge.core.audio_features import AudioFeature
from spotifyforge.core.curation import CurationTrack
from spotifyforge.core.export import (
    SCHEMA,
    build_library_export,
    load_export,
    write_export,
)


def _track(
    track_id: str,
    *,
    album_id: str | None = "alb1",
    album_name: str = "Kobaïa",
    total: int | None = 10,
    artists: tuple[tuple[str, str], ...] = (("a1", "Magma"),),
    isrc: str | None = None,
    genres: tuple[str, ...] = ("zeuhl",),
    year: int | None = 1970,
) -> CurationTrack:
    return CurationTrack(
        id=track_id,
        uri=f"spotify:track:{track_id}",
        name=f"Song {track_id}",
        artist_ids=tuple(a for a, _ in artists),
        artist_names=tuple(n for _, n in artists),
        release_year=year,
        popularity=20,
        isrc=isrc or f"ISRC{track_id.upper()}",
        genres=genres,
        album_id=album_id,
        album_name=album_name,
        album_total_tracks=total,
    )


def _build(tracks, features=None, expansions=None):
    return build_library_export(
        tracks, features or {}, expansions or {}, "gr8monk3ys", "2026-08-19T00:00:00+00:00"
    )


def test_rolls_liked_tracks_up_to_albums_with_affinity():
    doc = _build([_track("t1"), _track("t2"), _track("t3")])

    (album,) = doc["albums"]
    assert album["spotify_album_id"] == "alb1"
    assert album["title"] == "Kobaïa"
    assert album["liked_track_count"] == 3
    assert album["total_tracks"] == 10
    assert album["affinity"] == 0.3  # 3 of 10 — a playlist add, not a record
    assert album["isrcs"] == ["ISRCT1", "ISRCT2", "ISRCT3"]
    assert album["year"] == 1970
    assert album["genres"] == ["zeuhl"]


def test_affinity_is_none_when_album_length_is_unknown():
    """Spotify omits total_tracks on some albums; a made-up denominator
    would rank that album as if it were fully liked."""
    (album,) = _build([_track("t1", total=None)])["albums"]
    assert album["total_tracks"] is None
    assert album["affinity"] is None


def test_albums_ordered_by_liked_count_then_title():
    doc = _build(
        [
            _track("t1", album_id="b", album_name="Bbb"),
            _track("t2", album_id="c", album_name="Aaa"),
            _track("t3", album_id="a", album_name="Zzz"),
            _track("t4", album_id="a", album_name="Zzz"),
        ]
    )
    assert [a["title"] for a in doc["albums"]] == ["Zzz", "Aaa", "Bbb"]


def test_artists_are_credited_by_how_much_of_the_album_is_liked():
    """On a compilation the billed artist is not who the user came for."""
    doc = _build(
        [
            _track("t1", artists=(("a1", "Alice"),)),
            _track("t2", artists=(("a2", "Bob"),)),
            _track("t3", artists=(("a2", "Bob"),)),
        ]
    )
    (album,) = doc["albums"]
    assert [a["name"] for a in album["artists"]] == ["Bob", "Alice"]


def test_counts_tempo_and_key_coverage_per_album():
    features = {
        "ISRCT1": AudioFeature(tempo=120.0, key=5, mode=1),
        "ISRCT2": AudioFeature(tempo=98.0),  # tempo only
        # t3 absent entirely
    }
    (album,) = _build([_track("t1"), _track("t2"), _track("t3")], features)["albums"]
    assert (album["tempo_known"], album["key_known"]) == (2, 1)


def test_tracks_without_an_album_are_skipped_not_crashed():
    doc = _build([_track("t1"), _track("t2", album_id=None)])
    (album,) = doc["albums"]
    assert album["liked_track_count"] == 1


def test_discoveries_stay_separate_from_albums():
    """Unheard pins must never be mistaken for listened-to music."""
    pins = {("dungeon synth", None): [_track("d1", album_id="dalb")]}
    doc = _build([_track("t1")], expansions=pins)

    assert [a["spotify_album_id"] for a in doc["albums"]] == ["alb1"]
    (niche,) = doc["discoveries"]
    assert niche["genre"] == "dungeon synth"
    assert niche["tracks"][0]["artists"] == ["Magma"]


def test_empty_pin_lists_are_dropped():
    doc = _build([_track("t1")], expansions={("gqom", None): []})
    assert doc["discoveries"] == []


def test_document_carries_schema_and_provenance():
    doc = _build([_track("t1")])
    assert doc["schema"] == SCHEMA
    assert doc["source"] == {"platform": "spotify", "user": "gr8monk3ys"}
    assert doc["generated_at"] == "2026-08-19T00:00:00+00:00"


def test_roundtrip_and_schema_gate(tmp_path):
    path = tmp_path / "music-library.json"
    write_export(_build([_track("t1")]), path)

    assert load_export(path)["albums"][0]["spotify_album_id"] == "alb1"

    stale = json.loads(path.read_text())
    stale["schema"] = "music-library/99"
    path.write_text(json.dumps(stale))
    with pytest.raises(ValueError, match="Unsupported export schema"):
        load_export(path)


async def test_album_identity_survives_the_real_fetch_path(fake_spotify, client_for):
    """The exporter is only useful if album fields actually arrive from
    the API — they are dropped by every other curation path."""
    from spotifyforge.core.curation import CurationEngine

    fake_spotify.add_user("user1")
    for i in range(3):
        fake_spotify.add_track(
            f"lib{i}",
            name=f"Kobaian Chant {i}",
            artist_id="a1",
            artist_name="Magma",
            album_id="alb1",
            release_date="1970-01-01",
        )
        fake_spotify.save_track("user1", f"lib{i}")

    tracks = await CurationEngine(client_for("user1")).fetch_liked()

    assert {t.album_id for t in tracks} == {"alb1"}
    assert all(t.album_name for t in tracks)
    (album,) = _build(tracks)["albums"]
    assert album["liked_track_count"] == 3


def test_export_is_json_serialisable_and_stable():
    """Two builds of the same library produce byte-identical output, so a
    consumer can tell a real change from a re-run."""
    tracks = [_track("t1"), _track("t2", album_id="alb2", album_name="Other")]
    assert json.dumps(_build(tracks)) == json.dumps(_build(tracks))
