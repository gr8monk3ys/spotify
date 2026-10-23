# Design

This repository has one visual surface: the page `spotifyforge export page`
writes (`spotifyforge/core/taste_page.py`). It does not have a design system
of its own. It uses the one from the author's site, lscaturchio.xyz, "The
Field Notebook", whose `DESIGN.md` in that repository is the authority. What
follows records only what the page takes from it, so a change here can be
checked without opening the other repository.

## Tokens used

| Role | Light | Dark |
|---|---|---|
| Paper (ground) | `#f9f8f5` | `#111317` |
| Paper tinted (hover) | `#eeece7` | `#25272d` |
| Ink (text) | `#1a1f23` | `#fafafa` |
| Ink muted (labels, descriptions) | `#606976` | `#abb0ba` |
| Hairline (every rule, 1px) | `#e2dbd5` | `#292c32` |
| Forest ink (the one accent) | `#184e35` | `#42a979` |

| Voice | Face | Setting |
|---|---|---|
| Display | Fraunces 720 | `clamp(2.4rem, 5.2vw, 4.9rem)`, 1.02, -0.035em |
| Section title | Fraunces 650 | `clamp(1.55rem, 2.4vw, 2.25rem)`, 1.18, -0.026em |
| Body | Instrument Sans 400 | 1rem, 1.65 |
| Wall label | IBM Plex Mono 500 | 0.72rem, 0.16em, uppercase, tabular |

The three faces are embedded as data URIs (latin subsets, in
`spotifyforge/assets/fonts/`), because the page must fetch nothing.

## Rules carried over

- **One pen.** Forest ink marks emphasis and action only: the subject half of
  the title, a genre's busiest decade, the link on hover. Never an area.
- **Sand hairlines.** Every divider is 1px of Hairline. No thicker rules, no
  shadows, no cards.
- **Wall labels.** Every count, date and column head is mono, small, uppercase
  and muted. A separator leads its item and never trails.
- **The serif speaks.** Fraunces is for the title and section headings only.
- **Asymmetric splits.** The masthead is `1fr / 300px`; the footer `1.1fr / 2fr`.
- **Nothing animates in.** Transitions are 150 to 200ms on hover and are off
  under reduced motion.

## What is this page's own

- **The grid.** Genres down, decades across, counts in mono. A genre's busiest
  decade is set in forest ink with a 2px underline; an empty cell is a muted
  dash. Pointing at a cell tints its row and its column.
- **Phone layout.** The decade heads shorten to `60s`, and the tables fit a
  390px screen without sideways scrolling. In the artist and album tables the
  second column folds under the name.
- **Print.** Black on white, hairlines kept.

## The Wrapped page

`export wrapped` (`spotifyforge/core/wrapped.py`) is a second page on the same
system. It imports the taste page's stylesheet, fonts and table helpers rather
than restating them, and adds only a two-column split for the "where" section.
Its own grid is hours down and weekdays across; the busiest cell (every one, on a
tie) is set in forest ink, and the weekday heads shorten to `Mo` on phones. The
coverage of the log is a wall label in the masthead, under the thesis.

## Departure from the site

The site's wall label never goes below 0.72rem. On phones this page sets the
table's column heads at 0.6875rem (11px) with tighter tracking so nine numeric
columns fit. That is the only size outside the site's ramp.
