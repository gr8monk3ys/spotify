"""Tests for the smart curation rule engine."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from spotifyforge.core.curation_engine import (
    apply_rules,
    dry_run,
    track_matches_conditions,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _track(
    tid: int = 1,
    name: str = "Track",
    popularity: int = 50,
    spotify_id: str = "",
    added_at: datetime | None = None,
    duration_ms: int = 200000,
    genres: list[str] | None = None,
) -> dict:
    return {
        "id": tid,
        "name": name,
        "popularity": popularity,
        "spotify_id": spotify_id or f"sp_{tid}",
        "added_at": (added_at or datetime.now(UTC)).isoformat(),
        "duration_ms": duration_ms,
        "genres": genres or [],
    }


def _af(
    tid: int = 1,
    energy: float = 0.7,
    danceability: float = 0.6,
    valence: float = 0.5,
    tempo: float = 120.0,
    acousticness: float = 0.1,
    instrumentalness: float = 0.0,
    speechiness: float = 0.05,
    liveness: float = 0.1,
) -> dict:
    return {
        "energy": energy,
        "danceability": danceability,
        "valence": valence,
        "tempo": tempo,
        "acousticness": acousticness,
        "instrumentalness": instrumentalness,
        "speechiness": speechiness,
        "liveness": liveness,
    }


def _rule(
    name: str = "test_rule",
    rule_type: str = "filter",
    conditions: dict | None = None,
    actions: dict | None = None,
    priority: int = 0,
    enabled: bool = True,
) -> dict:
    return {
        "name": name,
        "rule_type": rule_type,
        "conditions": conditions or {},
        "actions": actions or {},
        "priority": priority,
        "enabled": enabled,
    }


# ---------------------------------------------------------------------------
# Condition matching tests
# ---------------------------------------------------------------------------


class TestConditionMatching:
    def test_popularity_below(self):
        track = _track(popularity=20)
        assert track_matches_conditions(track, None, {"popularity_below": 30})
        assert not track_matches_conditions(track, None, {"popularity_below": 15})

    def test_popularity_above(self):
        track = _track(popularity=60)
        assert track_matches_conditions(track, None, {"popularity_above": 50})
        assert not track_matches_conditions(track, None, {"popularity_above": 70})

    def test_popularity_between(self):
        track = _track(popularity=50)
        assert track_matches_conditions(track, None, {"popularity_between": [40, 60]})
        assert not track_matches_conditions(track, None, {"popularity_between": [60, 80]})

    def test_energy_range(self):
        af = _af(energy=0.7)
        track = _track()
        assert track_matches_conditions(track, af, {"energy_range": [0.5, 0.9]})
        assert not track_matches_conditions(track, af, {"energy_range": [0.8, 1.0]})

    def test_energy_range_no_audio_features(self):
        track = _track()
        assert not track_matches_conditions(track, None, {"energy_range": [0.5, 0.9]})

    def test_tempo_range(self):
        af = _af(tempo=120.0)
        track = _track()
        assert track_matches_conditions(track, af, {"tempo_range": [100, 140]})
        assert not track_matches_conditions(track, af, {"tempo_range": [130, 160]})

    def test_genre_match(self):
        track = _track(genres=["indie rock", "alternative"])
        assert track_matches_conditions(track, None, {"genre_match": "indie"})
        assert not track_matches_conditions(track, None, {"genre_match": "metal"})

    def test_added_before_days(self):
        old_track = _track(added_at=datetime.now(UTC) - timedelta(days=30))
        assert track_matches_conditions(old_track, None, {"added_before_days": 14})
        new_track = _track(added_at=datetime.now(UTC) - timedelta(days=3))
        assert not track_matches_conditions(new_track, None, {"added_before_days": 14})

    def test_added_after_days(self):
        new_track = _track(added_at=datetime.now(UTC) - timedelta(days=3))
        assert track_matches_conditions(new_track, None, {"added_after_days": 7})
        old_track = _track(added_at=datetime.now(UTC) - timedelta(days=30))
        assert not track_matches_conditions(old_track, None, {"added_after_days": 7})

    def test_duration_conditions(self):
        track = _track(duration_ms=300000)  # 5 minutes
        assert track_matches_conditions(track, None, {"duration_above_ms": 200000})
        assert not track_matches_conditions(track, None, {"duration_above_ms": 400000})
        assert track_matches_conditions(track, None, {"duration_below_ms": 400000})

    def test_multiple_conditions_all_must_match(self):
        track = _track(popularity=60)
        af = _af(energy=0.8)
        conditions = {"popularity_above": 50, "energy_range": [0.7, 0.9]}
        assert track_matches_conditions(track, af, conditions)

        # Fails if one doesn't match
        conditions2 = {"popularity_above": 70, "energy_range": [0.7, 0.9]}
        assert not track_matches_conditions(track, af, conditions2)

    def test_unknown_condition_is_permissive(self):
        track = _track()
        assert track_matches_conditions(track, None, {"future_condition": "value"})

    def test_empty_conditions_always_match(self):
        track = _track()
        assert track_matches_conditions(track, None, {})


# ---------------------------------------------------------------------------
# Rule application tests
# ---------------------------------------------------------------------------


class TestFilterRule:
    def test_keep_mode(self):
        tracks = [_track(tid=1, popularity=70), _track(tid=2, popularity=30)]
        af_map = {}
        rules = [_rule(rule_type="filter", conditions={"popularity_above": 50}, actions={"mode": "keep"})]
        result, log = apply_rules(tracks, af_map, rules)
        assert len(result) == 1
        assert result[0]["id"] == 1
        assert log[0]["status"] == "applied"

    def test_remove_mode(self):
        tracks = [_track(tid=1, popularity=70), _track(tid=2, popularity=30)]
        rules = [_rule(rule_type="filter", conditions={"popularity_below": 40}, actions={"mode": "remove"})]
        result, log = apply_rules(tracks, {}, rules)
        assert len(result) == 1
        assert result[0]["id"] == 1


class TestSortRule:
    def test_sort_by_popularity_desc(self):
        tracks = [_track(tid=1, popularity=30), _track(tid=2, popularity=80), _track(tid=3, popularity=50)]
        rules = [_rule(rule_type="sort", actions={"sort_by": "popularity", "order": "desc"})]
        result, _ = apply_rules(tracks, {}, rules)
        assert [t["id"] for t in result] == [2, 3, 1]

    def test_sort_by_popularity_asc(self):
        tracks = [_track(tid=1, popularity=80), _track(tid=2, popularity=30)]
        rules = [_rule(rule_type="sort", actions={"sort_by": "popularity", "order": "asc"})]
        result, _ = apply_rules(tracks, {}, rules)
        assert [t["id"] for t in result] == [2, 1]

    def test_sort_by_audio_feature(self):
        tracks = [_track(tid=1), _track(tid=2)]
        af_map = {1: _af(tid=1, energy=0.3), 2: _af(tid=2, energy=0.9)}
        rules = [_rule(rule_type="sort", actions={"sort_by": "energy", "order": "desc"})]
        result, _ = apply_rules(tracks, af_map, rules)
        assert [t["id"] for t in result] == [2, 1]


class TestLimitRule:
    def test_limit_tracks(self):
        tracks = [_track(tid=i) for i in range(10)]
        rules = [_rule(rule_type="limit", actions={"limit": 5})]
        result, _ = apply_rules(tracks, {}, rules)
        assert len(result) == 5

    def test_limit_larger_than_list(self):
        tracks = [_track(tid=i) for i in range(3)]
        rules = [_rule(rule_type="limit", actions={"limit": 10})]
        result, _ = apply_rules(tracks, {}, rules)
        assert len(result) == 3


class TestDeduplicateRule:
    def test_removes_duplicates(self):
        tracks = [
            _track(tid=1, spotify_id="sp_a"),
            _track(tid=2, spotify_id="sp_b"),
            _track(tid=3, spotify_id="sp_a"),  # duplicate
        ]
        rules = [_rule(rule_type="deduplicate")]
        result, _ = apply_rules(tracks, {}, rules)
        assert len(result) == 2
        assert result[0]["spotify_id"] == "sp_a"
        assert result[1]["spotify_id"] == "sp_b"


class TestRemoveRule:
    def test_remove_low_popularity(self):
        tracks = [_track(tid=1, popularity=10), _track(tid=2, popularity=60)]
        rules = [_rule(rule_type="remove_tracks", conditions={"popularity_below": 20})]
        result, _ = apply_rules(tracks, {}, rules)
        assert len(result) == 1
        assert result[0]["id"] == 2


class TestAddRule:
    def test_add_candidates(self):
        tracks = [_track(tid=1, spotify_id="sp_1")]
        candidates = [_track(tid=10, spotify_id="sp_10"), _track(tid=11, spotify_id="sp_11")]
        rules = [_rule(rule_type="add_tracks", actions={"candidates": candidates, "max_add": 2})]
        result, _ = apply_rules(tracks, {}, rules)
        assert len(result) == 3

    def test_add_skips_existing(self):
        tracks = [_track(tid=1, spotify_id="sp_1")]
        candidates = [_track(tid=10, spotify_id="sp_1")]  # already in list
        rules = [_rule(rule_type="add_tracks", actions={"candidates": candidates, "max_add": 5})]
        result, _ = apply_rules(tracks, {}, rules)
        assert len(result) == 1


class TestRulePipeline:
    def test_rules_chain_in_priority_order(self):
        tracks = [_track(tid=i, popularity=i * 10) for i in range(1, 11)]
        rules = [
            _rule(name="filter_low", rule_type="remove_tracks", conditions={"popularity_below": 30}, priority=1),
            _rule(name="sort", rule_type="sort", actions={"sort_by": "popularity", "order": "desc"}, priority=2),
            _rule(name="limit", rule_type="limit", actions={"limit": 3}, priority=3),
        ]
        result, log = apply_rules(tracks, {}, rules)
        assert len(result) == 3
        assert result[0]["popularity"] == 100  # highest after sort
        assert len(log) == 3
        assert all(entry["status"] == "applied" for entry in log)

    def test_disabled_rules_are_skipped(self):
        tracks = [_track(tid=1, popularity=50)]
        rules = [_rule(rule_type="limit", actions={"limit": 0}, enabled=False)]
        result, log = apply_rules(tracks, {}, rules)
        assert len(result) == 1  # rule was skipped
        assert len(log) == 0

    def test_unknown_rule_type_is_skipped(self):
        tracks = [_track()]
        rules = [_rule(rule_type="unknown_type")]
        result, log = apply_rules(tracks, {}, rules)
        assert len(result) == 1
        assert len(log) == 0


class TestDryRun:
    def test_dry_run_report(self):
        tracks = [
            _track(tid=1, popularity=80, spotify_id="sp_1"),
            _track(tid=2, popularity=20, spotify_id="sp_2"),
        ]
        rules = [_rule(rule_type="remove_tracks", conditions={"popularity_below": 30})]
        report = dry_run(tracks, {}, rules)
        assert report["original_count"] == 2
        assert report["result_count"] == 1
        assert report["removed_count"] == 1
        assert "sp_2" in report["removed_track_ids"]

    def test_dry_run_empty_tracks(self):
        rules = [_rule(rule_type="limit", actions={"limit": 5})]
        report = dry_run([], {}, rules)
        assert report["original_count"] == 0
        assert report["result_count"] == 0


# ---------------------------------------------------------------------------
# Edge cases for coverage
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_added_before_days_missing_added_at(self):
        track = {"id": 1, "name": "T", "popularity": 50}
        assert not track_matches_conditions(track, None, {"added_before_days": 7})

    def test_added_after_days_missing_added_at(self):
        track = {"id": 1, "name": "T", "popularity": 50}
        assert not track_matches_conditions(track, None, {"added_after_days": 7})

    def test_added_before_days_invalid_date_string(self):
        track = {"id": 1, "name": "T", "popularity": 50, "added_at": "not-a-date"}
        assert not track_matches_conditions(track, None, {"added_before_days": 7})

    def test_added_after_days_invalid_date_string(self):
        track = {"id": 1, "name": "T", "popularity": 50, "added_at": "not-a-date"}
        assert not track_matches_conditions(track, None, {"added_after_days": 7})

    def test_added_before_days_naive_datetime(self):
        """Naive datetimes (no tzinfo) should be handled."""
        from datetime import datetime

        old = datetime(2020, 1, 1)
        track = {"id": 1, "name": "T", "popularity": 50, "added_at": old.isoformat()}
        assert track_matches_conditions(track, None, {"added_before_days": 7})

    def test_rule_error_is_logged(self):
        """A rule that raises internally should be captured in the eval log."""
        tracks = [_track(tid=1)]
        # Force an error by giving bad conditions type via a filter with bad data
        bad_rule = _rule(
            name="bad_rule",
            rule_type="filter",
            conditions={"energy_range": "not-a-list"},  # should be [lo, hi]
        )
        result, log = apply_rules(tracks, {1: _af()}, [bad_rule])
        assert len(log) == 1
        assert log[0]["status"] == "error"

    def test_sort_by_missing_audio_feature(self):
        """Sort by audio feature when track has no features."""
        tracks = [_track(tid=1), _track(tid=2)]
        af_map = {}  # no audio features
        rules = [_rule(rule_type="sort", actions={"sort_by": "energy", "order": "asc"})]
        result, _ = apply_rules(tracks, af_map, rules)
        assert len(result) == 2

    def test_sort_by_non_numeric_field(self):
        """Sort by a field that's not numeric (e.g. name)."""
        tracks = [_track(tid=1, name="Zebra"), _track(tid=2, name="Apple")]
        rules = [_rule(rule_type="sort", actions={"sort_by": "name", "order": "asc"})]
        result, _ = apply_rules(tracks, {}, rules)
        assert len(result) == 2

    def test_sort_missing_field_value(self):
        """Tracks with None values for the sort field should not crash."""
        tracks = [{"id": 1, "name": "T", "popularity": None}, _track(tid=2, popularity=50)]
        rules = [_rule(rule_type="sort", actions={"sort_by": "popularity", "order": "desc"})]
        result, _ = apply_rules(tracks, {}, rules)
        assert len(result) == 2

    def test_audio_feature_range_missing_value(self):
        """Audio feature range check when the feature value is None."""
        af = {"energy": None, "danceability": 0.5}
        track = _track()
        assert not track_matches_conditions(track, af, {"energy_range": [0.3, 0.8]})

    def test_add_tracks_max_add_zero(self):
        """max_add=0 should add nothing."""
        tracks = [_track(tid=1)]
        candidates = [_track(tid=10, spotify_id="sp_10")]
        rules = [_rule(rule_type="add_tracks", actions={"candidates": candidates, "max_add": 0})]
        result, _ = apply_rules(tracks, {}, rules)
        assert len(result) == 1

    def test_add_tracks_empty_candidates(self):
        tracks = [_track(tid=1)]
        rules = [_rule(rule_type="add_tracks", actions={"candidates": [], "max_add": 5})]
        result, _ = apply_rules(tracks, {}, rules)
        assert len(result) == 1
