"""The liked library as a platform-neutral file other repos can read.

Taste lives in one account but describes several. This module rolls the
liked library up to *album* level — the unit Discogs (releases) and
RateYourMusic (ratings) are keyed by, where Spotify is keyed by track —
and writes it as one versioned JSON file that consumer repos import.

Two deliberate choices about identity:

ISRCs are exported, MusicBrainz ids are not. The feature cache resolves
ISRC → MusicBrainz recording while fetching tempo and key, then discards
the mapping; only the readings are kept. Consumers therefore re-resolve
from the ISRCs here, which MusicBrainz answers 25 at a time — cheap, and
cacheable on their side. Claiming to export ids we never stored would
have sent them looking for a field that is not there.

``affinity`` — liked tracks over album length — is the ranking signal.
An album with nine of ten tracks liked is a record; one with a single
liked track is a playlist add. No consumer should treat those the same.

Discoveries (unheard tracks pinned by ``curate expand``) are kept in
their own section rather than mixed into albums. A consumer that pushed
an unheard track to a wantlist, or queued it to be rated, would be
acting on music the user has never heard.

The ``schema`` field is the compatibility contract. Nothing here reads
the file back — the consumers live in other repos and parse it with
plain ``json`` — so they own the version check, and the string is the
only thing that tells them a future export is not theirs to read.

The envelope round the albums (``schema``, ``generated_at``, the sections
in order) and the atomic write are ``media_core.export``: five repos had
written the same two functions under the same two names. The rows are
still built here, because building them needs this account.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import TYPE_CHECKING, Any

from media_core.export import build_export
from media_core.export import write_export as _write_envelope

if TYPE_CHECKING:
    from pathlib import Path

    from spotifyforge.core.audio_features import AudioFeature
    from spotifyforge.core.curation import CurationTrack
    from spotifyforge.core.expansion import Pins

logger = logging.getLogger(__name__)

SCHEMA = "music-library/1"

# This repo writes the compact form: no indentation, ASCII-escaped, no trailing
# newline. media_core defaults to the indented form the other tools use, so the
# choice is passed explicitly and the file's bytes do not move.
_JSON_STYLE: dict[str, Any] = {"indent": None, "ensure_ascii": True, "newline": False}


def export_path() -> Path:
    """Where the interchange file lives: ``<music_dir>/music-library.json``.

    ``music_dir`` is the directory the other collection tools read from too
    (``MUSIC_DIR``, default ``~/.music``).
    """
    from spotifyforge.config import settings

    return settings.music_dir / "music-library.json"


def legacy_export_path() -> Path:
    """Where the file lived before ``~/.music``: beside the database."""
    from spotifyforge.config import sidecar_path

    return sidecar_path("music-library.json")


def _album_entry(tracks: list[CurationTrack], features: dict[str, AudioFeature]) -> dict[str, Any]:
    """One album's export record, rolled up from its liked tracks."""
    first = tracks[0]
    # Credit the artists actually liked on this album, most-liked first:
    # on a single-artist album that is the album artist, and on a
    # compilation it is the people the user came for.
    billing: Counter[tuple[str, str]] = Counter()
    for track in tracks:
        for artist_id, name in zip(track.artist_ids, track.artist_names, strict=False):
            billing[(artist_id, name)] += 1

    isrcs = sorted({t.isrc for t in tracks if t.isrc})
    readings = [features[isrc] for isrc in isrcs if isrc in features]
    total = first.album_total_tracks

    return {
        "spotify_album_id": first.album_id,
        "title": first.album_name,
        "artists": [{"spotify_id": aid, "name": name} for (aid, name), _ in billing.most_common()],
        "year": first.release_year,
        "liked_track_count": len(tracks),
        "total_tracks": total,
        "affinity": round(len(tracks) / total, 4) if total else None,
        "isrcs": isrcs,
        "genres": sorted({g for t in tracks for g in t.genres}),
        "tempo_known": sum(1 for r in readings if r.tempo is not None),
        "key_known": sum(1 for r in readings if r.has_key),
    }


def build_library_export(
    tracks: list[CurationTrack],
    features: dict[str, AudioFeature],
    expansions: Pins,
    user_id: str,
    generated_at: str,
) -> dict[str, Any]:
    """Build the interchange document from already-fetched data.

    Pure, so the shape two other repos depend on can be tested without a
    network. Albums are ordered by liked-track count then title, which
    makes the file diffable between runs.
    """
    by_album: dict[str, list[CurationTrack]] = {}
    for track in tracks:
        if track.album_id:
            by_album.setdefault(track.album_id, []).append(track)

    albums = [_album_entry(group, features) for group in by_album.values()]
    albums.sort(key=lambda a: (-a["liked_track_count"], a["title"]))

    discoveries = [
        {
            "genre": genre,
            "decade": decade,
            "tracks": [
                {"name": t.name, "artists": list(t.artist_names), "isrc": t.isrc} for t in pinned
            ],
        }
        # Undated pins sort first rather than raising: a pin key's decade
        # is None or an int, and comparing the two ends the whole export.
        for (genre, decade), pinned in sorted(
            expansions.items(), key=lambda kv: (kv[0][0], -1 if kv[0][1] is None else kv[0][1])
        )
        if pinned
    ]

    skipped = len(tracks) - sum(len(group) for group in by_album.values())
    if skipped:
        logger.info("%d liked track(s) carried no album and were skipped", skipped)
    logger.info("Exported %d album(s), %d discovered niche(s)", len(albums), len(discoveries))

    # `source` is passed as a section rather than as media_core's `username`:
    # this export names the platform beside the user, which the file-per-account
    # exports do not, so it keeps its own shape in its own position.
    return build_export(
        SCHEMA,
        generated_at,
        source={"platform": "spotify", "user": user_id},
        albums=albums,
        discoveries=discoveries,
    )


def write_export(document: dict[str, Any], path: Path | None = None) -> Path:
    """Write *document* out atomically and return where it landed."""
    target = _write_envelope(document, path or export_path(), **_JSON_STYLE)
    if path is None:
        # Consumers still on the old path (~/.spotifyforge/music-library.json)
        # keep reading a fresh file; remove after 2026-10.
        legacy = legacy_export_path()
        if legacy != target:
            _write_envelope(document, legacy, **_JSON_STYLE)
    return target
