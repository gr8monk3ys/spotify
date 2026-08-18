"""Photo cover art, matched to each playlist's vibe.

Generated gradients make the catalogue read as one collection; a photo
makes a playlist read as a *choice*. Photos come from Pexels — a source
whose license is built for reuse — never from scraped boards: a cover
upload republishes the image, so the source has to license that.

Every pick is pinned in a sidecar (photo id, photographer, page URL) so
re-runs are stable and attribution stays recoverable. The search starts
from the playlist's own genre or name and falls back to a scene that
evokes its family; the pick among results is hashed from the title, so
two playlists sharing a query still get different photos. Pexels'
free tier allows ~200 requests/hour — a full catalogue takes more, so a
rate-limited run saves its progress and says to come back, rather than
failing.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
from typing import TYPE_CHECKING, Any

import httpx
from PIL import Image, ImageEnhance

from spotifyforge.core.covers import encode_jpeg

if TYPE_CHECKING:
    from pathlib import Path

    from tekore import Spotify

logger = logging.getLogger(__name__)

_API = "https://api.pexels.com/v1/search"
_PER_PAGE = 15
_SIZE = 640
_UPLOAD_DELAY = 1.0  # one write per second, same pacing as forge

# When a niche genre finds no photos, fall back to a scene that evokes
# its family. First match wins; order goes specific → broad.
_SCENES: list[tuple[tuple[str, ...], str]] = [
    (("jazz", "bop", "swing", "bossa"), "saxophone smoke dark stage"),
    (("classical", "opera", "piano", "baroque"), "marble statue museum light"),
    (("techno", "house", "rave", "edm", "electro", "club"), "neon night club haze"),
    (("ambient", "drone", "new age"), "fog landscape minimal"),
    (("metal", "hardcore", "punk", "grind"), "grainy concert crowd dark"),
    (("hip hop", "rap", "trap", "drill", "grime"), "city street night neon"),
    (("folk", "country", "americana", "bluegrass"), "field golden hour film"),
    (("latin", "reggaeton", "dembow", "salsa", "cumbia", "bolero"), "tropical night warm"),
    (("soul", "r&b", "funk", "disco"), "warm light retro interior"),
    (("psych", "shoegaze", "dream"), "double exposure light leak"),
    (("rock", "indie", "garage"), "concert stage silhouette"),
    (("lo-fi", "beats", "chill"), "rainy window desk warm"),
    (("reggae", "dub", "ska"), "sunset palm film grain"),
    (("pop",), "studio color light"),
]
_DEFAULT_SCENE = "abstract film grain texture"


def picks_path() -> Path:
    """Where photo picks live: ``<db_path parent>/photo_covers.json``."""
    from spotifyforge.config import sidecar_path

    return sidecar_path("photo_covers.json")


def load_picks(path: Path | None = None) -> dict[str, dict[str, Any]]:
    target = path or picks_path()
    if not target.exists():
        return {}
    picks: dict[str, dict[str, Any]] = json.loads(target.read_text(encoding="utf-8"))
    return picks


def save_picks(picks: dict[str, dict[str, Any]], path: Path | None = None) -> Path:
    from spotifyforge.config import write_json_atomic

    target = path or picks_path()
    write_json_atomic(target, picks)
    return target


def scene_query(vibe: str) -> str:
    """The fallback scene for a genre or playlist name."""
    text = vibe.lower()
    for keywords, query in _SCENES:
        if any(k in text for k in keywords):
            return query
    return _DEFAULT_SCENE


class RateLimitedError(Exception):
    """Pexels said stop for now; progress so far is saved."""


class PexelsSource:
    """Minimal Pexels client. Searches count against the hourly quota;
    image downloads are CDN fetches and do not."""

    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._key = api_key

    async def close(self) -> None:
        await self._client.aclose()

    async def search(self, query: str) -> list[dict[str, Any]]:
        response = await self._client.get(
            _API,
            params={"query": query, "per_page": _PER_PAGE, "orientation": "square"},
            headers={"Authorization": self._key},
        )
        if response.status_code == 429:
            raise RateLimitedError
        response.raise_for_status()
        return list(response.json().get("photos") or [])

    async def fetch(self, url: str) -> bytes:
        response = await self._client.get(url)
        response.raise_for_status()
        return response.content


async def choose_photo(source: PexelsSource, title: str, vibe: str) -> dict[str, Any] | None:
    """A stable photo pick for one playlist, or ``None`` if none found."""
    queries = [vibe]
    fallback = scene_query(vibe)
    if fallback != vibe:
        queries.append(fallback)
    for query in queries:
        photos = await source.search(query)
        if not photos:
            continue
        index = hashlib.sha256(title.encode()).digest()[0] % len(photos)
        photo = photos[index]
        return {
            "photo_id": photo.get("id"),
            "photographer": photo.get("photographer", ""),
            "page": photo.get("url", ""),
            "src": (photo.get("src") or {}).get("large2x")
            or (photo.get("src") or {}).get("large")
            or (photo.get("src") or {}).get("original"),
            "query": query,
        }
    return None


def to_cover(data: bytes) -> bytes:
    """Centre-crop to a square, resize, and grade for cohesion.

    The slight desaturation is what keeps 280 unrelated photographs
    reading as one account rather than a mood board.
    """
    image = Image.open(io.BytesIO(data)).convert("RGB")
    side = min(image.size)
    left = (image.width - side) // 2
    top = (image.height - side) // 2
    image = image.crop((left, top, left + side, top + side)).resize(
        (_SIZE, _SIZE), Image.Resampling.LANCZOS
    )
    image = ImageEnhance.Color(image).enhance(0.82)
    image = ImageEnhance.Contrast(image).enhance(1.05)
    return encode_jpeg(image)


async def apply_photo_covers(
    spotify: Spotify,
    targets: list[tuple[str, str, str]],
    source: PexelsSource,
    overwrite: bool = False,
    path: Path | None = None,
    delay: float = _UPLOAD_DELAY,
) -> tuple[list[str], list[str], bool]:
    """Give each ``(title, playlist_id, vibe)`` target a photo cover.

    Titles already in the picks sidecar are skipped unless *overwrite*,
    which is what makes the run resumable across Pexels' hourly quota.
    Returns ``(covered, failed, rate_limited)``; hitting the quota saves
    progress and returns early instead of raising.
    """
    import base64

    picks = load_picks(path)
    covered: list[str] = []
    failed: list[str] = []
    rate_limited = False

    for title, playlist_id, vibe in targets:
        if title in picks and not overwrite:
            continue
        try:
            pick = await choose_photo(source, title, vibe)
            if pick is None or not pick["src"]:
                logger.info("No photo found for %r (%s)", title, vibe)
                failed.append(title)
                continue
            payload = base64.b64encode(to_cover(await source.fetch(pick["src"]))).decode("ascii")
            await spotify.playlist_cover_image_upload(playlist_id, payload)
        except RateLimitedError:
            rate_limited = True
            break
        except Exception as exc:  # noqa: BLE001 — one bad photo must not kill the run
            logger.warning("Could not photo-cover %r: %s", title, exc)
            failed.append(title)
            continue
        picks[title] = pick
        covered.append(title)
        save_picks(picks, path)
        if delay:
            await asyncio.sleep(delay)

    save_picks(picks, path)
    logger.info(
        "Photo-covered %d, failed %d%s",
        len(covered),
        len(failed),
        " (rate limited; resume later)" if rate_limited else "",
    )
    return covered, failed, rate_limited
