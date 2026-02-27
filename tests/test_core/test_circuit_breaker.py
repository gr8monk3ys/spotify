"""Tests for the circuit breaker module."""

from __future__ import annotations

import time

import pytest

from spotifyforge.core.circuit_breaker import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    get_breaker,
    reset_all,
)


@pytest.fixture(autouse=True)
def _reset_breakers():
    """Reset global breaker registry between tests."""
    reset_all()
    yield
    reset_all()


class TestCircuitState:
    def test_initial_state_is_closed(self):
        cb = CircuitBreaker("test_svc")
        assert cb.state == CircuitState.CLOSED

    def test_stays_closed_under_threshold(self):
        cb = CircuitBreaker("test_svc", failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.CLOSED

    def test_opens_at_threshold(self):
        cb = CircuitBreaker("test_svc", failure_threshold=3)
        for _ in range(3):
            cb.record_failure()
        assert cb.state == CircuitState.OPEN

    def test_success_resets_failure_count(self):
        cb = CircuitBreaker("test_svc", failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.CLOSED

    def test_half_open_after_cooldown(self):
        cb = CircuitBreaker("test_svc", failure_threshold=1, cooldown_seconds=0.05)
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        time.sleep(0.06)
        assert cb.state == CircuitState.HALF_OPEN

    def test_half_open_success_closes(self):
        cb = CircuitBreaker("test_svc", failure_threshold=1, cooldown_seconds=0.05)
        cb.record_failure()
        time.sleep(0.06)
        assert cb.state == CircuitState.HALF_OPEN
        cb.record_success()
        assert cb.state == CircuitState.CLOSED

    def test_half_open_failure_reopens(self):
        cb = CircuitBreaker("test_svc", failure_threshold=1, cooldown_seconds=0.05)
        cb.record_failure()
        time.sleep(0.06)
        assert cb.state == CircuitState.HALF_OPEN
        cb.record_failure()
        assert cb.state == CircuitState.OPEN


class TestCircuitCheck:
    def test_check_passes_when_closed(self):
        cb = CircuitBreaker("test_svc")
        cb.check()  # should not raise

    def test_check_raises_when_open(self):
        cb = CircuitBreaker("test_svc", failure_threshold=1)
        cb.record_failure()
        with pytest.raises(CircuitOpenError) as exc_info:
            cb.check()
        assert exc_info.value.service == "test_svc"
        assert exc_info.value.retry_after >= 0

    def test_check_passes_when_half_open(self):
        cb = CircuitBreaker("test_svc", failure_threshold=1, cooldown_seconds=0.01)
        cb.record_failure()
        time.sleep(0.02)
        cb.check()  # should not raise


class TestCircuitReset:
    def test_reset_clears_state(self):
        cb = CircuitBreaker("test_svc", failure_threshold=1)
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        cb.reset()
        assert cb.state == CircuitState.CLOSED
        assert cb._failure_count == 0


class TestStats:
    def test_stats_returns_dict(self):
        cb = CircuitBreaker("test_svc", failure_threshold=5, cooldown_seconds=30.0)
        stats = cb.stats()
        assert stats["service"] == "test_svc"
        assert stats["state"] == CircuitState.CLOSED
        assert stats["failure_threshold"] == 5
        assert stats["cooldown_seconds"] == 30.0


class TestGlobalRegistry:
    def test_get_breaker_creates_and_caches(self):
        cb1 = get_breaker("service_a")
        cb2 = get_breaker("service_a")
        assert cb1 is cb2

    def test_different_services_get_different_breakers(self):
        cb1 = get_breaker("service_a")
        cb2 = get_breaker("service_b")
        assert cb1 is not cb2

    def test_get_all_breakers(self):
        from spotifyforge.core.circuit_breaker import get_all_breakers

        get_breaker("svc_x")
        get_breaker("svc_y")
        all_b = get_all_breakers()
        assert "svc_x" in all_b
        assert "svc_y" in all_b
