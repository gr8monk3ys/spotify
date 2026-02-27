"""Tests for the content-based recommendation engine."""

from __future__ import annotations

import pytest

from spotifyforge.core.recommender import (
    build_feature_vector,
    compute_taste_profile,
    cosine_similarity,
    euclidean_distance,
    recommend_playlist_expansion,
    recommend_similar_tracks,
    score_track_similarity,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _af(
    energy: float = 0.7,
    danceability: float = 0.6,
    valence: float = 0.5,
    acousticness: float = 0.1,
    instrumentalness: float = 0.0,
    speechiness: float = 0.05,
    liveness: float = 0.1,
    tempo: float = 120.0,
) -> dict:
    return {
        "energy": energy,
        "danceability": danceability,
        "valence": valence,
        "acousticness": acousticness,
        "instrumentalness": instrumentalness,
        "speechiness": speechiness,
        "liveness": liveness,
        "tempo": tempo,
    }


def _track(tid: int = 1, name: str = "Track", popularity: int = 50) -> dict:
    return {
        "id": tid,
        "name": name,
        "spotify_id": f"sp_{tid}",
        "popularity": popularity,
    }


# ---------------------------------------------------------------------------
# Feature vector tests
# ---------------------------------------------------------------------------


class TestFeatureVector:
    def test_build_produces_correct_length(self):
        vec = build_feature_vector(_af())
        assert len(vec) == 8  # 8 features

    def test_missing_features_default_to_zero(self):
        vec = build_feature_vector({"energy": 0.5})
        assert vec[0] == 0.5  # energy
        assert vec[1] == 0.0  # danceability (missing)

    def test_tempo_normalized(self):
        vec_low = build_feature_vector(_af(tempo=40.0))  # min
        vec_high = build_feature_vector(_af(tempo=220.0))  # max
        # Tempo is the last element, weighted by 0.4
        assert vec_low[-1] == pytest.approx(0.0, abs=0.01)
        assert vec_high[-1] == pytest.approx(0.4, abs=0.01)


class TestCosineSimilarity:
    def test_identical_vectors(self):
        a = [1.0, 2.0, 3.0]
        assert cosine_similarity(a, a) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        assert cosine_similarity(a, b) == pytest.approx(0.0)

    def test_opposite_vectors(self):
        a = [1.0, 0.0]
        b = [-1.0, 0.0]
        assert cosine_similarity(a, b) == pytest.approx(-1.0)

    def test_zero_vector(self):
        assert cosine_similarity([0, 0], [1, 1]) == 0.0


class TestEuclideanDistance:
    def test_identical_points(self):
        assert euclidean_distance([1, 2], [1, 2]) == 0.0

    def test_known_distance(self):
        assert euclidean_distance([0, 0], [3, 4]) == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# Taste profile tests
# ---------------------------------------------------------------------------


class TestTasteProfile:
    def test_single_track(self):
        profile = compute_taste_profile([_af(energy=0.8, valence=0.6)])
        assert profile["energy"] == 0.8
        assert profile["valence"] == 0.6

    def test_multiple_tracks_averaged(self):
        profile = compute_taste_profile([
            _af(energy=0.6),
            _af(energy=0.8),
        ])
        assert profile["energy"] == pytest.approx(0.7)

    def test_empty_list(self):
        profile = compute_taste_profile([])
        assert profile == {}

    def test_missing_fields_excluded(self):
        profile = compute_taste_profile([{"energy": 0.5}])
        assert "energy" in profile
        assert "danceability" not in profile


# ---------------------------------------------------------------------------
# Similarity scoring tests
# ---------------------------------------------------------------------------


class TestScoreTrackSimilarity:
    def test_identical_features_high_score(self):
        af = _af(energy=0.7, valence=0.5, danceability=0.6)
        score = score_track_similarity(af, af)
        assert score >= 0.9

    def test_different_features_lower_score(self):
        target = _af(energy=0.9, valence=0.9)
        candidate = _af(energy=0.1, valence=0.1)
        score = score_track_similarity(target, candidate)
        assert score < 0.7

    def test_score_between_0_and_1(self):
        for e in [0.0, 0.5, 1.0]:
            for v in [0.0, 0.5, 1.0]:
                target = _af(energy=e, valence=v)
                candidate = _af(energy=1 - e, valence=1 - v)
                score = score_track_similarity(target, candidate)
                assert 0.0 <= score <= 1.0


# ---------------------------------------------------------------------------
# Recommendation tests
# ---------------------------------------------------------------------------


class TestRecommendSimilarTracks:
    def test_returns_sorted_by_score(self):
        target = _af(energy=0.8, danceability=0.7, valence=0.6)
        candidates = [
            (_track(1, "Close Match"), _af(energy=0.75, danceability=0.65, valence=0.55)),
            (_track(2, "Far Match"), _af(energy=0.1, danceability=0.1, valence=0.1)),
            (_track(3, "Medium Match"), _af(energy=0.5, danceability=0.5, valence=0.5)),
        ]
        results = recommend_similar_tracks(target, candidates, limit=3, diversity=0.0)
        assert len(results) == 3
        # Best match should be first (highest score)
        scores = [r["score"] for r in results]
        assert scores[0] >= scores[-1]

    def test_excludes_specified_ids(self):
        target = _af()
        candidates = [
            (_track(1), _af()),
            (_track(2), _af()),
        ]
        results = recommend_similar_tracks(target, candidates, exclude_ids={"sp_1"})
        assert len(results) == 1
        assert results[0]["track"]["spotify_id"] == "sp_2"

    def test_respects_limit(self):
        target = _af()
        candidates = [(_track(i), _af()) for i in range(20)]
        results = recommend_similar_tracks(target, candidates, limit=5)
        assert len(results) == 5

    def test_empty_candidates(self):
        results = recommend_similar_tracks(_af(), [], limit=5)
        assert results == []

    def test_results_include_reasons(self):
        target = _af(energy=0.8)
        candidates = [(_track(1), _af(energy=0.78))]
        results = recommend_similar_tracks(target, candidates)
        assert len(results) == 1
        assert isinstance(results[0]["reasons"], list)
        assert len(results[0]["reasons"]) > 0

    def test_diversity_increases_variety(self):
        target = _af(energy=0.8, valence=0.8)
        # All similar candidates
        candidates = [
            (_track(i), _af(energy=0.75 + i * 0.01, valence=0.75 + i * 0.01))
            for i in range(10)
        ]
        results_no_div = recommend_similar_tracks(target, candidates, limit=5, diversity=0.0)
        results_high_div = recommend_similar_tracks(target, candidates, limit=5, diversity=0.8)
        # Both should return 5 results
        assert len(results_no_div) == 5
        assert len(results_high_div) == 5


class TestRecommendPlaylistExpansion:
    def test_expansion_excludes_existing(self):
        existing = [
            (_track(1, "T1"), _af(energy=0.7)),
            (_track(2, "T2"), _af(energy=0.8)),
        ]
        candidates = [
            (_track(3, "T3"), _af(energy=0.75)),
            (_track(1, "T1 dup"), _af(energy=0.7)),  # same tid=1 → same sp_1
        ]
        results = recommend_playlist_expansion(existing, candidates, limit=5)
        assert len(results) == 1
        assert results[0]["track"]["spotify_id"] == "sp_3"

    def test_empty_playlist_returns_empty(self):
        results = recommend_playlist_expansion([], [(_track(1), _af())], limit=5)
        assert results == []
