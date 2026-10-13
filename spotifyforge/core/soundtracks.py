"""A playlist from the soundtracks of the films rated highest on Letterboxd.

The films arrive as ``letterboxd.json`` in the shared movies directory
(written by the letterboxd repo's export); nothing here talks to
Letterboxd. Each favourite is resolved to a soundtrack album by a
Spotify search, and — as with the Discogs matching in
:mod:`spotifyforge.core.library` — only a hit that can be defended is
accepted. A film title is a few common words; a search for "Persona
soundtrack" returns four video-game albums before anything by Bergman.
Three conditions have to hold together:

* the album is named for the film: the name is the film's title followed
  by nothing but soundtrack boilerplate ("Original Motion Picture
  Soundtrack", "Colonna sonora originale"). "Oppenheimer Soundtrack
  (LoFi Edition)" and "(Original Game Soundtrack)" both fail on the
  extra word;
* it says it is a soundtrack or a score at all;
* it came out with the film, in its year or the two after. This is the
  one that costs real matches — a 1950 film whose score was first
  issued in 2002 is refused — and it is also the only thing separating
  Persona (1966) from "Persona ORIGINAL SOUNDTRACK" (2026).

Over 231 films rated 4.5 or higher this matched 79. The ones it leaves
out are listed, not guessed.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from media_core.export import ExportError, read_export
from media_core.names import fold

from spotifyforge.core.curation import _REPLACE_CHUNK, gather_bounded
from spotifyforge.core.playlist_manager import PlaylistManager

if TYPE_CHECKING:
    from tekore import Spotify

logger = logging.getLogger(__name__)

LETTERBOXD_SCHEMA = "letterboxd/1"

# Spotify's search returns at most ten results per request for this app.
_SEARCH_LIMIT = 10

# How long after a film a soundtrack still counts as its own release.
_YEARS_AFTER = 2

# Words that say "this is the film's music". At least one must be present.
# "music" is one because of "Music From The Motion Picture", which is how
# compilation soundtracks (When Harry Met Sally..., Eyes Wide Shut) are titled.
_MARKERS = frozenset({"soundtrack", "score", "ost", "sonora", "originale", "music", "musique"})

# Everything a soundtrack title may contain besides the film's own title.
_BOILERPLATE = _MARKERS | frozenset(
    {
        "a", "album", "bande", "by", "colonna", "composed", "de", "del", "della", "du",
        "edition", "film", "from", "il", "la", "le", "motion", "movie", "original",
        "picture", "the",
    }
)  # fmt: skip

_WORD = re.compile(r"[^\W_]+", re.UNICODE)


class FilmsFileError(Exception):
    """``letterboxd.json`` is missing or is not something this reader owns."""


@dataclass(frozen=True)
class Film:
    title: str
    year: int | None
    rating: float


@dataclass(frozen=True)
class SoundtrackMatch:
    film: Film
    album_id: str | None = None
    album_name: str | None = None
    artist: str | None = None


def read_favourite_films(path: Path, min_rating: float = 4.5) -> list[Film]:
    """Films rated at least *min_rating*, best first, from a ``letterboxd/1`` file."""
    if not path.exists():
        raise FilmsFileError(f"{path} does not exist — run the letterboxd export first.")
    try:
        document = read_export(path, LETTERBOXD_SCHEMA)
    except ExportError as exc:
        raise FilmsFileError(f"{exc} It is produced by the letterboxd export.") from exc

    films = [
        Film(title=str(item["title"]), year=item.get("year"), rating=float(item["rating"]))
        for item in document.get("films") or []
        if item.get("title") and item.get("rating") is not None and item["rating"] >= min_rating
    ]
    # Stable within a rating: the file's own order (most recently watched first).
    films.sort(key=lambda film: -film.rating)
    return films


def _words(text: str) -> list[str]:
    return _WORD.findall(fold(text))


def is_soundtrack_of(album_name: str, album_year: int | None, film: Film) -> bool:
    """Whether *album_name*, released in *album_year*, is *film*'s own soundtrack."""
    title, name = _words(film.title), _words(album_name)
    if not title or name[: len(title)] != title:
        return False
    rest = name[len(title) :]
    if not rest or not _MARKERS.intersection(rest) or not _BOILERPLATE.issuperset(rest):
        return False
    if film.year is None or album_year is None:
        return False
    return 0 <= album_year - film.year <= _YEARS_AFTER


def _year(album: Any) -> int | None:
    released = getattr(album, "release_date", None) or ""
    return int(released[:4]) if released[:4].isdigit() else None


async def find_soundtrack(sp: Spotify, film: Film) -> SoundtrackMatch:
    """Resolve *film* to its soundtrack album, or to none.

    Several albums can pass (the soundtrack and the score, an Italian
    pressing, a one-track single of the theme). Spotify's own ranking
    decides between them, except that the fullest album wins over a
    shorter one of the same name.
    """
    (albums,) = await sp.search(f"{film.title} soundtrack", types=("album",), limit=_SEARCH_LIMIT)
    hits = [a for a in albums.items if is_soundtrack_of(a.name or "", _year(a), film)]
    if not hits:
        return SoundtrackMatch(film)
    best = hits[0]
    for other in hits[1:]:
        same_name = fold(other.name) == fold(best.name)
        if same_name and (other.total_tracks or 0) > (best.total_tracks or 0):
            best = other
    return SoundtrackMatch(
        film=film,
        album_id=best.id,
        album_name=best.name,
        artist=best.artists[0].name if best.artists else None,
    )


async def find_soundtracks(sp: Spotify, films: list[Film]) -> list[SoundtrackMatch]:
    """:func:`find_soundtrack` for every film, a few searches at a time."""
    found: list[SoundtrackMatch] = await gather_bounded(
        [partial(find_soundtrack, sp, film) for film in films]
    )
    return found


async def opening_tracks(sp: Spotify, album_id: str, count: int) -> list[str]:
    """URIs of the first *count* tracks of an album.

    The opening tracks rather than the most played: on a score album the
    first cue is the main title, which is the piece the film is
    remembered by, and it needs no second request per track to find.
    """
    page = await sp.album_tracks(album_id, limit=min(count, 50))
    return [track.uri for track in page.items[:count] if track.uri]


async def build_playlist_tracks(
    sp: Spotify, matches: list[SoundtrackMatch], per_film: int
) -> list[str]:
    """Track URIs for the playlist: *per_film* from each matched film, in film order."""
    pages = await gather_bounded(
        [partial(opening_tracks, sp, m.album_id, per_film) for m in matches if m.album_id]
    )
    uris: list[str] = []
    for page in pages:
        uris.extend(uri for uri in page if uri not in uris)
    return uris


async def write_playlist(sp: Spotify, name: str, description: str, uris: list[str]) -> bool:
    """Put *uris* in the user's playlist called *name*, creating it if needed.

    Returns whether it was created. A re-run replaces the contents of the
    playlist it made last time instead of adding a second one, so the
    playlist keeps its URL as the film list grows. New playlists are private.
    """
    manager = PlaylistManager(sp)
    mine = [p for p in await manager.get_owned_playlists() if p["name"] == name]
    if not mine:
        created = await manager.create_playlist(name=name, description=description, public=False)
        await manager.add_tracks(created.spotify_id, uris)
        return True
    playlist_id = mine[0]["id"]
    await sp.playlist_replace(playlist_id, uris[:_REPLACE_CHUNK])
    if len(uris) > _REPLACE_CHUNK:
        await manager.add_tracks(playlist_id, uris[_REPLACE_CHUNK:])
    return False
