"""Smart curation rule engine for SpotifyForge.

Evaluates declarative curation rules against a track list and produces a
modified track list.  Rules are executed in priority order (lowest first),
with each rule's output feeding into the next.

All public functions are pure — they operate on dicts/lists with no
database or API calls.
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

TrackData = dict[str, Any]
AudioFeaturesData = dict[str, Any]
RuleDict = dict[str, Any]

# ---------------------------------------------------------------------------
# Rule evaluation pipeline
# ---------------------------------------------------------------------------


def apply_rules(
    tracks: list[TrackData],
    audio_features_map: dict[int, AudioFeaturesData],
    rules: list[RuleDict],
) -> tuple[list[TrackData], list[dict[str, Any]]]:
    """Execute a chain of curation rules on a track list.

    Parameters
    ----------
    tracks:
        List of track dicts (must have ``"id"``, ``"name"``, ``"popularity"``,
        ``"added_at"`` keys at minimum).
    audio_features_map:
        Mapping from track ``id`` → audio features dict (keys like
        ``"energy"``, ``"danceability"``, etc.).
    rules:
        List of rule dicts sorted by priority. Each must have ``"rule_type"``,
        ``"conditions"``, ``"actions"``, and ``"name"`` keys.

    Returns
    -------
    A tuple of (modified track list, evaluation log entries).
    """
    sorted_rules = sorted(rules, key=lambda r: r.get("priority", 0))
    current = list(tracks)
    eval_log: list[dict[str, Any]] = []

    for rule in sorted_rules:
        if not rule.get("enabled", True):
            continue

        before_count = len(current)
        rule_type = rule.get("rule_type", "")
        conditions = rule.get("conditions") or {}
        actions = rule.get("actions") or {}
        rule_name = rule.get("name", "unnamed")

        try:
            if rule_type == "filter":
                current = _apply_filter(current, audio_features_map, conditions, actions)
            elif rule_type == "sort":
                current = _apply_sort(current, audio_features_map, actions)
            elif rule_type == "limit":
                current = _apply_limit(current, actions)
            elif rule_type == "deduplicate":
                current = _apply_deduplicate(current)
            elif rule_type == "remove_tracks":
                current = _apply_remove(current, audio_features_map, conditions)
            elif rule_type == "add_tracks":
                current = _apply_add(current, actions)
            else:
                logger.warning("Unknown rule type '%s' in rule '%s'", rule_type, rule_name)
                continue

            eval_log.append(
                {
                    "rule_name": rule_name,
                    "rule_type": rule_type,
                    "tracks_before": before_count,
                    "tracks_after": len(current),
                    "status": "applied",
                }
            )
        except Exception as exc:
            logger.error("Rule '%s' failed: %s", rule_name, exc)
            eval_log.append(
                {
                    "rule_name": rule_name,
                    "rule_type": rule_type,
                    "tracks_before": before_count,
                    "tracks_after": len(current),
                    "status": "error",
                    "error": str(exc),
                }
            )

    return current, eval_log


# ---------------------------------------------------------------------------
# Condition matchers
# ---------------------------------------------------------------------------


def track_matches_conditions(
    track: TrackData,
    af: AudioFeaturesData | None,
    conditions: dict[str, Any],
) -> bool:
    """Return True if a track matches ALL specified conditions."""
    for key, value in conditions.items():
        if not _check_condition(track, af, key, value):
            return False
    return True


def _check_condition(
    track: TrackData,
    af: AudioFeaturesData | None,
    key: str,
    value: Any,
) -> bool:
    """Evaluate a single condition against a track."""
    pop: int = int(track.get("popularity") or 0)

    if key == "popularity_below":
        return bool(pop < value)
    if key == "popularity_above":
        return bool(pop >= value)
    if key == "popularity_between":
        lo, hi = value
        return bool(lo <= pop <= hi)

    if key == "added_before_days":
        added_at = track.get("added_at")
        if not added_at:
            return False
        if isinstance(added_at, str):
            try:
                added_at = datetime.fromisoformat(added_at)
            except ValueError:
                return False
        if added_at.tzinfo is None:
            added_at = added_at.replace(tzinfo=UTC)
        cutoff = datetime.now(UTC) - timedelta(days=int(value))
        return added_at < cutoff

    if key == "added_after_days":
        added_at = track.get("added_at")
        if not added_at:
            return False
        if isinstance(added_at, str):
            try:
                added_at = datetime.fromisoformat(added_at)
            except ValueError:
                return False
        if added_at.tzinfo is None:
            added_at = added_at.replace(tzinfo=UTC)
        cutoff = datetime.now(UTC) - timedelta(days=int(value))
        return added_at >= cutoff

    # Audio feature range conditions
    audio_feature_ranges = {
        "energy_range",
        "danceability_range",
        "valence_range",
        "tempo_range",
        "acousticness_range",
        "instrumentalness_range",
        "speechiness_range",
        "liveness_range",
    }
    if key in audio_feature_ranges:
        if af is None:
            return False
        feature_name = key.replace("_range", "")
        feat_val = af.get(feature_name)
        if feat_val is None:
            return False
        lo, hi = value
        return bool(lo <= feat_val <= hi)

    # Genre match (checks if any artist genre contains the target)
    if key == "genre_match":
        genres = track.get("genres") or []
        target = value.lower()
        return any(target in g.lower() for g in genres)

    if key == "duration_above_ms":
        return bool(int(track.get("duration_ms") or 0) >= value)
    if key == "duration_below_ms":
        return bool(int(track.get("duration_ms") or 0) < value)

    logger.debug("Unknown condition key: %s", key)
    return True  # unknown conditions are permissive


# ---------------------------------------------------------------------------
# Rule type executors
# ---------------------------------------------------------------------------


def _apply_filter(
    tracks: list[TrackData],
    af_map: dict[int, AudioFeaturesData],
    conditions: dict[str, Any],
    actions: dict[str, Any],
) -> list[TrackData]:
    """Filter rule: keep only tracks matching conditions (or remove matching)."""
    mode = actions.get("mode", "keep")  # "keep" or "remove"

    result = []
    for track in tracks:
        tid = track.get("id")
        af = af_map.get(tid) if tid is not None else None
        matches = track_matches_conditions(track, af, conditions)

        if mode == "keep" and matches:
            result.append(track)
        elif mode == "remove" and not matches:
            result.append(track)

    return result


def _apply_sort(
    tracks: list[TrackData],
    af_map: dict[int, AudioFeaturesData],
    actions: dict[str, Any],
) -> list[TrackData]:
    """Sort rule: reorder tracks by a field."""
    sort_by = actions.get("sort_by", "popularity")
    order = actions.get("order", "desc")
    reverse = order == "desc"

    audio_keys = {
        "energy",
        "danceability",
        "valence",
        "tempo",
        "acousticness",
        "instrumentalness",
        "speechiness",
        "liveness",
        "loudness",
    }

    def sort_key(track: TrackData) -> float:
        if sort_by in audio_keys:
            tid = track.get("id")
            af = af_map.get(tid) if isinstance(tid, int) else None
            if af:
                val = af.get(sort_by)
                if val is not None:
                    return float(val)
            return -math.inf if reverse else math.inf
        val = track.get(sort_by)
        if val is None:
            return -math.inf if reverse else math.inf
        return float(val) if isinstance(val, (int, float)) else 0.0

    return sorted(tracks, key=sort_key, reverse=reverse)


def _apply_limit(
    tracks: list[TrackData],
    actions: dict[str, Any],
) -> list[TrackData]:
    """Limit rule: cap the track count."""
    limit = actions.get("limit", len(tracks))
    return tracks[:limit]


def _apply_deduplicate(tracks: list[TrackData]) -> list[TrackData]:
    """Deduplicate rule: remove tracks with duplicate spotify_id."""
    seen: set[str] = set()
    result: list[TrackData] = []
    for track in tracks:
        sid = track.get("spotify_id", "")
        if sid and sid in seen:
            continue
        if sid:
            seen.add(sid)
        result.append(track)
    return result


def _apply_remove(
    tracks: list[TrackData],
    af_map: dict[int, AudioFeaturesData],
    conditions: dict[str, Any],
) -> list[TrackData]:
    """Remove rule: remove tracks matching conditions."""
    result = []
    for track in tracks:
        tid = track.get("id")
        af = af_map.get(tid) if tid is not None else None
        if not track_matches_conditions(track, af, conditions):
            result.append(track)
    return result


def _apply_add(
    tracks: list[TrackData],
    actions: dict[str, Any],
) -> list[TrackData]:
    """Add rule: append tracks from a candidate pool.

    Expected actions keys:
    - ``candidates``: list of TrackData dicts to potentially add
    - ``max_add``: max number of tracks to add (default 10)
    """
    candidates: list[TrackData] = actions.get("candidates", [])
    max_add = actions.get("max_add", 10)

    existing_ids = {t.get("spotify_id") for t in tracks if t.get("spotify_id")}
    added = 0
    result = list(tracks)

    for candidate in candidates:
        if added >= max_add:
            break
        sid = candidate.get("spotify_id", "")
        if sid and sid not in existing_ids:
            result.append(candidate)
            existing_ids.add(sid)
            added += 1

    return result


# ---------------------------------------------------------------------------
# Dry-run helper
# ---------------------------------------------------------------------------


def dry_run(
    tracks: list[TrackData],
    audio_features_map: dict[int, AudioFeaturesData],
    rules: list[RuleDict],
) -> dict[str, Any]:
    """Execute rules in dry-run mode and return a detailed report.

    Does not modify anything — just shows what *would* happen.
    """
    result_tracks, eval_log = apply_rules(tracks, audio_features_map, rules)

    original_ids = {t.get("spotify_id") for t in tracks}
    result_ids = {t.get("spotify_id") for t in result_tracks}
    removed_ids = original_ids - result_ids
    added_ids = result_ids - original_ids

    return {
        "original_count": len(tracks),
        "result_count": len(result_tracks),
        "removed_count": len(removed_ids),
        "added_count": len(added_ids),
        "eval_log": eval_log,
        "removed_track_ids": list(removed_ids),
        "added_track_ids": list(added_ids),
    }
