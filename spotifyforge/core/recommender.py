"""Content-based music recommendation engine for SpotifyForge.

Uses audio features to compute track similarity and generate recommendations.
Implements cosine similarity on audio feature vectors with Maximal Marginal
Relevance (MMR) for diversity-aware selection.

All public functions are pure — no Spotify API or database calls.
"""

from __future__ import annotations

import logging
import math
from typing import Any

logger = logging.getLogger(__name__)

# Audio feature keys used for similarity computation, with weights
FEATURE_WEIGHTS: dict[str, float] = {
    "energy": 1.0,
    "danceability": 1.0,
    "valence": 1.0,
    "acousticness": 0.8,
    "instrumentalness": 0.7,
    "speechiness": 0.6,
    "liveness": 0.5,
    "tempo": 0.4,  # normalized to 0-1 range
}

TEMPO_MIN = 40.0
TEMPO_MAX = 220.0

TrackData = dict[str, Any]
AudioFeaturesData = dict[str, Any]


# ---------------------------------------------------------------------------
# Feature vector operations
# ---------------------------------------------------------------------------


def build_feature_vector(af: AudioFeaturesData) -> list[float]:
    """Build a weighted feature vector from audio features dict."""
    vec: list[float] = []
    for key, weight in FEATURE_WEIGHTS.items():
        val = af.get(key)
        if val is None:
            vec.append(0.0)
            continue
        # Normalize tempo to 0-1 range
        if key == "tempo":
            val = max(0.0, min(1.0, (val - TEMPO_MIN) / (TEMPO_MAX - TEMPO_MIN)))
        vec.append(float(val) * weight)
    return vec


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def euclidean_distance(a: list[float], b: list[float]) -> float:
    """Compute Euclidean distance between two vectors."""
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


# ---------------------------------------------------------------------------
# Taste profile
# ---------------------------------------------------------------------------


def compute_taste_profile(
    audio_features_list: list[AudioFeaturesData],
) -> dict[str, float]:
    """Aggregate audio features into a user taste profile.

    Computes the mean of each audio feature across the provided tracks.
    Returns a dict suitable for passing to recommendation functions.
    """
    if not audio_features_list:
        return {}

    totals: dict[str, float] = {}
    counts: dict[str, int] = {}

    for af in audio_features_list:
        for key in FEATURE_WEIGHTS:
            val = af.get(key)
            if val is not None:
                totals[key] = totals.get(key, 0.0) + float(val)
                counts[key] = counts.get(key, 0) + 1

    return {k: round(totals[k] / counts[k], 4) for k in totals if counts.get(k, 0) > 0}


# ---------------------------------------------------------------------------
# Similarity scoring
# ---------------------------------------------------------------------------


def score_track_similarity(
    target_profile: AudioFeaturesData,
    candidate_af: AudioFeaturesData,
    candidate_track: TrackData | None = None,
    freshness_weight: float = 0.1,
    popularity_weight: float = 0.05,
) -> float:
    """Score how similar a candidate track is to a target profile.

    Returns a score between 0.0 and 1.0 where higher = more similar.

    Parameters
    ----------
    target_profile:
        The reference audio feature profile (e.g., from a playlist or taste).
    candidate_af:
        Audio features of the candidate track.
    candidate_track:
        Optional track metadata for freshness/popularity bonuses.
    freshness_weight:
        Weight for release freshness bonus (0.0–1.0).
    popularity_weight:
        Weight for popularity bonus (0.0–1.0).
    """
    vec_target = build_feature_vector(target_profile)
    vec_candidate = build_feature_vector(candidate_af)

    similarity = cosine_similarity(vec_target, vec_candidate)

    # Clamp to [0, 1]
    score = max(0.0, min(1.0, similarity))

    # Optional freshness bonus
    if candidate_track and freshness_weight > 0:
        popularity = candidate_track.get("popularity") or 0
        pop_bonus = (popularity / 100.0) * popularity_weight
        score = score * (1.0 - freshness_weight - popularity_weight) + pop_bonus

    return round(score, 4)


# ---------------------------------------------------------------------------
# Recommendation generators
# ---------------------------------------------------------------------------


def recommend_similar_tracks(
    target_af: AudioFeaturesData,
    candidates: list[tuple[TrackData, AudioFeaturesData]],
    exclude_ids: set[str] | None = None,
    limit: int = 20,
    diversity: float = 0.3,
) -> list[dict[str, Any]]:
    """Find tracks similar to a target audio profile.

    Uses MMR (Maximal Marginal Relevance) to balance relevance and diversity.

    Parameters
    ----------
    target_af:
        Target audio features to match against.
    candidates:
        List of (track_data, audio_features) tuples to score.
    exclude_ids:
        Set of spotify_ids to skip (e.g., tracks already in playlist).
    limit:
        Maximum number of recommendations to return.
    diversity:
        MMR diversity parameter (0.0 = pure relevance, 1.0 = max diversity).

    Returns
    -------
    List of recommendation dicts with keys: track, score, reasons.
    """
    exclude = exclude_ids or set()

    # Score all candidates
    scored: list[tuple[TrackData, AudioFeaturesData, float]] = []
    for track, af in candidates:
        sid = track.get("spotify_id", "")
        if sid in exclude:
            continue
        sim = score_track_similarity(target_af, af, track)
        scored.append((track, af, sim))

    if not scored:
        return []

    # MMR selection
    selected = _mmr_select(scored, target_af, limit=limit, lambda_param=1.0 - diversity)

    results = []
    for track, af, score in selected:
        reasons = _explain_similarity(target_af, af)
        results.append({
            "track": track,
            "score": score,
            "reasons": reasons,
        })

    return results


