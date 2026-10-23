"""Tests for the preview tempo estimator.

Synthetic click tracks stand in for music: the beat is known exactly, so
an estimate can be checked to the BPM. Encoded as WAV with the standard
library, since libsndfile reads WAV the same way it reads the MP3s
Deezer serves.
"""

from __future__ import annotations

import builtins
import io
import wave

import pytest

from spotifyforge.core import tempo

np = pytest.importorskip("numpy")
pytest.importorskip("soundfile")

RATE = 22050


def _clicks(bpm: float, seconds: float = 12.0, rate: int = RATE):
    """A click track: a short decaying noise burst on every beat."""
    rng = np.random.default_rng(0)
    samples = np.zeros(int(seconds * rate), dtype=np.float32)
    burst = (rng.standard_normal(400) * np.exp(-np.arange(400) / 60)).astype(np.float32)
    period = 60.0 / bpm * rate
    for start in np.arange(0, len(samples) - len(burst), period).astype(int):
        samples[start : start + len(burst)] += burst
    return samples


def _wav(samples, rate: int = RATE) -> bytes:
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(pcm)
    return buffer.getvalue()


def _id3(payload: bytes = b"", footer: bool = False) -> bytes:
    """An ID3v2 header announcing *payload* bytes of tag, size syncsafe."""
    size = len(payload)
    syncsafe = bytes((size >> shift) & 0x7F for shift in (21, 14, 7, 0))
    flags = 0x10 if footer else 0
    return b"ID3\x04\x00" + bytes([flags]) + syncsafe + payload + (b"3DI" + b"\0" * 7) * footer


@pytest.mark.parametrize("bpm", [90.0, 120.0, 140.0])
def test_estimate_finds_the_beat_of_a_click_track(bpm):
    estimate = tempo.estimate_bpm(_clicks(bpm), RATE)
    assert estimate == pytest.approx(bpm, abs=3)


def test_a_slow_beat_is_reported_in_the_octave_nearest_the_prior():
    # 50 BPM is outside the searched range; its double is the honest answer.
    estimate = tempo.estimate_bpm(_clicks(50.0, seconds=20), RATE)
    assert estimate == pytest.approx(100.0, abs=3)


def test_silence_has_no_tempo():
    assert tempo.estimate_bpm(np.zeros(RATE * 10, dtype=np.float32), RATE) is None


def test_a_clip_too_short_to_hear_a_beat_has_no_tempo():
    assert tempo.estimate_bpm(_clicks(120.0, seconds=2), RATE) is None


def test_estimate_from_audio_decodes_through_a_leading_id3_tag():
    audio = _id3(b"\0" * 32) + _wav(_clicks(120.0))
    assert tempo.estimate_from_audio(audio) == pytest.approx(120.0, abs=3)


def test_undecodable_audio_is_no_tempo_not_an_error():
    assert tempo.estimate_from_audio(b"<html>not audio</html>") is None


def test_strip_id3_skips_header_tag_and_footer():
    body = b"RIFF-the-audio"
    assert tempo._strip_id3(_id3(b"x" * 300) + body) == body  # size spans two syncsafe bytes
    assert tempo._strip_id3(_id3(b"x" * 5, footer=True) + body) == body
    assert tempo._strip_id3(body) == body


def test_available_is_false_without_the_extra(monkeypatch):
    real_import = builtins.__import__

    def no_soundfile(name, *args, **kwargs):
        if name == "soundfile":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_soundfile)
    assert tempo.available() is False
