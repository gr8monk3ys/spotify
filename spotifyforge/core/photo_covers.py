"""Photo cover art, matched to each playlist's vibe.

Generated gradients make the catalogue read as one collection; a photo
makes a playlist read as a *choice*. Photos come from Pexels — a source
whose license is built for reuse — never from scraped boards: a cover
upload republishes the image, so the source has to license that.

The selection brief is deliberate: abstract, art-leaning images, never
people — a face on a playlist cover reads as a claim about who the
music is by or for, and a portrait grid looks like a follower farm.
Queries lean abstract, results whose alt text mentions a person are
skipped, and every photo is used at most once across the whole account
(collisions were guaranteed when many niche genres shared one fallback
scene over a 15-photo pool — the pool is now 80 deep and picks are
globally unique).

Every pick is pinned in a sidecar (photo id, photographer, page URL) so
re-runs are stable and attribution stays recoverable. Pexels' free tier
allows ~200 searches/hour — searches are cached per query within a run,
and a rate-limited run saves its progress and says to come back rather
than failing.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import re
from typing import TYPE_CHECKING, Any

import httpx
from PIL import Image, ImageEnhance

from spotifyforge.core.covers import encode_jpeg

if TYPE_CHECKING:
    from pathlib import Path

    from tekore import Spotify

logger = logging.getLogger(__name__)

_API = "https://api.pexels.com/v1/search"
_PER_PAGE = 80  # Pexels max; a deep pool is what makes unique picks possible
_SIZE = 640
_UPLOAD_DELAY = 1.0  # one write per second, same pacing as forge

# Alt text that signals a person in frame. Empty alt is allowed — it
# proves nothing either way.
_PEOPLE = re.compile(
    r"\b(person|people|man|men|woman|women|girl|boy|portrait|model|face|"
    r"couple|bride|groom|lady|guy|child|kid|selfie|crowd)\b",
    re.IGNORECASE,
)

# When a niche genre finds nothing, fall back to an abstract scene that
# evokes its family. First match wins; order goes specific → broad.
_SCENES: list[tuple[tuple[str, ...], str]] = [
    (("jazz", "bop", "swing", "bossa"), "smoke abstract dark blue"),
    (("classical", "opera", "piano", "baroque"), "marble sculpture texture detail"),
    (("techno", "house", "rave", "edm", "electro", "club"), "neon light long exposure abstract"),
    (("ambient", "drone", "new age"), "fog minimal landscape abstract"),
    (("metal", "hardcore", "punk", "grind"), "dark concrete texture grain"),
    (("hip hop", "rap", "trap", "drill", "grime"), "city lights bokeh night abstract"),
    (("folk", "country", "americana", "bluegrass"), "golden field texture film grain"),
    (
        ("latin", "reggaeton", "dembow", "salsa", "cumbia", "bolero"),
        "tropical leaves shadow abstract",
    ),
    (("soul", "r&b", "funk", "disco"), "warm light gradient abstract"),
    (("psych", "shoegaze", "dream"), "double exposure light leak abstract"),
    (("rock", "indie", "garage"), "red stage light smoke abstract"),
    (("lo-fi", "beats", "chill"), "rain on window bokeh"),
    (("reggae", "dub", "ska"), "palm shadow sunset film"),
    (("pop",), "color gradient light abstract"),
]
# The no-match tail spreads across several pools instead of piling onto
# one query, which is what caused the repeats.
_DEFAULT_SCENES = [
    "abstract painting texture",
    "oil paint macro abstract",
    "ink in water abstract",
    "light leak film abstract",
    "brutalist architecture detail",
    "colored smoke abstract",
    "prism light refraction abstract",
    "textured paper minimal art",
]


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


def queries_for(title: str, vibe: str) -> list[str]:
    """The search ladder for one playlist: vibe-as-art, family scene,
    then a hash-spread generic art pool."""
    text = vibe.lower()
    queries = [f"{vibe} abstract"]
    for keywords, query in _SCENES:
        if any(k in text for k in keywords):
            queries.append(query)
            break
    tail = _DEFAULT_SCENES[hashlib.sha256(title.encode()).digest()[1] % len(_DEFAULT_SCENES)]
    queries.append(tail)
    return list(dict.fromkeys(queries))


class RateLimitedError(Exception):
    """Pexels said stop for now; progress so far is saved."""


class PexelsSource:
    """Minimal Pexels client. Searches count against the hourly quota
    (and are cached per query for the life of this source); image
    downloads are CDN fetches and do not."""

    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._key = api_key
        self._cache: dict[str, list[dict[str, Any]]] = {}

    async def close(self) -> None:
        await self._client.aclose()

    async def search(self, query: str) -> list[dict[str, Any]]:
        if query in self._cache:
            return self._cache[query]
        response = await self._client.get(
            _API,
            params={"query": query, "per_page": _PER_PAGE, "orientation": "square"},
            headers={"Authorization": self._key},
        )
        if response.status_code == 429:
            raise RateLimitedError
        response.raise_for_status()
        photos = list(response.json().get("photos") or [])
        self._cache[query] = photos
        return photos

    async def fetch(self, url: str) -> bytes:
        response = await self._client.get(url)
        response.raise_for_status()
        return response.content


def _eligible(photo: dict[str, Any], used: set[Any]) -> bool:
    if photo.get("id") in used:
        return False
    return not _PEOPLE.search(photo.get("alt") or "")


async def choose_photo(
    source: PexelsSource, title: str, vibe: str, used: set[Any]
) -> dict[str, Any] | None:
    """A stable, account-unique, person-free pick for one playlist.

    Starts at a title-hashed offset into each query's pool and takes the
    first eligible photo from there, so picks stay stable while never
    repeating across playlists.
    """
    for query in queries_for(title, vibe):
        photos = await source.search(query)
        if not photos:
            continue
        start = hashlib.sha256(title.encode()).digest()[0] % len(photos)
        for offset in range(len(photos)):
            photo = photos[(start + offset) % len(photos)]
            if not _eligible(photo, used):
                continue
            return {
                "photo_id": photo.get("id"),
                "photographer": photo.get("photographer", ""),
                "page": photo.get("url", ""),
                "alt": photo.get("alt", ""),
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
    Uniqueness is account-wide: a photo any pick already uses is never
    chosen again. Returns ``(covered, failed, rate_limited)``; hitting
    the quota saves progress and returns early instead of raising.
    """
    import base64

    picks = load_picks(path)
    used = {pick.get("photo_id") for pick in picks.values()}
    covered: list[str] = []
    failed: list[str] = []
    rate_limited = False

    for title, playlist_id, vibe in targets:
        if title in picks and not overwrite:
            continue
        try:
            pick = await choose_photo(source, title, vibe, used)
            if pick is None or not pick["src"]:
                logger.info("No usable photo for %r (%s)", title, vibe)
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
        used.add(pick["photo_id"])
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
