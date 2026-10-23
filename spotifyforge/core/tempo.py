"""Tempo estimated from audio, for recordings no database has a BPM for.

Deezer stopped filling in ``bpm`` for much of its catalogue (it answers
0) and AcousticBrainz stopped collecting in 2022, so a recent track can
be missing from both. Deezer still serves a 30-second preview of nearly
everything, and 30 seconds is enough to hear the beat.

The method is the standard one, kept small enough to read: a spectral
flux onset envelope, its autocorrelation, and a pick of the strongest
periodicity in a plausible tempo range. Two refinements decide most of
the accuracy:

* **Harmonic summing.** A beat at period *p* also correlates at 2p, 3p
  and 4p. Scoring each candidate by the sum over its multiples rewards
  the true beat over a lone accent pattern.
* **A tempo prior.** Autocorrelation cannot tell 85 BPM from 170 BPM on
  its own — both are "right" about the same pulse. A log-normal prior
  centred on :data:`_PRIOR_CENTRE_BPM` settles the octave the way most
  BPM databases do; see that constant for how it was chosen.

numpy and soundfile (which bundles libsndfile, an MP3 decoder) come from
the optional ``tempo`` extra. Without them :func:`available` is false
and the preview provider sits out; nothing else in the package imports
this module's dependencies.
"""

from __future__ import annotations

import io
import logging
from typing import Any

# A numpy array. Spelled as Any so that type-checking this package never
# depends on which numpy, if any, is installed.
Samples = Any

logger = logging.getLogger(__name__)

# Analysis frames: 2048-sample windows every 10 ms at 44.1 kHz (the rate
# Deezer previews are encoded at; other rates are scaled to match).
_N_FFT = 2048
_FRAME_RATE = 100.0
_MAX_ONSET_HZ = 11000

# The periods searched. Below 60 a "beat" is usually a bar; above 200 it
# is usually hi-hats.
_MIN_BPM = 60.0
_MAX_BPM = 200.0

# Log-normal tempo prior: centre and width (in octaves). The centre is
# where the half/double ambiguity is resolved. Checked against 400 liked
# songs whose BPM Deezer or AcousticBrainz already report: centres from
# 120 to 130 and a one-octave width tie for best (within noise), and at
# 120 the remaining octave errors split evenly between half and double,
# so the prior does not lean either way. Narrower widths trade octave
# errors for 2/3-time errors, which no sequencing can forgive.
_PRIOR_CENTRE_BPM = 120.0
_PRIOR_WIDTH_OCTAVES = 1.0

# Multiples of the beat period summed when scoring a candidate.
_HARMONICS = (1, 2, 3, 4)


def available() -> bool:
    """Is the optional ``tempo`` extra installed?"""
    try:
        import numpy  # noqa: F401
        import soundfile  # noqa: F401
    except (ImportError, OSError):
        # soundfile raises OSError when its bundled libsndfile cannot load.
        return False
    return True


def _strip_id3(audio: bytes) -> bytes:
    """Drop a leading ID3v2 tag.

    Deezer's previews open with an empty one, and libsndfile, reading from
    memory rather than a path, then fails to recognise the stream as MP3.
    The tag holds nothing the analysis needs.
    """
    if len(audio) < 10 or audio[:3] != b"ID3":
        return audio
    size = 0
    for byte in audio[6:10]:  # "syncsafe": seven bits per byte
        size = (size << 7) | (byte & 0x7F)
    footer = 10 if audio[5] & 0x10 else 0
    return audio[10 + size + footer :]


def decode(audio: bytes) -> tuple[Samples, int]:
    """Decode an MP3 (or anything libsndfile reads) to mono samples."""
    import soundfile

    stream = io.BytesIO(_strip_id3(audio))
    samples, rate = soundfile.read(stream, dtype="float32", always_2d=True)
    return samples.mean(axis=1), int(rate)


def onset_envelope(samples: Samples, rate: int) -> Samples:
    """Spectral flux at :data:`_FRAME_RATE` frames per second.

    Log-compressed magnitudes, so a quiet hi-hat counts next to a loud
    kick, and only increases in energy, which is what an onset is.
    """
    import numpy as np

    hop = max(1, round(rate / _FRAME_RATE))
    n_fft = max(256, round(_N_FFT * rate / 44100))
    if len(samples) < n_fft:
        return np.zeros(0)
    frames = np.lib.stride_tricks.sliding_window_view(samples, n_fft)[::hop]
    spectrum = np.abs(np.fft.rfft(frames * np.hanning(n_fft), axis=1))
    # Hi-hats live up to ~11 kHz; above that a lossy preview is mostly
    # encoder noise. (Cutting at 4 kHz cost five points of agreement.)
    spectrum = spectrum[:, : int(_MAX_ONSET_HZ / rate * n_fft) + 1]
    compressed = np.log1p(100.0 * spectrum)
    flux = np.maximum(0.0, np.diff(compressed, axis=0)).sum(axis=1)
    return np.asarray(flux, dtype=np.float64)


def _prior(bpm: Samples) -> Samples:
    import numpy as np

    octaves = np.log2(bpm / _PRIOR_CENTRE_BPM) / _PRIOR_WIDTH_OCTAVES
    return np.asarray(np.exp(-0.5 * octaves**2), dtype=np.float64)


def estimate_bpm(samples: Samples, rate: int) -> float | None:
    """Tempo of *samples* in BPM, or ``None`` when no beat stands out."""
    import numpy as np

    envelope = onset_envelope(np.asarray(samples, dtype=np.float32), rate)
    if len(envelope) < 4 * _FRAME_RATE or not envelope.any():
        return None
    envelope = envelope - envelope.mean()
    size = 1 << int(np.ceil(np.log2(2 * len(envelope))))
    spectrum = np.fft.rfft(envelope, size)
    acf = np.fft.irfft(spectrum * np.conj(spectrum))[: len(envelope)]
    if acf[0] <= 0:
        return None
    acf = acf / acf[0]

    lags = np.arange(
        int(_FRAME_RATE * 60 / _MAX_BPM), int(_FRAME_RATE * 60 / _MIN_BPM) + 1, dtype=np.float64
    )
    score = np.zeros_like(lags)
    for harmonic in _HARMONICS:
        reach = (lags * harmonic).astype(int)
        inside = reach < len(acf)
        score[inside] += np.maximum(0.0, acf[reach[inside]]) / harmonic
    score *= _prior(_FRAME_RATE * 60 / lags)
    best = int(np.argmax(score))
    # No confidence floor beyond "something repeats": on this library,
    # refusing the weakest 10% of answers bought under two points of
    # accuracy, and a rough tempo sequences better than none.
    if score[best] <= 0:
        return None

    # Parabolic interpolation on the raw autocorrelation for sub-frame
    # precision: one frame is ~1 BPM at 100 BPM, ~3 BPM at 180.
    lag = lags[best]
    i = int(lag)
    if 0 < i < len(acf) - 1:
        left, centre, right = acf[i - 1], acf[i], acf[i + 1]
        curvature = left - 2 * centre + right
        if curvature < 0:
            lag = i + 0.5 * (left - right) / curvature
    return float(_FRAME_RATE * 60 / lag)


def estimate_from_audio(audio: bytes) -> float | None:
    """Tempo of an encoded audio clip, or ``None`` if it cannot be read."""
    try:
        samples, rate = decode(audio)
    except Exception as exc:  # noqa: BLE001 - libsndfile raises its own types
        logger.debug("could not decode preview: %s", exc)
        return None
    return estimate_bpm(samples, rate)
