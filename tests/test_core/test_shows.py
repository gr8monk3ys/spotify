"""Tests for shows near home by the artists you play.

Ticketmaster is mocked with ``httpx.MockTransport``. The response bodies
follow the Discovery API v2 documentation (``_embedded.attractions``,
``_embedded.events`` with ``dates``/``_embedded.venues``, ``page``);
they were not captured from a live key, since none was available.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest

from spotifyforge.core import shows
from spotifyforge.core.history import Play
from spotifyforge.core.shows import (
    HOW_TO_GET_A_KEY,
    Home,
    PlayedArtist,
    TicketmasterError,
    artists_you_play,
    find_shows,
    geohash,
    match_attraction,
    upcoming_shows,
)

NOW = datetime(2026, 10, 22, 18, 30, 15, 123456, tzinfo=UTC)
HOME = Home(latlong=(34.0522, -118.2437), radius_miles=50)
KEY = "test-consumer-key"


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    monkeypatch.setattr(shows, "_TICKETMASTER_INTERVAL", 0)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(shows.asyncio, "sleep", no_sleep)


def _artist(spotify_id: str | None, name: str) -> PlayedArtist:
    return PlayedArtist(spotify_id=spotify_id, name=name, best_rank=1, ranges=("short_term",))


def _attraction(
    attraction_id: str,
    name: str,
    *,
    spotify: str | None = None,
    upcoming: int | None = 3,
    aliases: list[str] | None = None,
) -> dict:
    body: dict = {
        "name": name,
        "type": "attraction",
        "id": attraction_id,
        "test": False,
        "url": f"https://www.ticketmaster.com/{name.lower()}-tickets/artist/1",
        "locale": "en-us",
        "classifications": [{"primary": True, "segment": {"name": "Music"}}],
        "_links": {"self": {"href": f"/discovery/v2/attractions/{attraction_id}?locale=en-us"}},
    }
    if spotify:
        body["externalLinks"] = {"spotify": [{"url": f"https://open.spotify.com/artist/{spotify}"}]}
    if upcoming is not None:
        body["upcomingEvents"] = {"_total": upcoming, "ticketmaster": upcoming, "_filtered": 0}
    if aliases:
        body["aliases"] = aliases
    return body


def _event(
    event_id: str,
    name: str,
    date: str,
    *,
    status: str = "onsale",
    distance: float = 12.3,
    units: str = "MILES",
    venue: str = "Hollywood Bowl",
) -> dict:
    return {
        "name": name,
        "type": "event",
        "id": event_id,
        "url": f"https://www.ticketmaster.com/event/{event_id}",
        "locale": "en-us",
        "distance": distance,
        "units": units,
        "dates": {
            "start": {"localDate": date, "localTime": "20:00:00", "dateTBA": False},
            "timezone": "America/Los_Angeles",
            "status": {"code": status},
        },
        "_embedded": {
            "venues": [
                {
                    "name": venue,
                    "type": "venue",
                    "id": "KovZpZAEkn6A",
                    "distance": distance,
                    "units": units,
                    "city": {"name": "Los Angeles"},
                    "state": {"name": "California", "stateCode": "CA"},
                    "country": {"name": "United States Of America", "countryCode": "US"},
                }
            ]
        },
    }


def _page(key: str, items: list[dict], total: int | None = None) -> dict:
    total = len(items) if total is None else total
    body: dict = {"page": {"size": 20, "totalElements": total, "totalPages": 1, "number": 0}}
    if items:  # Ticketmaster omits _embedded entirely when nothing matched
        body["_embedded"] = {key: items}
    return body


class FakeTicketmaster:
    """Answers attraction searches by keyword and event searches by attraction id."""

    def __init__(self, attractions: dict[str, list[dict]], events: dict[str, dict]) -> None:
        self.attractions = attractions
        self.events = events
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        query = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        assert query["apikey"] == KEY
        if request.url.path.endswith("/attractions.json"):
            return httpx.Response(
                200, json=_page("attractions", self.attractions.get(query["keyword"], []))
            )
        assert request.url.path.endswith("/events.json")
        return httpx.Response(200, json=self.events.get(query["attractionId"], _page("events", [])))

    def queries(self, path: str) -> list[dict[str, str]]:
        return [
            {k: v[0] for k, v in parse_qs(r.url.query.decode()).items()}
            for r in self.requests
            if r.url.path.endswith(path)
        ]


async def _find(fake: FakeTicketmaster, artists, home: Home = HOME, days: int = 90):
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as client:
        return await find_shows(artists, home, days, KEY, client=client, now=NOW)


# ---------------------------------------------------------------------------
# Home
# ---------------------------------------------------------------------------


def test_geohash_matches_known_values():
    assert geohash(42.6, -5.6, precision=5) == "ezs42"
    assert geohash(57.64911, 10.40744, precision=11) == "u4pruydqqvj"


def test_home_parses_a_point_and_searches_by_geopoint():
    home = Home.parse(" 34.0522, -118.2437 ", radius_miles=25)
    assert home.latlong == (34.0522, -118.2437)
    assert home.params() == {
        "geoPoint": geohash(34.0522, -118.2437),
        "radius": "25",
        "unit": "miles",
    }
    assert home.describe() == "within 25 miles of 34.0522,-118.244"


def test_home_takes_anything_else_as_a_city():
    home = Home.parse("Los Angeles")
    assert home.params() == {"city": "Los Angeles"}
    assert home.describe() == "in Los Angeles"


def test_home_refuses_an_impossible_point_and_explains_an_empty_one():
    with pytest.raises(ValueError, match="not a latitude"):
        Home.parse("95,10")
    with pytest.raises(ValueError, match="SPOTIFYFORGE_HOME_LOCATION"):
        Home.parse("  ")


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def test_a_spotify_link_is_a_certain_match_whatever_the_name():
    candidates = [
        _attraction("A1", "Boygenius Tribute"),
        _attraction("A2", "boygenius", spotify="1hLiboQ98IQWhpKeP9vRFw"),
    ]
    chosen, confidence, _ = match_attraction(_artist("1hLiboQ98IQWhpKeP9vRFw", "x"), candidates)
    assert chosen["id"] == "A2"
    assert confidence == "linked"


def test_one_unlinked_attraction_with_the_same_name_is_a_name_match():
    candidates = [_attraction("A1", "Beyoncé"), _attraction("A2", "Beyonce Experience")]
    chosen, confidence, _ = match_attraction(_artist("spX", "Beyonce"), candidates)
    assert chosen["id"] == "A1"  # folded: case and accents do not matter
    assert confidence == "name"


def test_an_alias_counts_as_the_name():
    candidates = [_attraction("A1", "The Weeknd Official", aliases=["the weeknd"])]
    chosen, confidence, _ = match_attraction(_artist("spX", "The Weeknd"), candidates)
    assert (chosen["id"], confidence) == ("A1", "name")


def test_a_same_named_act_linked_to_another_spotify_artist_is_not_ours():
    # Ticketmaster saying "this Low is that Spotify artist" rules it out
    # for a different Spotify artist called Low.
    candidates = [_attraction("A1", "Low", spotify="someoneElse")]
    assert match_attraction(_artist("ourLow", "Low"), candidates) == (None, None, 0)


def test_several_same_named_acts_are_ambiguous_not_guessed():
    candidates = [
        _attraction("A1", "Air"),
        _attraction("A2", "AIR"),
        _attraction("A3", "Air Supply"),
    ]
    assert match_attraction(_artist("spAir", "Air"), candidates) == (None, None, 2)


# ---------------------------------------------------------------------------
# Finding shows
# ---------------------------------------------------------------------------


async def test_find_shows_joins_artists_to_events_near_home():
    fake = FakeTicketmaster(
        attractions={
            "Phoebe Bridgers": [_attraction("PB", "Phoebe Bridgers", spotify="spPB")],
            "Julien Baker": [_attraction("JB", "Julien Baker")],  # name match only
            "Nobody Tours": [],
            "Air": [_attraction("A1", "Air"), _attraction("A2", "Air")],
            "Quiet Year": [_attraction("QY", "Quiet Year", spotify="spQY", upcoming=0)],
        },
        events={
            "PB": _page(
                "events",
                [
                    _event("E2", "Phoebe Bridgers with Julien Baker", "2026-12-01"),
                    _event(
                        "E1", "Phoebe Bridgers", "2026-11-14", distance=20.0, units="KILOMETERS"
                    ),
                    _event("EX", "Phoebe Bridgers", "2026-11-20", status="canceled"),
                ],
            ),
            "JB": _page(
                "events", [_event("E2", "Phoebe Bridgers with Julien Baker", "2026-12-01")]
            ),
        },
    )
    artists = [
        _artist("spPB", "Phoebe Bridgers"),
        _artist("spJB", "Julien Baker"),
        _artist("spN", "Nobody Tours"),
        _artist("spAir", "Air"),
        _artist("spQY", "Quiet Year"),
    ]

    report = await _find(fake, artists)

    assert [s.event_id for s in report.shows] == ["E1", "E2"]  # by date; cancelled dropped
    first, shared = report.shows
    assert first.distance_miles == pytest.approx(20.0 / 1.609344)
    assert (first.venue, first.city, first.region, first.country) == (
        "Hollywood Bowl",
        "Los Angeles",
        "CA",
        "US",
    )
    # One event, two of your artists on the bill: listed once, as sure as
    # the weaker of the two matches.
    assert shared.artists == ("Phoebe Bridgers", "Julien Baker")
    assert shared.confidence == "name"

    assert report.ambiguous == [("Air", 2)]
    assert report.not_on_ticketmaster == ["Nobody Tours"]
    assert {a.artist.name for a in report.matched} == {
        "Phoebe Bridgers",
        "Julien Baker",
        "Quiet Year",
    }
    # Five searches, and events only for the two matched acts with anything
    # upcoming: Quiet Year's zero saves a request.
    assert [q["attractionId"] for q in fake.queries("events.json")] == ["PB", "JB"]
    assert report.requests == 7


async def test_event_search_parameters_follow_the_discovery_api():
    fake = FakeTicketmaster(
        attractions={"Phoebe Bridgers": [_attraction("PB", "Phoebe Bridgers", spotify="spPB")]},
        events={},
    )
    await _find(fake, [_artist("spPB", "Phoebe Bridgers")], days=30)

    (search,) = fake.queries("attractions.json")
    assert search["classificationName"] == "music"
    (events,) = fake.queries("events.json")
    assert events["geoPoint"] == geohash(34.0522, -118.2437)
    assert (events["radius"], events["unit"]) == ("50", "miles")
    # Whole seconds with a Z: Ticketmaster rejects fractional seconds.
    assert events["startDateTime"] == "2026-10-22T18:30:15Z"
    assert events["endDateTime"] == "2026-11-21T18:30:15Z"
    assert events["sort"] == "date,asc"
    assert "latlong" not in events  # deprecated in favour of geoPoint


async def test_more_events_than_one_page_is_reported():
    fake = FakeTicketmaster(
        attractions={"Residency": [_attraction("R", "Residency", spotify="spR")]},
        events={"R": _page("events", [_event("E1", "Residency", "2026-11-01")], total=140)},
    )
    report = await _find(fake, [_artist("spR", "Residency")])
    assert report.truncated == ["Residency"]


@pytest.mark.parametrize(
    "url",
    [
        "https://www.ticketmaster.com/event/E1",
        "https://ticketmaster.com/event/E1",
        "https://concerts.livenation.com/event/E1",
    ],
)
def test_ticket_pages_on_ticketmaster_and_live_nation_are_linked(url):
    assert shows.safe_event_url(url) == url


def test_a_linked_url_is_rebuilt_from_its_checked_parts():
    # The fragment is not a part that was checked, so it does not survive.
    url = "https://www.ticketmaster.com/event/E1?src=api#x"
    assert shows.safe_event_url(url) == "https://www.ticketmaster.com/event/E1?src=api"


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "JAVASCRIPT://www.ticketmaster.com/%0aalert(1)",
        "http://www.ticketmaster.com/event/E1",  # not https
        "https://evil.example/event/E1",
        "https://ticketmaster.com.evil.example/event/E1",
        "https://evilticketmaster.com/event/E1",  # suffix without the dot
        "https://www.ticketmaster.com@evil.example/event/E1",  # credentials disguise the host
        "https://user@www.ticketmaster.com/event/E1",
        "https://www.ticketmaster.com/event/E1\x1b]8;;https://evil.example\x1b\\",
        "https://www.ticketmaster.com/event/\x07E1",
        "https://www.ticketmaster.com/event/E1 https://evil.example",
        "https://www.ticketmaster.com/event/‮E1",  # bidi override
        "https://[::1/event",  # unparseable
        # Parser differentials: a browser and urlsplit can disagree on the host.
        "https://evil.com\\@www.ticketmaster.com/",
        "https://www.ticketmaster.com\\.evil.com/",
        "https://evil.com\\www.ticketmaster.com/",
        "https://www.ticketmaster.com/event\\E1",  # any backslash at all
        "https://:pw@www.ticketmaster.com/",
        "https://@www.ticketmaster.com/",
        "https://www.ticketmaster.com:443/event/E1",  # ports, even the default
        "https://www.ticketmaster.com:8443/event/E1",
        "https://www.ticketmaster.com:/event/E1",
        # Hosts must be plain lowercase ASCII before the suffix is checked.
        "https://WWW.TICKETMASTER.COM/event/E1",
        "https://www.tіcketmaster.com/event/E1",  # Cyrillic і
        "https://xn--evil.ticketmaster.com_/event/E1",
        "https://[::1]/event/E1",
        "https://www.ticketmaster.com%2eevil.com/event/E1",
        "",
        None,
        42,
    ],
)
def test_anything_else_is_not_linked(url):
    assert shows.safe_event_url(url) is None


async def test_a_hostile_event_loses_its_link_and_its_control_characters():
    event = _event(
        "E1", "Real Show\x1b]8;;https://evil.example\x1b\\click\x1b]8;;\x1b\\", "2026-11-01"
    )
    event["url"] = "javascript:alert(1)"
    event["_embedded"]["venues"][0]["name"] = "The \x1b[31mRed\x1b[0m Room‮"
    fake = FakeTicketmaster(
        attractions={"X": [_attraction("X", "X", spotify="spX")]},
        events={"X": _page("events", [event])},
    )

    (show,) = (await _find(fake, [_artist("spX", "X")])).shows

    assert show.url is None
    assert "\x1b" not in show.name and show.name.startswith("Real Show")
    assert show.venue == "The [31mRed[0m Room"


async def test_artist_names_lose_control_characters_too():
    sp = _TopArtists({"short_term": [("a", "Nice\x1b[2JName")]})
    (artist,) = await artists_you_play(sp)
    assert artist.name == "Nice[2JName"


async def test_a_rejected_key_says_so():
    def handler(request):
        return httpx.Response(401, json={"fault": {"faultstring": "Invalid ApiKey"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(TicketmasterError, match="rejected the API key"):
            await find_shows([_artist("x", "X")], HOME, 90, KEY, client=client, now=NOW)


async def test_a_rate_limit_spike_is_retried_once_and_a_spent_quota_reported():
    answers = iter([429, 200, 429, 429])
    calls = []

    def handler(request):
        calls.append(request)
        status = next(answers)
        return httpx.Response(status, json=_page("attractions", []) if status == 200 else {})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await find_shows([_artist("x", "X")], HOME, 90, KEY, client=client, now=NOW)
        assert report.not_on_ticketmaster == ["X"]
        with pytest.raises(TicketmasterError, match="5,000 requests a day"):
            await find_shows([_artist("x", "X")], HOME, 90, KEY, client=client, now=NOW)
    assert len(calls) == 4


async def test_no_key_explains_how_to_get_one_before_asking_spotify():
    with pytest.raises(TicketmasterError) as caught:
        await upcoming_shows(object(), HOME, 90, "")  # any Spotify call would fail
    assert str(caught.value) == HOW_TO_GET_A_KEY
    assert "SPOTIFYFORGE_TICKETMASTER_API_KEY" in HOW_TO_GET_A_KEY


# ---------------------------------------------------------------------------
# The artists you play
# ---------------------------------------------------------------------------


class _TopArtists:
    def __init__(self, by_range: dict[str, list[tuple[str, str]]]) -> None:
        self.by_range = by_range

    async def current_user_top_artists(self, time_range: str, limit: int):
        items = [SimpleNamespace(id=i, name=n) for i, n in self.by_range.get(time_range, [])]
        return SimpleNamespace(items=items[:limit])


def _play(artist_id: str, artist: str) -> Play:
    return Play(
        played_at="2026-10-20T10:00:00+00:00",
        track_id="t",
        name="Song",
        artists=(artist,),
        artist_ids=(artist_id,),
        album="Album",
        album_id=None,
        duration_ms=200_000,
    )


async def test_artists_you_play_ranks_steady_favourites_first_then_the_log():
    sp = _TopArtists(
        {
            "short_term": [("new", "New Crush"), ("steady", "Steady")],
            "medium_term": [("steady", "Steady")],
            "long_term": [("old", "Old Love"), ("steady", "Steady")],
        }
    )
    plays = [_play("logged", "Logged Only")] * 3 + [_play("once", "Played Once")]

    artists = await artists_you_play(sp, plays)

    assert [a.name for a in artists] == ["Steady", "New Crush", "Old Love", "Logged Only"]
    assert artists[0].ranges == ("short_term", "medium_term", "long_term")
    assert artists[-1].logged_plays == 3 and artists[-1].ranges == ()


async def test_artists_you_play_respects_the_limit():
    sp = _TopArtists({"short_term": [(f"a{i}", f"Artist {i}") for i in range(10)]})
    assert len(await artists_you_play(sp, limit=4)) == 4
