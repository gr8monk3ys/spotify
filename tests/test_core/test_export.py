"""Tests for the interchange export (core/export.py).

The shape asserted here is a contract two other repos read, so these
tests are deliberately about structure and ordering, not just values.
"""

from __future__ import annotations

import json

from spotifyforge.core.audio_features import AudioFeature
from spotifyforge.core.curation import CurationTrack
from spotifyforge.core.export import (
    SCHEMA,
    build_library_export,
    write_export,
)


def _track(
    track_id: str,
    *,
    album_id: str | None = "alb1",
    album_name: str = "Kobaïa",
    total: int | None = 10,
    artists: tuple[tuple[str, str], ...] = (("a1", "Magma"),),
) -> CurationTrack:
    return CurationTrack(
        id=track_id,
        uri=f"spotify:track:{track_id}",
        name=f"Song {track_id}",
        artist_ids=tuple(a for a, _ in artists),
        artist_names=tuple(n for _, n in artists),
        release_year=1970,
        popularity=20,
        isrc=f"ISRC{track_id.upper()}",
        genres=("zeuhl",),
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


def test_written_file_is_plain_json_a_consumer_can_read(tmp_path):
    """Consumers live in other repos and parse this with stdlib json —
    nothing here should be needed to read it back."""
    path = tmp_path / "music-library.json"
    assert write_export(_build([_track("t1")]), path) == path

    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["schema"] == SCHEMA
    assert document["albums"][0]["spotify_album_id"] == "alb1"


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


def test_discoveries_survive_a_mix_of_dated_and_undated_pins():
    """Pin keys carry a decade or None, and sorting them together
    compared None against an int — the whole export died before it
    returned. Real pins only started mixing once expand ran on
    decade-split genres."""
    from spotifyforge.core.export import build_library_export

    pins = {
        ("gabber", 1990): [_track("p1")],
        ("gabber", None): [_track("p2")],
        ("zeuhl", 1970): [_track("p3")],
    }

    payload = build_library_export([], {}, pins, "gr8monk3ys", "2026-08-19T00:00:00Z")

    assert [(d["genre"], d["decade"]) for d in payload["discoveries"]] == [
        ("gabber", None),
        ("gabber", 1990),
        ("zeuhl", 1970),
    ]


def test_export_path_honours_music_dir(monkeypatch, tmp_path):
    """Three repos read this file from one shared directory; the plain
    MUSIC_DIR variable (no SPOTIFYFORGE_ prefix) is what they all honour."""
    from spotifyforge.config import Settings
    from spotifyforge.core import export

    monkeypatch.setenv("MUSIC_DIR", str(tmp_path / "music"))
    monkeypatch.setattr("spotifyforge.config.settings", Settings())

    assert export.export_path() == tmp_path / "music" / "music-library.json"


def test_music_dir_defaults_to_dot_music(monkeypatch):
    from pathlib import Path

    from spotifyforge.config import Settings

    monkeypatch.delenv("MUSIC_DIR", raising=False)
    monkeypatch.delenv("SPOTIFYFORGE_MUSIC_DIR", raising=False)
    assert Settings().music_dir == Path.home() / ".music"


def test_write_export_keeps_a_legacy_sidecar_for_old_readers(monkeypatch, tmp_path):
    """Consumers still on ~/.spotifyforge/music-library.json keep working
    for one release."""
    from spotifyforge.config import Settings
    from spotifyforge.core import export

    monkeypatch.setenv("MUSIC_DIR", str(tmp_path / "music"))
    fresh = Settings()
    fresh.db_path = tmp_path / "cfg" / "app.db"
    monkeypatch.setattr("spotifyforge.config.settings", fresh)

    target = export.write_export(_build([_track("t1")]))

    assert target == tmp_path / "music" / "music-library.json"
    assert (tmp_path / "cfg" / "music-library.json").exists()
