"""Save the records owned on Discogs to the Spotify library.

The Discogs collection arrives as ``discogs.json`` in the shared music
directory (written by ``discogs export`` in its own repo); nothing here
talks to Discogs. Each record is resolved to a Spotify album by an
artist+title search, and only an *unambiguous* hit is accepted — a
false match saves a stranger's record to a real account, while a missed
one only shows up as "no match" in the table. The matching rules are
copied from ``rym/match.py`` rather than imported: the repos are coupled
only by the interchange files.

Saving is idempotent (already-saved albums are skipped) and batched.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tekore

DISCOGS_SCHEMA = "discogs/1"
BATCH = 20


class LibraryFileError(Exception):
    """``discogs.json`` is missing or is not something this reader owns."""


@dataclass(frozen=True)
class OwnedRecord:
    artist: str
    title: str
    year: int | None
    release_id: int


@dataclass(frozen=True)
class Match:
    record: OwnedRecord
    album_id: str | None
    album_name: str | None


def read_discogs_collection(path: Path) -> list[OwnedRecord]:
    """Parse the collection out of a ``discogs/1`` file.

    The wantlist is ignored on purpose: it is what the user does *not*
    own, and saving it would make the library say otherwise.
    """
    if not path.exists():
        raise LibraryFileError(f"{path} does not exist — run `discogs export` first.")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LibraryFileError(f"{path} is not valid JSON: {exc}") from exc
    schema = document.get("schema") if isinstance(document, dict) else None
    if schema != DISCOGS_SCHEMA:
        raise LibraryFileError(
            f"{path} has schema {schema!r}; this command reads {DISCOGS_SCHEMA!r} "
            "(produced by `discogs export`)."
        )

    records = []
    for item in document.get("collection") or []:
        artists = item.get("artists") or []
        if not artists or not item.get("title"):
            continue
        records.append(
            OwnedRecord(
                artist=str(artists[0]),
                title=str(item["title"]),
                year=item.get("year"),
                release_id=int(item["release_id"]),
            )
        )
    return records


# -- matching (copied from rym/match.py; keep the three copies identical) --

_EDITION = re.compile(
    r"\s*[\(\[-]\s*("
    r"deluxe|expanded|remaster|remastered|reissue|anniversary|special|"
    r"bonus|deluxe edition|super deluxe|legacy|collector|mono|stereo|"
    r"explicit|clean|international|japan|uk|us"
    r")\b.*$",
    re.IGNORECASE,
)
_BRACKETS = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]\s*$")
_PUNCT = re.compile(r"[^\w\s]")
_SPACE = re.compile(r"\s+")


def normalise(text: str) -> str:
    """Strip edition noise, punctuation and casing from a title or artist."""
    cleaned = _EDITION.sub("", text)
    cleaned = _BRACKETS.sub("", cleaned)
    cleaned = _PUNCT.sub(" ", cleaned)
    return _SPACE.sub(" ", cleaned).strip().casefold()


def strip_article(name: str) -> str:
    """Catalogues file "The Beatles" under Beatles about as often as not."""
    stripped = normalise(name)
    for article in ("the ", "a ", "an "):
        if stripped.startswith(article):
            return stripped[len(article) :]
    return stripped


def key(artist: str, title: str) -> tuple[str, str]:
    return (strip_article(artist), normalise(title))


def _is_plain(title: str) -> bool:
    """True when *title* carries no edition noise — the canonical release."""
    return _SPACE.sub(" ", _PUNCT.sub(" ", title)).strip().casefold() == normalise(title)


async def find_album(sp: tekore.Spotify, record: OwnedRecord) -> Match:
    """Resolve *record* to one Spotify album, or to none.

    Hits are kept only when the artist+title pair matches after
    normalisation. Spotify then routinely lists one record several
    times — plain, Deluxe, Super Deluxe, 30th Anniversary — and those
    are editions of the record the user owns, not rival candidates: the
    plain title is taken as canonical. What is still refused, and
    reported rather than guessed: no hit, several hits with no plain
    edition among them, or two plain editions (a reissue under a second
    artist entry, say).
    """
    query = f"album:{normalise(record.title)} artist:{record.artist}"
    (albums,) = await sp.search(query, types=("album",), limit=5)
    wanted = key(record.artist, record.title)
    hits = [a for a in albums.items if a.artists and key(a.artists[0].name, a.name) == wanted]
    if len(hits) > 1:
        hits = [a for a in hits if _is_plain(a.name)]
    if len(hits) != 1:
        return Match(record, None, None)
    return Match(record, hits[0].id, hits[0].name)


def _batches(ids: list[str]) -> list[list[str]]:
    return [ids[i : i + BATCH] for i in range(0, len(ids), BATCH)]


async def saved_status(sp: tekore.Spotify, album_ids: list[str]) -> dict[str, bool]:
    """Which of *album_ids* are already in the library."""
    status: dict[str, bool] = {}
    for batch in _batches(album_ids):
        flags: list[bool] = await sp.saved_albums_contains(batch)
        status.update(zip(batch, flags, strict=True))
    return status


async def save_albums(sp: tekore.Spotify, album_ids: list[str]) -> int:
    """Save *album_ids* to the library, ``BATCH`` per request; returns the count."""
    for batch in _batches(album_ids):
        await sp.saved_albums_add(batch)
    return len(album_ids)


def plan(matches: list[Match], status: dict[str, bool]) -> dict[str, list[Any]]:
    """Split matches into what a dry run prints and ``--apply`` acts on."""
    to_save = [m for m in matches if m.album_id and not status.get(m.album_id)]
    already = [m for m in matches if m.album_id and status.get(m.album_id)]
    unmatched = [m for m in matches if not m.album_id]
    return {"to_save": to_save, "already": already, "unmatched": unmatched}
