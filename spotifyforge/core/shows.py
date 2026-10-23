"""Upcoming shows near home by the artists you actually play.

Spotify knows who you listen to but not who is touring; Ticketmaster's
Discovery API knows who is touring but not who you listen to. This joins
the two: your most-played artists (Spotify's top artists over all three
time ranges, plus the local listening log when there is one) are looked
up as Ticketmaster *attractions*, and each matched attraction's events
near home in the next N days are listed.

Matching is the part that can go wrong. Ticketmaster's attraction search
is a fuzzy keyword search, and band names collide ("Low", "Boston",
"Air"). So a match is only taken when it is one of:

* **linked** — the attraction's ``externalLinks.spotify`` points at this
  very Spotify artist. Certain.
* **name** — exactly one attraction whose name (or an alias) equals the
  artist's, ignoring case, punctuation and accents, and which links to no
  *other* Spotify artist. Probable, and reported as such.

Several same-named attractions with no Spotify link to choose between is
reported as ambiguous and not guessed at.

Ticketmaster allows 5 requests a second and 5,000 a day on a free key.
One run asks one attraction search per artist and one event search per
matched attraction that has anything upcoming, so ~100 requests for 50
artists. Nothing is cached: their terms only allow event content to be
kept "for reasonable periods", and a run is quick enough not to need it.
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

import httpx
from media_core.names import fold

from spotifyforge.core.audio_features import _RateLimiter

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tekore import Spotify

    from spotifyforge.core.history import Play

logger = logging.getLogger(__name__)

_API = "https://app.ticketmaster.com/discovery/v2"

# Under the documented 5 requests/second, with room for clock jitter.
_TICKETMASTER_INTERVAL = 0.22
# Attraction candidates read per artist. Exact names rank near the top of
# Ticketmaster's relevance sort; twenty leaves room for tribute acts.
_ATTRACTION_CANDIDATES = 20
# Events read per attraction. An artist with more than this many shows
# near one home inside the window is a residency; the report says so.
_EVENTS_PER_ATTRACTION = 100

# Ticketmaster spells it "canceled"; the other spelling costs nothing to accept.
_CANCELLED = {"canceled", "cancelled"}

_TIME_RANGES = ("short_term", "medium_term", "long_term")
# A logged artist needs this many plays in this window to count as one you play.
_MIN_LOGGED_PLAYS = 3
_LOG_WINDOW = timedelta(days=90)

_SPOTIFY_ARTIST = re.compile(r"open\.spotify\.com/(?:[a-z-]+/)?artist/([A-Za-z0-9]+)")
_LATLONG = re.compile(r"^\s*(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")

HOW_TO_GET_A_KEY = (
    "Shows need a Ticketmaster API key. Get a free one at "
    "https://developer.ticketmaster.com (sign up, then use the Consumer Key "
    "of the app it creates) and set SPOTIFYFORGE_TICKETMASTER_API_KEY."
)
HOW_TO_SET_HOME = (
    'Shows need a home location. Set SPOTIFYFORGE_HOME_LOCATION to "lat,long" '
    '(e.g. "34.0522,-118.2437") or a city name, or pass one for this run.'
)

Confidence = Literal["linked", "name"]


class TicketmasterError(Exception):
    """Ticketmaster refused or failed a request; the message says why."""


# ---------------------------------------------------------------------------
# Home
# ---------------------------------------------------------------------------

_GEOHASH_ALPHABET = "0123456789bcdefghjkmnpqrstuvwxyz"


def geohash(lat: float, lon: float, precision: int = 9) -> str:
    """Encode a point as a geohash, which is what ``geoPoint`` takes.

    Ticketmaster deprecated its ``latlong`` filter in favour of
    ``geoPoint``. Nine characters is a cell of a few metres, far finer
    than any radius anyone would search.
    """
    bounds = [[-90.0, 90.0], [-180.0, 180.0]]
    bits: list[int] = []
    even = True  # geohash interleaves longitude first
    while len(bits) < precision * 5:
        axis, value = (1, lon) if even else (0, lat)
        mid = sum(bounds[axis]) / 2
        if value >= mid:
            bits.append(1)
            bounds[axis][0] = mid
        else:
            bits.append(0)
            bounds[axis][1] = mid
        even = not even
    return "".join(
        _GEOHASH_ALPHABET[int("".join(map(str, bits[i : i + 5])), 2)]
        for i in range(0, len(bits), 5)
    )


@dataclass(frozen=True)
class Home:
    """Where to look: a point and radius, or a city by name."""

    latlong: tuple[float, float] | None = None
    city: str | None = None
    radius_miles: int = 50

    @classmethod
    def parse(cls, location: str, radius_miles: int = 50) -> Home:
        """``"34.05,-118.24"`` is a point; anything else is a city name."""
        text = location.strip()
        if not text:
            raise ValueError(HOW_TO_SET_HOME)
        match = _LATLONG.match(text)
        if match:
            lat, lon = float(match.group(1)), float(match.group(2))
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError(f"{text!r} is not a latitude,longitude pair.")
            return cls(latlong=(lat, lon), radius_miles=radius_miles)
        return cls(city=text, radius_miles=radius_miles)

    def params(self) -> dict[str, str]:
        if self.latlong is not None:
            return {
                "geoPoint": geohash(*self.latlong),
                "radius": str(self.radius_miles),
                "unit": "miles",
            }
        return {"city": self.city or ""}

    def describe(self) -> str:
        if self.latlong is not None:
            lat, lon = self.latlong
            return f"within {self.radius_miles} miles of {lat:g},{lon:g}"
        return f"in {self.city}"


# ---------------------------------------------------------------------------
# The artists you play
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlayedArtist:
    """An artist you play, and the evidence that you do."""

    spotify_id: str | None
    name: str
    best_rank: int  # best position in any Spotify top-artists range, 1-based
    ranges: tuple[str, ...] = ()  # which top-artists ranges it appears in
    logged_plays: int = 0


async def artists_you_play(
    sp: Spotify, plays: Sequence[Play] = (), limit: int = 50
) -> list[PlayedArtist]:
    """Your most-played artists, most certain first.

    Spotify's top artists over four weeks, six months and all time are
    merged; an artist in more ranges ranks higher, then by its best
    position. Artists the listening log shows at least
    :data:`_MIN_LOGGED_PLAYS` plays of are added after them, since a
    local log is shorter evidence than Spotify's own.
    """
    ranks: dict[str, int] = {}
    names: dict[str, str] = {}
    in_ranges: dict[str, list[str]] = {}
    for time_range in _TIME_RANGES:
        paging = await sp.current_user_top_artists(time_range=time_range, limit=50)
        for position, artist in enumerate(paging.items or (), start=1):
            if not artist.id:
                continue
            names[artist.id] = _clean(artist.name) or ""
            ranks[artist.id] = min(ranks.get(artist.id, position), position)
            in_ranges.setdefault(artist.id, []).append(time_range)

    logged: Counter[str] = Counter()
    for play in plays:
        if play.artist_ids:
            logged[play.artist_ids[0]] += 1
            names.setdefault(
                play.artist_ids[0], (_clean(play.artists[0]) or "") if play.artists else ""
            )

    out = [
        PlayedArtist(aid, names[aid], ranks[aid], tuple(in_ranges[aid]), logged[aid])
        for aid in ranks
    ]
    out.sort(key=lambda a: (-len(a.ranges), a.best_rank, -a.logged_plays, a.name))
    out += sorted(
        (
            PlayedArtist(aid, names[aid], 0, (), count)
            for aid, count in logged.items()
            if aid not in ranks and count >= _MIN_LOGGED_PLAYS and names[aid]
        ),
        key=lambda a: (-a.logged_plays, a.name),
    )
    return out[:limit]


# ---------------------------------------------------------------------------
# Ticketmaster
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Attraction:
    """A Ticketmaster attraction matched to one of your artists."""

    artist: PlayedArtist
    attraction_id: str
    name: str
    confidence: Confidence
    upcoming: int | None  # Ticketmaster's count of upcoming events, worldwide


@dataclass(frozen=True)
class Show:
    """One upcoming event near home."""

    event_id: str
    name: str
    artists: tuple[str, ...]  # your artists on the bill, as Spotify names them
    confidence: Confidence  # the weakest match among them
    local_date: str | None
    local_time: str | None
    venue: str | None
    city: str | None
    region: str | None
    country: str | None
    distance_miles: float | None
    status: str | None
    url: str | None  # only ever a vetted https ticket page; see safe_event_url


@dataclass
class ShowsReport:
    """What a search found, and how sure it is."""

    home: Home
    days: int
    artists_checked: int = 0
    shows: list[Show] = field(default_factory=list)
    matched: list[Attraction] = field(default_factory=list)
    ambiguous: list[tuple[str, int]] = field(default_factory=list)  # (artist, candidates)
    not_on_ticketmaster: list[str] = field(default_factory=list)
    # Artists with more events than one search returned.
    truncated: list[str] = field(default_factory=list)
    requests: int = 0


class Ticketmaster:
    """The two Discovery API calls this needs, rate-limited and keyed."""

    def __init__(self, api_key: str, client: httpx.AsyncClient) -> None:
        if not api_key:
            raise TicketmasterError(HOW_TO_GET_A_KEY)
        self._key = api_key
        self._client = client
        self._limiter = _RateLimiter(_TICKETMASTER_INTERVAL)
        self.requests = 0

    async def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        for attempt in range(2):
            await self._limiter.wait()
            self.requests += 1
            response = await self._client.get(
                f"{_API}/{path}", params={**params, "apikey": self._key}
            )
            if response.status_code == 429 and attempt == 0:
                # Per-second spikes clear in a second; a spent daily quota
                # does not, and the second 429 reports it.
                await asyncio.sleep(1.0)
                continue
            break
        if response.status_code == 401:
            raise TicketmasterError(
                "Ticketmaster rejected the API key. Check SPOTIFYFORGE_TICKETMASTER_API_KEY "
                "is the app's Consumer Key."
            )
        if response.status_code == 429:
            raise TicketmasterError(
                "Ticketmaster's rate limit was hit (a free key allows 5,000 requests a "
                "day). Try again later."
            )
        if response.status_code != 200:
            raise TicketmasterError(f"Ticketmaster answered HTTP {response.status_code}.")
        body = response.json()
        return body if isinstance(body, dict) else {}

    async def attractions(self, keyword: str) -> list[dict[str, Any]]:
        body = await self._get(
            "attractions.json",
            {
                "keyword": keyword,
                "classificationName": "music",
                "size": str(_ATTRACTION_CANDIDATES),
            },
        )
        return list((body.get("_embedded") or {}).get("attractions") or [])

    async def events(
        self, attraction_id: str, home: Home, start: datetime, end: datetime
    ) -> tuple[list[dict[str, Any]], int]:
        """Events for one attraction near *home*; also the total that exist."""
        body = await self._get(
            "events.json",
            {
                "attractionId": attraction_id,
                **home.params(),
                # Ticketmaster rejects fractional seconds here.
                "startDateTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "endDateTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "sort": "date,asc",
                "size": str(_EVENTS_PER_ATTRACTION),
            },
        )
        events = list((body.get("_embedded") or {}).get("events") or [])
        total = int((body.get("page") or {}).get("totalElements") or len(events))
        return events, total


def _spotify_ids(attraction: dict[str, Any]) -> set[str]:
    links = (attraction.get("externalLinks") or {}).get("spotify") or []
    found = set()
    for link in links:
        match = _SPOTIFY_ARTIST.search(str((link or {}).get("url") or ""))
        if match:
            found.add(match.group(1))
    return found


# Hosts an event link may point at. Anything else is dropped rather than
# shown: the CLI turns a URL into a terminal hyperlink, and an MCP client
# may render one, so it has to be a ticket page and nothing cleverer.
_LINK_HOSTS = ("ticketmaster.com", "livenation.com")
# Lowercase ASCII hostnames only: no IDN look-alikes, no IPv6 brackets.
_HOST = re.compile(r"[a-z0-9.-]+")
# Bidirectional overrides reorder how text displays without changing it.
_BIDI_CONTROLS = {chr(c) for c in (*range(0x202A, 0x202F), *range(0x2066, 0x206A))}


def _clean(value: object) -> str | None:
    """Text from an outside service, minus control characters.

    Rich passes ESC through, so a name carrying an escape sequence could
    otherwise restyle the terminal or plant a hyperlink of its own.
    """
    if value is None:
        return None
    return "".join(
        c for c in str(value) if unicodedata.category(c) != "Cc" and c not in _BIDI_CONTROLS
    )


def safe_event_url(raw: object) -> str | None:
    """An https link to a Ticketmaster or Live Nation page, rebuilt from vetted parts.

    Anything a different URL parser might read differently is refused
    outright rather than repaired: backslashes (``evil.com\\@ticketmaster.com``
    is a different host to a browser than to :func:`urlsplit`), userinfo,
    ports, whitespace and control characters. What is returned is rebuilt
    from the checked scheme, host, path and query, never *raw* itself, so
    nothing the checks did not look at reaches the terminal or the model.
    """
    if not isinstance(raw, str) or not raw or "\\" in raw:
        return None
    if any(c.isspace() or unicodedata.category(c).startswith("C") for c in raw):
        return None
    try:
        parts = urlsplit(raw)
    except ValueError:
        return None
    if parts.scheme != "https" or "@" in parts.netloc or ":" in parts.netloc:
        return None
    host = parts.netloc
    if not _HOST.fullmatch(host):
        return None
    if not any(host == h or host.endswith("." + h) for h in _LINK_HOSTS):
        return None
    return f"https://{host}{parts.path}" + (f"?{parts.query}" if parts.query else "")


def _comparable(name: str) -> str:
    """*name* folded, accents dropped: "Sigur Rós" and "Sigur Ros" agree."""
    decomposed = unicodedata.normalize("NFKD", name)
    return fold("".join(c for c in decomposed if not unicodedata.combining(c)))


def _upcoming(attraction: dict[str, Any]) -> int | None:
    total = (attraction.get("upcomingEvents") or {}).get("_total")
    return int(total) if isinstance(total, int) else None


def match_attraction(
    artist: PlayedArtist, candidates: Sequence[dict[str, Any]]
) -> tuple[dict[str, Any] | None, Confidence | None, int]:
    """Pick *artist*'s attraction from search results.

    Returns (attraction, confidence, same-named candidates). An attraction
    that links to a different Spotify artist is never a name match, however
    alike the names: that link is Ticketmaster saying it is someone else.
    """
    if artist.spotify_id:
        for candidate in candidates:
            if artist.spotify_id in _spotify_ids(candidate):
                return candidate, "linked", 1

    wanted = _comparable(artist.name)
    same_name = [
        c
        for c in candidates
        if c.get("id")
        and not _spotify_ids(c)
        and wanted
        in {
            _comparable(str(c.get("name") or "")),
            *(_comparable(str(a)) for a in c.get("aliases") or ()),
        }
    ]
    if len(same_name) == 1:
        return same_name[0], "name", 1
    return None, None, len(same_name)


def _show(event: dict[str, Any], artist: str, confidence: Confidence) -> Show:
    dates = event.get("dates") or {}
    start = dates.get("start") or {}
    venues = (event.get("_embedded") or {}).get("venues") or [{}]
    venue = venues[0] or {}
    distance = venue.get("distance", event.get("distance"))
    units = str(venue.get("units") or event.get("units") or "MILES").upper()
    if isinstance(distance, int | float) and units.startswith("K"):
        distance = distance / 1.609344
    return Show(
        event_id=str(event.get("id") or ""),
        name=_clean(event.get("name")) or "",
        artists=(artist,),
        confidence=confidence,
        local_date=_clean(start.get("localDate")),
        local_time=_clean(start.get("localTime")),
        venue=_clean(venue.get("name")),
        city=_clean((venue.get("city") or {}).get("name")),
        region=_clean((venue.get("state") or {}).get("stateCode")),
        country=_clean((venue.get("country") or {}).get("countryCode")),
        distance_miles=float(distance) if isinstance(distance, int | float) else None,
        status=_clean((dates.get("status") or {}).get("code")),
        url=safe_event_url(event.get("url")),
    )


def _merge(existing: Show, artist: str, confidence: Confidence) -> Show:
    """One event, several of your artists on the bill."""
    weakest: Confidence = "name" if "name" in (existing.confidence, confidence) else "linked"
    artists = existing.artists if artist in existing.artists else (*existing.artists, artist)
    return replace(existing, artists=artists, confidence=weakest)


async def find_shows(
    artists: Sequence[PlayedArtist],
    home: Home,
    days: int,
    api_key: str,
    client: httpx.AsyncClient | None = None,
    now: datetime | None = None,
) -> ShowsReport:
    """Upcoming shows by *artists* near *home* in the next *days* days."""
    start = (now or datetime.now(UTC)).replace(microsecond=0)
    end = start + timedelta(days=days)
    report = ShowsReport(home=home, days=days, artists_checked=len(artists))
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=30)
    by_event: dict[str, Show] = {}
    try:
        api = Ticketmaster(api_key, client)
        try:
            for artist in artists:
                candidates = await api.attractions(artist.name)
                chosen, confidence, same_named = match_attraction(artist, candidates)
                if chosen is None or confidence is None:
                    if same_named > 1:
                        report.ambiguous.append((artist.name, same_named))
                    else:
                        report.not_on_ticketmaster.append(artist.name)
                    continue
                attraction = Attraction(
                    artist,
                    str(chosen["id"]),
                    str(chosen.get("name") or artist.name),
                    confidence,
                    _upcoming(chosen),
                )
                report.matched.append(attraction)
                if attraction.upcoming == 0:
                    continue  # nothing anywhere, so nothing near home
                events, total = await api.events(attraction.attraction_id, home, start, end)
                if total > len(events):
                    report.truncated.append(artist.name)
                for event in events:
                    status = ((event.get("dates") or {}).get("status") or {}).get("code")
                    if status in _CANCELLED:
                        continue
                    shown = _show(event, artist.name, confidence)
                    key = shown.event_id or f"{shown.name}|{shown.local_date}|{shown.venue}"
                    by_event[key] = (
                        _merge(by_event[key], artist.name, confidence) if key in by_event else shown
                    )
        finally:
            report.requests = api.requests
    finally:
        if owns_client:
            await client.aclose()

    report.shows = sorted(
        by_event.values(), key=lambda s: (s.local_date or "9999", s.local_time or "", s.name)
    )
    return report


async def upcoming_shows(
    sp: Spotify,
    home: Home,
    days: int,
    api_key: str,
    artist_limit: int = 50,
    client: httpx.AsyncClient | None = None,
) -> ShowsReport:
    """The whole join: the artists you play, then their shows near *home*.

    The key is checked before Spotify is asked anything, so a missing one
    costs no requests.
    """
    from spotifyforge.core.clock import utc_now
    from spotifyforge.core.history import load_plays

    if not api_key:
        raise TicketmasterError(HOW_TO_GET_A_KEY)
    now = utc_now()
    plays = load_plays(since=now - _LOG_WINDOW)
    artists = await artists_you_play(sp, plays, limit=artist_limit)
    return await find_shows(artists, home, days, api_key, client=client, now=now)
