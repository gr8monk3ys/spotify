"""In-process event bus and notification manager for SpotifyForge.

Provides a simple pub/sub event system that services can emit to and
WebSocket/webhook consumers can subscribe to.  All operations are
thread-safe and async-friendly.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Event types
# ---------------------------------------------------------------------------


class EventType(StrEnum):
    """Recognised notification event types."""

    PLAYLIST_CHANGED = "playlist_changed"
    NEW_RELEASE = "new_release_detected"
    COMPETITOR_UPDATE = "competitor_update"
    CURATION_APPLIED = "curation_rule_applied"
    JOB_COMPLETED = "job_completed"
    JOB_FAILED = "job_failed"
    HEALTH_ALERT = "health_alert"
    RECOMMENDATION_READY = "recommendation_ready"


class Event:
    """An immutable notification event."""

    __slots__ = ("event_type", "user_id", "payload", "timestamp")

    def __init__(
        self,
        event_type: EventType,
        user_id: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.event_type = event_type
        self.user_id = user_id
        self.payload = payload or {}
        self.timestamp = datetime.now(UTC)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "user_id": self.user_id,
            "payload": self.payload,
            "timestamp": self.timestamp.isoformat(),
        }


# ---------------------------------------------------------------------------
# Event bus
# ---------------------------------------------------------------------------

# Callback type: async functions that receive an Event
EventCallback = Callable[[Event], Any]


class EventBus:
    """In-process async event bus.

    Subscribers register for specific event types and receive callbacks
    when matching events are emitted.
    """

    def __init__(self) -> None:
        self._subscribers: dict[EventType, list[EventCallback]] = defaultdict(list)
        self._global_subscribers: list[EventCallback] = []

    def subscribe(self, event_type: EventType, callback: EventCallback) -> None:
        """Subscribe to a specific event type."""
        self._subscribers[event_type].append(callback)

    def subscribe_all(self, callback: EventCallback) -> None:
        """Subscribe to all event types."""
        self._global_subscribers.append(callback)

    def unsubscribe(self, event_type: EventType, callback: EventCallback) -> None:
        """Remove a subscription."""
        try:
            self._subscribers[event_type].remove(callback)
        except ValueError:
            pass

    def unsubscribe_all(self, callback: EventCallback) -> None:
        """Remove a global subscription."""
        try:
            self._global_subscribers.remove(callback)
        except ValueError:
            pass

    async def emit(self, event: Event) -> int:
        """Emit an event to all matching subscribers.

        Returns the number of subscribers notified.
        """
        notified = 0
        callbacks = list(self._subscribers.get(event.event_type, []))
        callbacks.extend(self._global_subscribers)

        for callback in callbacks:
            try:
                result = callback(event)
                if asyncio.iscoroutine(result):
                    await result
                notified += 1
            except Exception:
                logger.exception(
                    "Event subscriber failed for %s", event.event_type
                )

        if notified > 0:
            logger.debug(
                "Emitted %s to %d subscriber(s)", event.event_type, notified
            )

        return notified

    def emit_sync(self, event: Event) -> None:
        """Fire-and-forget emit for use in sync contexts.

        Creates an async task if there's a running event loop, otherwise logs.
        """
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.emit(event))
        except RuntimeError:
            logger.debug(
                "No event loop available for sync emit of %s", event.event_type
            )

    def subscriber_count(self, event_type: EventType | None = None) -> int:
        """Return the number of subscribers for a given event type."""
        if event_type is None:
            total = len(self._global_subscribers)
            for subs in self._subscribers.values():
                total += len(subs)
            return total
        return len(self._subscribers.get(event_type, [])) + len(self._global_subscribers)

    def clear(self) -> None:
        """Remove all subscribers."""
        self._subscribers.clear()
        self._global_subscribers.clear()


# ---------------------------------------------------------------------------
# Global singleton
# ---------------------------------------------------------------------------

_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    """Return the global EventBus singleton."""
    global _bus  # noqa: PLW0603
    if _bus is None:
        _bus = EventBus()
    return _bus


# ---------------------------------------------------------------------------
# Convenience emitters
# ---------------------------------------------------------------------------


async def emit_job_completed(
    user_id: int,
    job_name: str,
    job_type: str,
    details: dict[str, Any] | None = None,
) -> None:
    """Emit a JOB_COMPLETED event."""
    bus = get_event_bus()
    await bus.emit(Event(
        event_type=EventType.JOB_COMPLETED,
        user_id=user_id,
        payload={"job_name": job_name, "job_type": job_type, **(details or {})},
    ))


async def emit_job_failed(
    user_id: int,
    job_name: str,
    job_type: str,
    error: str,
) -> None:
    """Emit a JOB_FAILED event."""
    bus = get_event_bus()
    await bus.emit(Event(
        event_type=EventType.JOB_FAILED,
        user_id=user_id,
        payload={"job_name": job_name, "job_type": job_type, "error": error},
    ))


async def emit_playlist_changed(
    user_id: int,
    playlist_id: int,
    change_type: str,
    details: dict[str, Any] | None = None,
) -> None:
    """Emit a PLAYLIST_CHANGED event."""
    bus = get_event_bus()
    await bus.emit(Event(
        event_type=EventType.PLAYLIST_CHANGED,
        user_id=user_id,
        payload={"playlist_id": playlist_id, "change_type": change_type, **(details or {})},
    ))


async def emit_health_alert(
    user_id: int,
    playlist_id: int,
    alert_type: str,
    message: str,
) -> None:
    """Emit a HEALTH_ALERT event."""
    bus = get_event_bus()
    await bus.emit(Event(
        event_type=EventType.HEALTH_ALERT,
        user_id=user_id,
        payload={
            "playlist_id": playlist_id,
            "alert_type": alert_type,
            "message": message,
        },
    ))
