# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

delegated: one self-contained static HTML file written by a Python command
(`spotifyforge export page`). No build step, no JavaScript framework, no
external requests, so the file can be opened from disk or dropped onto any
host. Chosen because the project is a Python CLI with no web toolchain, and
the page is an output of the tool, not an application.

## Users

People looking at Lorenzo's work: someone who arrived from his GitHub profile
or CV and has a minute or two. They are deciding whether this project is real
and whether its author has taste and rigour. They are technical enough to
recognise a number that was measured.

## Product Purpose

SpotifyForge is a CLI and MCP server that reads one person's Spotify liked
songs and turns them into sequenced genre playlists. The taste page is a
single page it generates from that library: what the library contains, as
this tool reads it. It succeeds when a visitor comes away knowing what Lorenzo
listens to and that the tool behind the page does something non-trivial.

## Positioning

Every number on the page is produced by the tool's own reading of the library:
genres inherited from each track's primary artist, edition variants collapsed,
untagged artists filled from MusicBrainz by ISRC. A screenshot of Spotify
Wrapped cannot show that, and neither can a generic stats site.

## Operating Context

Generated on demand from the command line into a local file. It is not
published by the tool; where it is hosted, if anywhere, is the owner's
decision after the fact (confirmed 2026-10-12: local file only).

## Capabilities and Constraints

- Shows a summary only (confirmed 2026-10-12): counts, top genres, decades,
  most-liked artists, most-liked albums. No full track list and no play
  history.
- Static: no live data, no account access, no requests at view time.
- Must not imply an audience. The account has 19 followers; the page is about
  a library, not reach.
- Undecided: whether the page is ever published, and where.

## Brand Commitments

The page looks like the author's site, lscaturchio.xyz (confirmed 2026-10-12:
"make the design more like lscaturchio.xyz"). That site's `DESIGN.md`, "The
Field Notebook", is the visual authority: warm paper, one forest-green ink,
Fraunces for headings, Instrument Sans for reading, IBM Plex Mono for labels.

## Evidence on Hand

- The live library: about 3,000 liked songs, 2,662 unique after collapsing
  editions, 388 distinct genres.
- 753 songs had no Spotify genre; MusicBrainz classified 424 of them.
- The repository: about 600 tests against an in-memory Spotify API.
- No testimonials, users, or usage figures exist. None may be invented.

## Product Principles

- Measured, not claimed. A figure appears only if the tool computed it.
- The library is the subject. The page reports it; it does not sell anything.
- Say what is unknown. Unclassified songs are shown as unclassified.
- One file. It must survive being emailed, opened offline, or printed.

## Accessibility & Inclusion

Readable in light and dark, from a phone to a wide desktop, with keyboard and
screen reader. Titles and artist names are in many scripts (Japanese, Arabic,
Hebrew, Cyrillic appear in the library) and must render and wrap correctly.
