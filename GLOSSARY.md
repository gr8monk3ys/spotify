# SpotifyForge

Turns a Spotify account's liked songs into sequenced genre playlists, and joins that library with Discogs, Letterboxd and the local listening log. Terms below are the ones the code uses.

## Library

**Liked track** (`CurationTrack`):
A saved song, with its artists, genres, release year, ISRC and album identity. The unit curation decides on.
_Avoid_: Saved song, favourite

**Song key** (`song_key`):
The normalised (title, primary artist) pair that makes two edition variants of one recording the same song.
_Avoid_: Track id, dedupe key

**Edition variant**:
The same song released as a remaster, deluxe or album cut under a different track ID. Collapsed by `dedupe_versions`.
_Avoid_: Duplicate, remaster

**ISRC**:
The global recording id; the key for every tempo and key lookup and for finding an artist in MusicBrainz.

## Curation

**Catalogue** (`CurationPlan`):
The full set of playlists a library would produce, plus how it got there. A pure function of the liked library.
_Avoid_: Playlist list

**Playlist spec** (`PlaylistSpec`):
One planned playlist: title, description, genre, decade and ordered tracks.
_Avoid_: Playlist (that is the live Spotify object)

**Forge**:
Create the catalogue's playlists on Spotify (`curate forge`).
_Avoid_: Generate, build

**Forged playlist**:
A live playlist whose description matches one of this code's own templates (`was_forged`). Only forged playlists may be retired.

**Reflow**:
Re-sequence existing forged playlists in place so URLs, followers and additions are preserved (`curate reflow`).
_Avoid_: Rebuild, regenerate

**Retire**:
Unfollow a forged playlist the catalogue no longer plans. Spotify has no delete.
_Avoid_: Delete

**Exclusive**:
The option that files each track under its rarest genre only (`CurationOptions.exclusive`).

**Expansion** (`Pins`):
Unheard tracks from the same niche, searched once and pinned in a sidecar keyed by (genre, decade), then folded into the plan.
_Avoid_: Suggestions, recommendations

**Unclassified**:
The genre label for tracks whose artists carry no genre Spotify would name; shown as "beyond genre".

## Sequencing

**Flow order** (`order_for_flow`):
The sequencing of a playlist so it runs smoothly, preferring harmonic key moves and separating same-artist tracks.

**Arc ordering**:
The fallback sequence by popularity used when key coverage is too thin for harmonic ordering.

**Camelot key** (`camelot`):
A track's key on the Camelot wheel; `key_distance` between two is what harmonic ordering minimises.

**Feature** (`AudioFeature`):
Tempo and key for one recording, from Deezer, AcousticBrainz or a preview estimate, cached on disk by ISRC.
_Avoid_: Audio features endpoint (withdrawn by Spotify)

**Estimated tempo**:
A BPM computed locally from a Deezer 30-second preview (`PreviewTempoProvider`) when no database has one.

## Listening

**Play** (`Play`):
One play of one track from the local listening log.
_Avoid_: Stream, listen

**Listening log**:
The append-only `listening_history.jsonl`, the only history there is because Spotify exposes just the last 50 plays.

**Gap**:
A stretch the log may be missing plays from, recorded when a sync found all 50 plays new.

## Cross-platform

**Shelf record** (`ShelfRecord`):
One record on a Discogs shelf, either collection or wantlist.

**Taste page**:
A single self-contained HTML summary of the library (`export page`).

**Wrapped**:
A private HTML page built from the listening log (`export wrapped`).

**Music dir**:
The shared directory (`~/.music`) holding `music-library.json` and the files exchanged with the other media repos.
