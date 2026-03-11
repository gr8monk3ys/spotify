"""Circuit breaker pattern for external service calls.

Prevents cascading failures by tracking error rates and short-circuiting
calls to unhealthy services.  Three states:

* **CLOSED** — normal operation, requests pass through
* **OPEN** — too many failures, requests are rejected immediately
* **HALF_OPEN** — cooldown elapsed, one probe request is allowed through

All public functions are thread-safe via a simple lock.
"""

from __future__ import annotations

import logging
import threading
import time
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    """Raised when a call is attempted on an open circuit."""

    def __init__(self, service: str, retry_after: float) -> None:
        self.service = service
        self.retry_after = retry_after
        super().__init__(f"Circuit open for '{service}', retry after {retry_after:.1f}s")


class CircuitBreaker:
    """Per-service circuit breaker.

    Parameters
    ----------
    service:
        Human-readable service name (e.g. ``"spotify_api"``).
    failure_threshold:
        Number of consecutive failures before the circuit opens.
    cooldown_seconds:
        How long to wait in OPEN state before allowing a probe.
    """

    def __init__(
        self,
        service: str,
        failure_threshold: int = 5,
        cooldown_seconds: float = 30.0,
    ) -> None:
        self.service = service
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time = 0.0
        self._lock = threading.Lock()

    @property
    def state(self) -> CircuitState:
        with self._lock:
            if self._state == CircuitState.OPEN:
                if time.monotonic() - self._last_failure_time >= self.cooldown_seconds:
                    self._state = CircuitState.HALF_OPEN
                    logger.info("Circuit '%s' transitioning to HALF_OPEN", self.service)
            return self._state

    def check(self) -> None:
        """Raise :class:`CircuitOpenError` if the circuit is open."""
        current = self.state
        if current == CircuitState.OPEN:
            remaining = self.cooldown_seconds - (time.monotonic() - self._last_failure_time)
            raise CircuitOpenError(self.service, max(0.0, remaining))

    def record_success(self) -> None:
        """Record a successful call — resets the failure counter."""
        with self._lock:
            if self._state in (CircuitState.HALF_OPEN, CircuitState.CLOSED):
                self._failure_count = 0
                if self._state == CircuitState.HALF_OPEN:
                    logger.info("Circuit '%s' recovered → CLOSED", self.service)
                self._state = CircuitState.CLOSED

    def record_failure(self) -> None:
        """Record a failed call — may trip the circuit to OPEN."""
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()

            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                logger.warning(
                    "Circuit '%s' probe failed → OPEN (cooldown=%.0fs)",
                    self.service,
                    self.cooldown_seconds,
                )
            elif self._failure_count >= self.failure_threshold:
                self._state = CircuitState.OPEN
                logger.warning(
                    "Circuit '%s' tripped → OPEN after %d failures",
                    self.service,
                    self._failure_count,
                )

    def reset(self) -> None:
        """Force-reset the circuit to CLOSED (for testing or manual recovery)."""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._last_failure_time = 0.0

    def stats(self) -> dict[str, Any]:
        """Return current circuit state and counters."""
        return {
            "service": self.service,
            "state": self.state,
            "failure_count": self._failure_count,
            "failure_threshold": self.failure_threshold,
            "cooldown_seconds": self.cooldown_seconds,
        }


# ---------------------------------------------------------------------------
# Global registry
# ---------------------------------------------------------------------------

_breakers: dict[str, CircuitBreaker] = {}
_registry_lock = threading.Lock()


def get_breaker(
    service: str,
    failure_threshold: int = 5,
    cooldown_seconds: float = 30.0,
) -> CircuitBreaker:
    """Return (or create) the circuit breaker for *service*."""
    with _registry_lock:
        if service not in _breakers:
            _breakers[service] = CircuitBreaker(
                service=service,
                failure_threshold=failure_threshold,
                cooldown_seconds=cooldown_seconds,
            )
        return _breakers[service]


def get_all_breakers() -> dict[str, CircuitBreaker]:
    """Return a snapshot of all registered circuit breakers."""
    with _registry_lock:
        return dict(_breakers)


def reset_all() -> None:
    """Reset all breakers to CLOSED (for testing)."""
    with _registry_lock:
        for breaker in _breakers.values():
            breaker.reset()
