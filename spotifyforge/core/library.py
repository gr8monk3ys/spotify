"""Save the records owned on Discogs to the Spotify library.

The Discogs collection arrives as ``discogs.json`` in the shared music
directory (written by ``discogs export`` in its own repo); nothing here
talks to Discogs. Each record is resolved to a Spotify album by an
artist+title search, and only an *unambiguous* hit is accepted — a
false match saves a stranger's record to a real account, while a missed
one only shows up as "no match" in the table. The matching rules come
from ``media_core.names``, which is where the three hand-kept copies of
them ended up; only ``_is_plain`` below is this repo's own.

Saving is idempotent (already-saved albums are skipped) and batched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tekore
from media_core.export import ExportError, read_export
from media_core.names import key, normalise, strip_article

DISCOGS_SCHEMA = "discogs/1"
BATCH = 20

# `normalise`, `strip_article` and `key` are part of this module's surface even
# though they now live in media_core: callers and tests import them from here.
__all__ = [
    "DISCOGS_SCHEMA",
    "LibraryFileError",
    "Match",
    "OwnedRecord",
    "find_album",
    "key",
    "normalise",
    "plan",
    "read_discogs_collection",
    "save_albums",
    "saved_status",
    "strip_article",
]


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
        # Checked here rather than left to read_export: the useful part of this
        # message is which command in which repo produces the file, and that is
        # something only this end of the contract knows.
        raise LibraryFileError(f"{path} does not exist — run `discogs export` first.")
    try:
        document = read_export(path, DISCOGS_SCHEMA)
    except ExportError as exc:
        raise LibraryFileError(f"{exc} It is produced by `discogs export`.") from exc

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


# -- matching -------------------------------------------------------------
#
# ``normalise``, ``strip_article`` and ``key`` are re-exported from
# ``media_core.names`` rather than defined here. They used to be a verbatim copy
# kept in step with two other repos by hand, pinned by the golden corpus in
# ``tests/fixtures/name_normalisation_corpus.json``; the corpus still holds them
# to exactly the behaviour it did then.

# Only used by _is_plain, which is this repo's alone: the shared normaliser
# strips edition noise, and the question here is whether there was any.
_PUNCT = re.compile(r"[^\w\s]")
_SPACE = re.compile(r"\s+")


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
