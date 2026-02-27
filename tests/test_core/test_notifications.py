"""Tests for the event bus and notification system."""

from __future__ import annotations

import pytest

from spotifyforge.core.notifications import (
    Event,
    EventBus,
    EventType,
    emit_health_alert,
    emit_job_completed,
    emit_job_failed,
    emit_playlist_changed,
    get_event_bus,
)


@pytest.fixture()
def bus():
    """Create a fresh EventBus for each test."""
    return EventBus()


@pytest.fixture(autouse=True)
def _reset_global_bus():
    """Reset the global event bus between tests."""
    import spotifyforge.core.notifications as mod

    old = mod._bus
    mod._bus = None
    yield
    mod._bus = old


class TestEvent:
    def test_event_to_dict(self):
        event = Event(
            event_type=EventType.JOB_COMPLETED,
            user_id=1,
            payload={"job_name": "sync"},
        )
        d = event.to_dict()
        assert d["event_type"] == "job_completed"
        assert d["user_id"] == 1
        assert d["payload"]["job_name"] == "sync"
        assert "timestamp" in d

    def test_event_default_payload(self):
        event = Event(event_type=EventType.HEALTH_ALERT)
        assert event.payload == {}
        assert event.user_id is None


class TestEventBus:
    @pytest.mark.asyncio
    async def test_subscribe_and_emit(self, bus: EventBus):
        received = []

        async def handler(event):
            received.append(event)

        bus.subscribe(EventType.JOB_COMPLETED, handler)
        event = Event(EventType.JOB_COMPLETED, user_id=1)
        count = await bus.emit(event)

        assert count == 1
        assert len(received) == 1
        assert received[0].event_type == EventType.JOB_COMPLETED

    @pytest.mark.asyncio
    async def test_subscribe_all(self, bus: EventBus):
        received = []

        async def handler(event):
            received.append(event)

        bus.subscribe_all(handler)
        await bus.emit(Event(EventType.JOB_COMPLETED))
        await bus.emit(Event(EventType.HEALTH_ALERT))

        assert len(received) == 2

    @pytest.mark.asyncio
    async def test_unsubscribe(self, bus: EventBus):
        received = []

        async def handler(event):
            received.append(event)

        bus.subscribe(EventType.JOB_COMPLETED, handler)
        bus.unsubscribe(EventType.JOB_COMPLETED, handler)
        await bus.emit(Event(EventType.JOB_COMPLETED))

        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_unsubscribe_all(self, bus: EventBus):
        received = []

        async def handler(event):
            received.append(event)

        bus.subscribe_all(handler)
        bus.unsubscribe_all(handler)
        await bus.emit(Event(EventType.JOB_COMPLETED))

        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_no_crosstalk_between_event_types(self, bus: EventBus):
        received = []

        async def handler(event):
            received.append(event)

        bus.subscribe(EventType.JOB_COMPLETED, handler)
        await bus.emit(Event(EventType.HEALTH_ALERT))

        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_multiple_subscribers(self, bus: EventBus):
        counts = {"a": 0, "b": 0}

        async def handler_a(event):
            counts["a"] += 1

        async def handler_b(event):
            counts["b"] += 1

        bus.subscribe(EventType.JOB_COMPLETED, handler_a)
        bus.subscribe(EventType.JOB_COMPLETED, handler_b)
        await bus.emit(Event(EventType.JOB_COMPLETED))

        assert counts["a"] == 1
        assert counts["b"] == 1

    @pytest.mark.asyncio
    async def test_sync_callback(self, bus: EventBus):
        """Sync callbacks should work too (not just async)."""
        received = []

        def handler(event):
            received.append(event)

        bus.subscribe(EventType.JOB_COMPLETED, handler)
        count = await bus.emit(Event(EventType.JOB_COMPLETED))

        assert count == 1
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_handler_exception_doesnt_break_others(self, bus: EventBus):
        received = []

        async def bad_handler(event):
            raise RuntimeError("boom")

        async def good_handler(event):
            received.append(event)

        bus.subscribe(EventType.JOB_COMPLETED, bad_handler)
        bus.subscribe(EventType.JOB_COMPLETED, good_handler)
        count = await bus.emit(Event(EventType.JOB_COMPLETED))

        assert count == 1  # only good_handler succeeded
        assert len(received) == 1

    def test_subscriber_count(self, bus: EventBus):
        async def h(event):
            pass

        assert bus.subscriber_count() == 0
        bus.subscribe(EventType.JOB_COMPLETED, h)
        assert bus.subscriber_count(EventType.JOB_COMPLETED) == 1
        assert bus.subscriber_count() == 1

        bus.subscribe_all(h)
        assert bus.subscriber_count() == 2

    def test_clear(self, bus: EventBus):
        async def h(event):
            pass

        bus.subscribe(EventType.JOB_COMPLETED, h)
        bus.subscribe_all(h)
        bus.clear()
        assert bus.subscriber_count() == 0


class TestConvenienceEmitters:
    @pytest.mark.asyncio
    async def test_emit_job_completed(self):
        bus = get_event_bus()
        received = []
        bus.subscribe(EventType.JOB_COMPLETED, lambda e: received.append(e))
        await emit_job_completed(user_id=1, job_name="sync", job_type="playlist_sync")
        assert len(received) == 1
        assert received[0].payload["job_name"] == "sync"

    @pytest.mark.asyncio
    async def test_emit_job_failed(self):
        bus = get_event_bus()
        received = []
        bus.subscribe(EventType.JOB_FAILED, lambda e: received.append(e))
        await emit_job_failed(user_id=1, job_name="sync", job_type="playlist_sync", error="timeout")
        assert len(received) == 1
        assert received[0].payload["error"] == "timeout"

    @pytest.mark.asyncio
    async def test_emit_playlist_changed(self):
        bus = get_event_bus()
        received = []
        bus.subscribe(EventType.PLAYLIST_CHANGED, lambda e: received.append(e))
        await emit_playlist_changed(user_id=1, playlist_id=42, change_type="tracks_added")
        assert len(received) == 1
        assert received[0].payload["playlist_id"] == 42

    @pytest.mark.asyncio
    async def test_emit_health_alert(self):
        bus = get_event_bus()
        received = []
        bus.subscribe(EventType.HEALTH_ALERT, lambda e: received.append(e))
        await emit_health_alert(
            user_id=1, playlist_id=42, alert_type="staleness", message="Playlist is stale"
        )
        assert len(received) == 1
        assert received[0].payload["alert_type"] == "staleness"


class TestGlobalSingleton:
    def test_get_event_bus_returns_same_instance(self):
        bus1 = get_event_bus()
        bus2 = get_event_bus()
        assert bus1 is bus2