def recommend_playlist_expansion(
    playlist_tracks: list[tuple[TrackData, AudioFeaturesData]],
    candidates: list[tuple[TrackData, AudioFeaturesData]],
    limit: int = 20,
    diversity: float = 0.3,
) -> list[dict[str, Any]]:
    """Recommend tracks to expand a playlist.

    Computes the playlist's aggregate profile and finds similar candidates
    not already in the playlist.

    Parameters
    ----------
    playlist_tracks:
        Existing (track, audio_features) pairs in the playlist.
    candidates:
        Pool of (track, audio_features) pairs to choose from.
    limit:
        Max recommendations.
    diversity:
        MMR diversity factor.
    """
    if not playlist_tracks:
        return []

    # Build playlist profile
    af_list = [af for _, af in playlist_tracks]
    profile = compute_taste_profile(af_list)

    # Exclude tracks already in the playlist
    existing_ids = {t.get("spotify_id", "") for t, _ in playlist_tracks}

    return recommend_similar_tracks(
        target_af=profile,
        candidates=candidates,
        exclude_ids=existing_ids,
        limit=limit,
        diversity=diversity,
    )


# ---------------------------------------------------------------------------
# MMR (Maximal Marginal Relevance)
# ---------------------------------------------------------------------------


def _mmr_select(
    scored: list[tuple[TrackData, AudioFeaturesData, float]],
    target_af: AudioFeaturesData,
    limit: int,
    lambda_param: float = 0.7,
) -> list[tuple[TrackData, AudioFeaturesData, float]]:
    """Select items using Maximal Marginal Relevance.

    Balances relevance to the target with diversity among selected items.

    Parameters
    ----------
    scored:
        Pre-scored (track, af, similarity_score) tuples.
    target_af:
        Target profile for relevance scoring.
    limit:
        Number of items to select.
    lambda_param:
        Trade-off: 1.0 = pure relevance, 0.0 = pure diversity.
    """
    if not scored:
        return []

    # Sort by score descending as a starting point
    remaining = list(scored)
    selected: list[tuple[TrackData, AudioFeaturesData, float]] = []

    while remaining and len(selected) < limit:
        best_idx = -1
        best_mmr = -math.inf

        for i, (track, af, sim) in enumerate(remaining):
            # Relevance term
            relevance = lambda_param * sim

            # Diversity term: max similarity to any already-selected item
            max_sim_to_selected = 0.0
            if selected:
                vec_cand = build_feature_vector(af)
                for _, sel_af, _ in selected:
                    vec_sel = build_feature_vector(sel_af)
                    s = cosine_similarity(vec_cand, vec_sel)
                    max_sim_to_selected = max(max_sim_to_selected, s)

            diversity_penalty = (1.0 - lambda_param) * max_sim_to_selected
            mmr_score = relevance - diversity_penalty

            if mmr_score > best_mmr:
                best_mmr = mmr_score
                best_idx = i

        if best_idx >= 0:
            selected.append(remaining.pop(best_idx))

    return selected


# ---------------------------------------------------------------------------
# Explanation
# ---------------------------------------------------------------------------


def _explain_similarity(
    target: AudioFeaturesData,
    candidate: AudioFeaturesData,
) -> list[str]:
    """Generate human-readable reasons for why a track was recommended."""
    reasons: list[str] = []
    feature_labels = {
        "energy": ("energy", "energetic", "calm"),
        "danceability": ("danceability", "danceable", "less danceable"),
        "valence": ("mood", "upbeat", "melancholic"),
        "acousticness": ("acousticness", "acoustic", "electronic"),
        "instrumentalness": ("instrumentalness", "instrumental", "vocal"),
    }

    close_threshold = 0.15  # features within this range are "matching"

    for key, (label, high_word, low_word) in feature_labels.items():
        t_val = target.get(key)
        c_val = candidate.get(key)
        if t_val is None or c_val is None:
            continue
        diff = abs(t_val - c_val)
        if diff <= close_threshold:
            word = high_word if c_val >= 0.5 else low_word
            reasons.append(f"Similar {label} ({word})")

    if not reasons:
        reasons.append("General audio profile match")

    return reasons
