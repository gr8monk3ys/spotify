"""Generate playlist cover art.

An account whose playlists all carry Spotify's default four-track mosaic
reads as automated. Distinct, deliberate covers read as curated, and a
browsing stranger decides which one you are in about a second — so this
is the honest half of "growth": make the work look like work.

Covers are derived from the playlist's own genre, so they are stable
across runs (the same genre always yields the same artwork) and no two
genres collide by accident. Nothing here talks to Spotify; the upload
lives in :mod:`spotifyforge.core.curation`.
"""

from __future__ import annotations

import base64
import colorsys
import hashlib
import io
import math
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw, ImageFont

if TYPE_CHECKING:
    from spotifyforge.core.curation import PlaylistSpec

# Spotify accepts a base64 JPEG up to 256 KB. 640px is its display size
# for a playlist tile; larger only costs payload.
_SIZE = 640
_MAX_BYTES = 256 * 1024
_JPEG_QUALITY = 88


def _palette(seed: str) -> tuple[tuple[int, int, int], tuple[int, int, int], tuple[int, int, int]]:
    """Two backdrop tones and an ink colour, derived from *seed*.

    Hue comes from a hash so a genre keeps its identity run to run.
    Saturation and lightness stay in a deliberately narrow band: the set
    should look like one collection, not 221 unrelated images.
    """
    digest = hashlib.sha256(seed.encode()).digest()
    hue = digest[0] / 255.0
    drift = 0.06 + (digest[1] / 255.0) * 0.10  # how far the gradient travels
    dark = colorsys.hls_to_rgb(hue, 0.16 + (digest[2] / 255.0) * 0.08, 0.55)
    light = colorsys.hls_to_rgb((hue + drift) % 1.0, 0.42, 0.62)
    ink = (245, 245, 242)
    return (_rgb(dark), _rgb(light), ink)


def _rgb(hls: tuple[float, float, float]) -> tuple[int, int, int]:
    r, g, b = hls
    return (int(r * 255), int(g * 255), int(b * 255))


def _gradient(size: int, dark: tuple[int, int, int], light: tuple[int, int, int]) -> Image.Image:
    """A smooth diagonal wash between two tones."""
    base = Image.new("RGB", (size, size), dark)
    overlay = Image.new("RGB", (size, size), light)
    mask = Image.new("L", (size, size))
    pixels = mask.load()
    assert pixels is not None
    for y in range(size):
        for x in range(0, size, 4):
            value = int(255 * (x + y) / (2 * size))
            for dx in range(4):
                if x + dx < size:
                    pixels[x + dx, y] = value
    base.paste(overlay, (0, 0), mask)
    return base


def _rings(image: Image.Image, seed: str, ink: tuple[int, int, int]) -> None:
    """Faint concentric arcs — a record groove, never the focal point."""
    digest = hashlib.sha256(("rings" + seed).encode()).digest()
    draw = ImageDraw.Draw(image, "RGBA")
    cx = int(_SIZE * (0.55 + (digest[0] / 255.0) * 0.3))
    cy = int(_SIZE * (0.45 + (digest[1] / 255.0) * 0.3))
    for i in range(5, 0, -1):
        radius = int(_SIZE * (0.10 + i * 0.085))
        draw.ellipse(
            (cx - radius, cy - radius, cx + radius, cy + radius),
            outline=(*ink, 26),
            width=max(1, radius // 40),
        )


def _wrap(
    text: str,
    font: ImageFont.ImageFont | ImageFont.FreeTypeFont,
    max_width: int,
    draw: ImageDraw.ImageDraw,
) -> list[str]:
    """Break *text* into lines that fit *max_width*."""
    words, lines, current = text.split(), [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    return ImageFont.load_default(size=size)


def render_cover(title: str, subject: str) -> bytes:
    """Render one cover as JPEG bytes.

    *subject* is the genre, which drives the colour and is set large;
    *title* is the playlist name, set small beneath it as a caption.
    """
    dark, light, ink = _palette(subject)
    image = _gradient(_SIZE, dark, light)
    _rings(image, subject, ink)
    draw = ImageDraw.Draw(image)

    margin = int(_SIZE * 0.09)
    max_width = _SIZE - 2 * margin

    # Shrink the headline until it fits in at most three lines *and* no
    # line overruns the margin — a long single word ("EXPERIMENTAL")
    # cannot be wrapped, so only a smaller size will contain it.
    for point in (96, 84, 72, 62, 54, 46, 40, 34, 28, 24):
        font = _font(point)
        lines = _wrap(subject.upper(), font, max_width, draw)
        widest = max((draw.textlength(line, font=font) for line in lines), default=0)
        if len(lines) <= 3 and widest <= max_width:
            break
    line_height = int(point * 1.12)
    block = line_height * len(lines)
    y = (_SIZE - block) // 2 - int(_SIZE * 0.04)

    for line in lines:
        draw.text((margin, y), line, font=font, fill=ink)
        y += line_height

    draw.line((margin, y + 14, margin + int(max_width * 0.28), y + 14), fill=(*ink, 200), width=3)

    caption_font = _font(24)
    caption = title if draw.textlength(title, font=caption_font) <= max_width else subject
    draw.text((margin, y + 34), caption, font=caption_font, fill=(*ink, 190))

    return encode_jpeg(image)


def encode_jpeg(image: Image.Image) -> bytes:
    """JPEG-encode, stepping quality down until Spotify's limit is met."""
    quality = _JPEG_QUALITY
    while True:
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=quality, optimize=True)
        data = buffer.getvalue()
        # base64 inflates by 4/3, and that encoded form is what is sent.
        if math.ceil(len(data) / 3) * 4 <= _MAX_BYTES or quality <= 30:
            return data
        quality -= 10


def cover_payload(spec: PlaylistSpec) -> str:
    """The base64 JPEG Spotify's cover-upload endpoint expects."""
    return base64.b64encode(render_cover(spec.title, spec.genre_label)).decode("ascii")
