"""WebSocket endpoint and connection manager for real-time notifications.

Clients connect to ``ws://host/ws/notifications`` and receive JSON events
matching their user_id.  Authentication is via a query-string token.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from spotifyforge.core.notifications import Event, get_event_bus

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Manages active WebSocket connections per user."""

    def __init__(self) -> None:
        # user_id → list of active WebSocket connections
        self._connections: dict[int, list[WebSocket]] = {}

    async def connect(self, user_id: int, websocket: WebSocket) -> None:
        """Accept a WebSocket and register it for the given user."""
        await websocket.accept()
        if user_id not in self._connections:
            self._connections[user_id] = []
        self._connections[user_id].append(websocket)
        logger.info("WebSocket connected for user %d (total: %d)", user_id, self.total_connections)

    def disconnect(self, user_id: int, websocket: WebSocket) -> None:
        """Remove a WebSocket connection."""
        conns = self._connections.get(user_id, [])
        try:
            conns.remove(websocket)
        except ValueError:
            pass
        if not conns and user_id in self._connections:
            del self._connections[user_id]
        logger.info("WebSocket disconnected for user %d", user_id)

    async def send_to_user(self, user_id: int, data: dict[str, Any]) -> int:
        """Send a JSON message to all connections for a user.

        Returns the number of connections that received the message.
        """
        conns = self._connections.get(user_id, [])
        sent = 0
        dead: list[WebSocket] = []

        for ws in conns:
            try:
                await ws.send_text(json.dumps(data, default=str))
                sent += 1
            except Exception:
                dead.append(ws)

        # Clean up dead connections
        for ws in dead:
            self.disconnect(user_id, ws)

        return sent

    async def broadcast(self, data: dict[str, Any]) -> int:
        """Send a message to all connected users. Returns total sent."""
        sent = 0
        for user_id in list(self._connections.keys()):
            sent += await self.send_to_user(user_id, data)
        return sent

    @property
    def total_connections(self) -> int:
        return sum(len(conns) for conns in self._connections.values())

    @property
    def connected_users(self) -> list[int]:
        return list(self._connections.keys())


# ---------------------------------------------------------------------------
# Global singleton
# ---------------------------------------------------------------------------

_manager: ConnectionManager | None = None


def get_connection_manager() -> ConnectionManager:
    """Return the global ConnectionManager singleton."""
    global _manager  # noqa: PLW0603
    if _manager is None:
        _manager = ConnectionManager()
    return _manager


# ---------------------------------------------------------------------------
# Event bus bridge — forwards events to WebSocket clients
# ---------------------------------------------------------------------------


async def _ws_event_handler(event: Event) -> None:
    """Bridge: forward events from the EventBus to WebSocket clients."""
    manager = get_connection_manager()
    if event.user_id is not None:
        await manager.send_to_user(event.user_id, event.to_dict())
    else:
        await manager.broadcast(event.to_dict())


def setup_ws_bridge() -> None:
    """Register the WebSocket bridge with the event bus.

    Call this once during application startup.
    """
    bus = get_event_bus()
    bus.subscribe_all(_ws_event_handler)
    logger.info("WebSocket notification bridge registered")


# ---------------------------------------------------------------------------
# WebSocket route handler
# ---------------------------------------------------------------------------


async def websocket_endpoint(websocket: WebSocket) -> None:
    """Handle a WebSocket connection for notifications.

    Authentication is checked via the ``token`` query parameter, which
    should match a user's access token hash.
    """
    from spotifyforge.db.engine import get_session
    from spotifyforge.models.models import User

    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=4001, reason="Missing token")
        return

    # Look up user by token hash
    from spotifyforge.security import hash_token

    token_hash = hash_token(token)
    with get_session() as session:
        from sqlmodel import select

        stmt = select(User).where(User.token_hash == token_hash)
        user = session.exec(stmt).first()

    if user is None or user.id is None:
        await websocket.close(code=4003, reason="Invalid token")
        return

    manager = get_connection_manager()
    await manager.connect(user.id, websocket)

    try:
        while True:
            # Keep connection alive; handle client pings
            data = await websocket.receive_text()
            # Clients can send filter preferences
            if data == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        manager.disconnect(user.id, websocket)
    except Exception:
        logger.exception("WebSocket error for user %d", user.id)
        manager.disconnect(user.id, websocket)
