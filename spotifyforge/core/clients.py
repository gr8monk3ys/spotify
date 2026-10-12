"""Spotify client construction.

The single place a plaintext access token becomes a usable
:class:`tekore.Spotify` client, so the retry and timeout policy is the
same for the CLI and the MCP server.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
import tekore as tk
from tekore._sender.concrete import Sender
from tekore._sender.extending import ExtendingSender

from spotifyforge.auth.oauth import make_sender

logger = logging.getLogger(__name__)

# Retry 429/5xx responses this many times, honouring Retry-After.
_RETRIES = 2

# httpx defaults to 5s, which a bulk run trips on eventually: one slow
# response then aborts hundreds of playlists' worth of work.
_TIMEOUT = 30.0


class TimeoutRetryingSender(ExtendingSender):
    """Retry requests that never got an answer at all.

    ``tk.RetryingSender`` retries on 429 and 5xx — responses. A read
    timeout or dropped connection raises before there is a response, so
    it slips past and aborts whatever bulk operation was running. Across
    the few hundred requests a full catalogue run makes, that is close to
    inevitable, and it killed a 221-playlist reflow three times.
    """

    def __init__(self, sender: Sender | None = None, retries: int = 2) -> None:
        super().__init__(sender)
        self.retries = retries

    def send(self, request: object) -> object:
        if self.is_async:
            return self._async_send(request)
        raise NotImplementedError("TimeoutRetryingSender is async-only")

    async def _async_send(self, request: object) -> object:
        delay = 1.0
        for attempt in range(self.retries + 1):
            try:
                return await self.sender.send(request)
            except httpx.TransportError as exc:
                if attempt == self.retries:
                    logger.error("Request failed after %d attempts: %r", attempt + 1, exc)
                    raise
                logger.warning("Transport error (%r); retrying in %.0fs", exc, delay)
                await asyncio.sleep(delay)
                delay *= 2
        raise AssertionError("unreachable")


def build_spotify(access_token: str) -> tk.Spotify:
    """Wrap a plaintext access token in an async, retrying Spotify client."""
    inner = make_sender(True) or tk.AsyncSender(client=httpx.AsyncClient(timeout=_TIMEOUT))
    sender = tk.RetryingSender(retries=_RETRIES, sender=TimeoutRetryingSender(inner))
    return tk.Spotify(access_token, sender=sender)
