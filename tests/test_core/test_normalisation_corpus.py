"""The golden corpus, now asserting that extraction changed nothing.

`normalise`/`strip_article` used to be a verbatim copy of the same two functions
in two other repos. Nothing at import time made the three agree, so this corpus
did: each repo asserted its own copy against the same 41 cases, and a change
landing in one turned the other two red.

There is one implementation now — `media_core.names`, installed from a pinned
tag — and the corpus has a different job. It is the *regression* test for the
move: every case here passed against this repo's own copy before it was deleted,
and passes against the shared one after. If a future version of media-core
changes what a name normalises to, this file is what says so, in the repo where
a false match would actually save a stranger's record to a real account.

`tests/fixtures/name_normalisation_corpus.json` is still identical byte-for-byte
to the copy media-core ships as its own spec (md5 f941657d…).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from media_core.names import normalise, strip_article

from spotifyforge.core import library

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


def test_library_module_still_exposes_the_shared_functions() -> None:
    """Callers and the matcher import these from `core.library`, not media_core.

    The re-export is the whole adoption: if it ever stopped being the shared
    implementation, this repo would have quietly grown a fourth copy.
    """
    assert library.normalise is normalise
    assert library.strip_article is strip_article
