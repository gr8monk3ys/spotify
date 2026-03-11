"""Retry decorator with exponential backoff and jitter.

Provides a ``with_retry`` decorator for wrapping async and sync functions
with automatic retry logic.  Designed for transient failures (network
timeouts, 429 rate limits, 5xx server errors).
"""

from __future__ import annotations

import asyncio
import functools
import logging
import random
import time
from collections.abc import Callable
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


class RetryExhaustedError(Exception):
    """Raised when all retry attempts have been used up."""

    def __init__(self, attempts: int, last_exception: Exception) -> None:
        self.attempts = attempts
        self.last_exception = last_exception
        super().__init__(f"All {attempts} retry attempts exhausted. Last error: {last_exception}")


def with_retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    jitter: float = 0.5,
    retry_on: tuple[type[Exception], ...] | None = None,
    service_name: str = "",
) -> Callable[[F], F]:
    """Decorator that retries a function on failure with exponential backoff.

    Parameters
    ----------
    max_attempts:
        Total number of attempts (including the first call).
    base_delay:
        Base delay in seconds before the first retry.
    max_delay:
        Maximum delay cap in seconds.
    jitter:
        Random jitter factor (0.0–1.0) added to each delay.
    retry_on:
        Tuple of exception types to retry on.  If ``None``, retries on all
        exceptions.
    service_name:
        Optional service name for logging (defaults to function name).
    """

    def decorator(func: F) -> F:
        svc = service_name or func.__qualname__

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exc: Exception | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as exc:
                    if retry_on and not isinstance(exc, retry_on):
                        raise
                    last_exc = exc
                    if attempt == max_attempts:
                        break
                    delay = _compute_delay(attempt, base_delay, max_delay, jitter)
                    logger.warning(
                        "[%s] Attempt %d/%d failed: %s. Retrying in %.2fs",
                        svc,
                        attempt,
                        max_attempts,
                        exc,
                        delay,
                    )
                    await asyncio.sleep(delay)

            assert last_exc is not None
            logger.error("[%s] All %d attempts exhausted", svc, max_attempts)
            raise RetryExhaustedError(max_attempts, last_exc)

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exc: Exception | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as exc:
                    if retry_on and not isinstance(exc, retry_on):
                        raise
                    last_exc = exc
                    if attempt == max_attempts:
                        break
                    delay = _compute_delay(attempt, base_delay, max_delay, jitter)
                    logger.warning(
                        "[%s] Attempt %d/%d failed: %s. Retrying in %.2fs",
                        svc,
                        attempt,
                        max_attempts,
                        exc,
                        delay,
                    )
                    time.sleep(delay)

            assert last_exc is not None
            logger.error("[%s] All %d attempts exhausted", svc, max_attempts)
            raise RetryExhaustedError(max_attempts, last_exc)

        if asyncio.iscoroutinefunction(func):
            return async_wrapper  # type: ignore[return-value]
        return sync_wrapper  # type: ignore[return-value]

    return decorator


def _compute_delay(
    attempt: int,
    base_delay: float,
    max_delay: float,
    jitter: float,
) -> float:
    """Compute delay with exponential backoff and random jitter."""
    delay = base_delay * (2 ** (attempt - 1))
    delay = min(delay, max_delay)
    jitter_amount = delay * jitter * random.random()  # noqa: S311
    return float(delay + jitter_amount)
