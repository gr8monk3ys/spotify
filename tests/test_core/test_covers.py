"""Tests for generated playlist cover art.

Covers are checked as images — decoded and measured — rather than by
trusting the drawing code, since the defects that matter here (text
overrunning the canvas, a payload Spotify rejects) are only visible in
the output.
"""

from __future__ import annotations

import base64
import io

import pytest
from PIL import Image, ImageDraw

from spotifyforge.core.covers import (
    _MAX_BYTES,
    _SIZE,
    _font,
    _palette,
    _wrap,
    cover_payload,
    render_cover,
)
from spotifyforge.core.curation import PlaylistSpec

GENRES = [
    "zeuhl",
    "experimental hip hop",  # long single word, forces a size reduction
    "rock en español",  # non-ASCII
    "dungeon synth",
    "unclassified",
    "タイム",  # CJK
    "a",  # degenerate
]


def _open(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data))


@pytest.mark.parametrize("genre", GENRES)
def test_cover_is_a_square_jpeg_of_the_expected_size(genre):
    image = _open(render_cover(f"strictly {genre}", genre))
    assert image.format == "JPEG"
    assert image.size == (_SIZE, _SIZE)


@pytest.mark.parametrize("genre", GENRES)
def test_cover_fits_spotifys_upload_limit(genre):
    # Spotify's cap applies to the base64 form, which is ~4/3 the bytes.
    assert len(cover_payload(PlaylistSpec("t", "d", genre, None))) <= _MAX_BYTES


@pytest.mark.parametrize("genre", GENRES)
def test_headline_never_overruns_the_canvas(genre):
    """The bug this pins: "EXPERIMENTAL" ran off the right edge because
    the shrink loop checked line count but not line width."""
    draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    margin = int(_SIZE * 0.09)
    max_width = _SIZE - 2 * margin

    for point in (96, 84, 72, 62, 54, 46, 40, 34, 28, 24):
        font = _font(point)
        lines = _wrap(genre.upper(), font, max_width, draw)
        widest = max((draw.textlength(line, font=font) for line in lines), default=0)
        if len(lines) <= 3 and widest <= max_width:
            break

    assert widest <= max_width
    assert len(lines) <= 3


def test_the_same_genre_always_yields_the_same_artwork():
    # Re-running `curate covers` must not reshuffle the account's look.
    assert render_cover("strictly zeuhl", "zeuhl") == render_cover("strictly zeuhl", "zeuhl")


def test_the_title_does_not_change_the_palette():
    """Colour keys off the genre, so a decade split matches its parent."""
    assert _palette("shoegaze") == _palette("shoegaze")
    a = _open(render_cover("shoegaze // late transmissions", "shoegaze")).getpixel((5, 5))
    b = _open(render_cover("shoegaze // late transmissions ('90s)", "shoegaze")).getpixel((5, 5))
    assert a == b


def test_different_genres_get_different_colours():
    seen = {_palette(g)[0] for g in GENRES}
    assert len(seen) == len(GENRES)


def test_cover_payload_is_valid_base64_jpeg():
    payload = cover_payload(PlaylistSpec("strictly emo", "d", "emo", None))
    decoded = base64.b64decode(payload, validate=True)
    assert _open(decoded).format == "JPEG"
