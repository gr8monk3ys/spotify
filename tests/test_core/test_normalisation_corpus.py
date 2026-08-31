"""The golden corpus that keeps three independent copies of `normalise` agreeing.

`normalise`/`strip_article` are duplicated verbatim in three repos that share
a file format, not code: `rym/src/rym/match.py` (canonical),
`discogs/src/discogs/spotify/names.py` and
`spotify/spotifyforge/core/library.py`. Nothing at import time makes them
agree, so this corpus does: each repo asserts its own copy against the same
fixture.

`tests/fixtures/name_normalisation_corpus.json` IS DUPLICATED ON PURPOSE and
must be updated in all three repos together. A change landing in one repo
alone turns the other two red, which is the point — a silent drift in
normalisation makes a false match, and a false match hides a record forever.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from spotifyforge.core.library import normalise, strip_article

CORPUS = json.loads(
    (Path(__file__).parent / ".." / "fixtures" / "name_normalisation_corpus.json").read_text(
        encoding="utf-8"
    )
)


@pytest.mark.parametrize("case", CORPUS, ids=[c["input"] or "<empty>" for c in CORPUS])
def test_normalise_matches_corpus(case: dict[str, str]) -> None:
    assert normalise(case["input"]) == case["normalised"]


@pytest.mark.parametrize("case", CORPUS, ids=[c["input"] or "<empty>" for c in CORPUS])
def test_strip_article_matches_corpus(case: dict[str, str]) -> None:
    assert strip_article(case["input"]) == case["stripped_article"]


def test_corpus_is_not_empty() -> None:
    """A corpus that silently emptied would assert nothing at all."""
    assert len(CORPUS) >= 30
