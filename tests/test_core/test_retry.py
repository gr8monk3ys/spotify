"""Tests for the retry module."""

from __future__ import annotations

import pytest

from spotifyforge.core.retry import RetryExhaustedError, _compute_delay, with_retry


class TestComputeDelay:
    def test_base_delay_attempt_1(self):
        delay = _compute_delay(attempt=1, base_delay=1.0, max_delay=30.0, jitter=0.0)
        assert delay == 1.0

    def test_exponential_backoff(self):
        d1 = _compute_delay(attempt=1, base_delay=1.0, max_delay=30.0, jitter=0.0)
        d2 = _compute_delay(attempt=2, base_delay=1.0, max_delay=30.0, jitter=0.0)
        d3 = _compute_delay(attempt=3, base_delay=1.0, max_delay=30.0, jitter=0.0)
        assert d1 == 1.0
        assert d2 == 2.0
        assert d3 == 4.0

    def test_max_delay_cap(self):
        delay = _compute_delay(attempt=10, base_delay=1.0, max_delay=5.0, jitter=0.0)
        assert delay == 5.0

    def test_jitter_adds_variability(self):
        delays = set()
        for _ in range(20):
            d = _compute_delay(attempt=1, base_delay=1.0, max_delay=30.0, jitter=0.5)
            delays.add(round(d, 4))
        # With jitter, we should see some variation (not all the same)
        assert len(delays) > 1


class TestSyncRetry:
    def test_succeeds_on_first_try(self):
        call_count = 0

        @with_retry(max_attempts=3, base_delay=0.01)
        def good_func():
            nonlocal call_count
            call_count += 1
            return "ok"

        result = good_func()
        assert result == "ok"
        assert call_count == 1

    def test_retries_on_failure_then_succeeds(self):
        call_count = 0

        @with_retry(max_attempts=3, base_delay=0.01)
        def flaky_func():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("transient")
            return "recovered"

        result = flaky_func()
        assert result == "recovered"
        assert call_count == 3

    def test_exhausts_all_attempts(self):
        @with_retry(max_attempts=2, base_delay=0.01)
        def always_fail():
            raise ValueError("permanent")

        with pytest.raises(RetryExhaustedError) as exc_info:
            always_fail()
        assert exc_info.value.attempts == 2
        assert isinstance(exc_info.value.last_exception, ValueError)

    def test_retry_on_specific_exception(self):
        call_count = 0

        @with_retry(max_attempts=3, base_delay=0.01, retry_on=(ConnectionError,))
        def selective_fail():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ValueError("not retryable")
            return "ok"

        # ValueError should not be retried
        with pytest.raises(ValueError):
            selective_fail()
        assert call_count == 1


class TestAsyncRetry:
    @pytest.mark.asyncio
    async def test_async_succeeds_on_first_try(self):
        call_count = 0

        @with_retry(max_attempts=3, base_delay=0.01)
        async def good_func():
            nonlocal call_count
            call_count += 1
            return "ok"

        result = await good_func()
        assert result == "ok"
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_async_retries_then_succeeds(self):
        call_count = 0

        @with_retry(max_attempts=3, base_delay=0.01)
        async def flaky_func():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise ConnectionError("transient")
            return "recovered"

        result = await flaky_func()
        assert result == "recovered"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_async_exhausts_attempts(self):
        @with_retry(max_attempts=2, base_delay=0.01)
        async def always_fail():
            raise RuntimeError("permanent")

        with pytest.raises(RetryExhaustedError):
            await always_fail()
