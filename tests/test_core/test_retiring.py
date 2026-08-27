"""Tests for retiring playlists (core/retiring).

Retiring is the one irreversible-in-practice operation in this tool, and
the failure that matters is retiring something a person made by hand. So
these are almost entirely about what must be left alone.
"""

from __future__ import annotations

import pytest

from spotifyforge.core.curation import _DESCRIPTION_TEMPLATES
from spotifyforge.core.retiring import plan_retirements, retire_playlists, was_forged


@pytest.mark.parametrize("template", _DESCRIPTION_TEMPLATES)
def test_every_description_this_code_writes_is_recognised(template):
    """Built from the templates, so adding one cannot silently make its
    playlists unretirable."""
    assert was_forged(template.format(genre="shoegaze", artists="Slowdive and Ride"))


def test_the_decade_and_dj_set_suffixes_do_not_hide_a_forged_description():
    text = (
        "the hard bop shelf, filed with care. Art Blakey."
        " all from the 1950s. mixed by key, like a dj set."
    )
    assert was_forged(text)


@pytest.mark.parametrize(
    "description",
    [
        "I have dementia",
        "Monke",
        "A place to study",
        "Freaky Playlist",
        "Jungle Terror, House, general EDM",
        "This will make you fall in luv",
        "",
        "   ",
    ],
)
def test_a_description_a_person_typed_is_never_forged(description):
    """These are real descriptions from the account's personal
    playlists. Any one of them matching would retire it."""
    assert not was_forged(description)


def test_only_unplanned_forged_playlists_are_retirable():
    live = {
        "hard techno | techno | tekno": "the hard techno shelf, filed with care. Raxeller.",
        "techno | hard techno | tekno": "Natte Visstick. techno, end to end.",
        "the reptile house": "Jungle Terror, House, general EDM",
        "marble rooms": "I have dementia",
    }
    retirable, kept = plan_retirements(live, planned_titles=["hard techno | techno | tekno"])

    assert retirable == ["techno | hard techno | tekno"]
    assert kept == ["marble rooms", "the reptile house"]


def test_a_planned_playlist_is_never_retirable_even_if_forged():
    live = {"bebop at 3am": "a long sit with bebop: Charlie Parker, more."}

    retirable, kept = plan_retirements(live, planned_titles=["bebop at 3am"])

    assert (retirable, kept) == ([], [])


async def test_retire_unfollows_and_reports_what_it_could_not(fake_spotify, client_for):
    fake_spotify.add_user("user1")
    fake_spotify.add_playlist("pl1", name="techno | hard techno | tekno")
    sp = client_for("user1")

    retired, failed = await retire_playlists(
        sp,
        {"techno | hard techno | tekno": "pl1"},
        ["techno | hard techno | tekno", "never existed"],
        delay=0,
    )

    assert retired == ["techno | hard techno | tekno"]
    assert failed == ["never existed"]
