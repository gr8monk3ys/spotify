"""Fixtures shared by the core-module tests."""

from __future__ import annotations

import pytest
import tekore as tk


@pytest.fixture()
async def client_for(fake_spotify):
    """A factory for authenticated async clients against the fake backend.

    Every client it hands out is closed when the test ends, whichever
    user it was minted for.
    """
    clients: list[tk.Spotify] = []

    def make(user_id: str = "user1") -> tk.Spotify:
        client = fake_spotify.async_client(user_id)
        clients.append(client)
        return client

    yield make
    for client in clients:
        await client.close()


@pytest.fixture()
def db_user():
    """A factory that creates a ``User`` row and returns its id."""
    from sqlmodel import Session

    from spotifyforge.db.engine import get_engine
    from spotifyforge.models.models import User

    def make(spotify_id: str = "user1") -> int:
        with Session(get_engine()) as session:
            user = User(spotify_id=spotify_id)
            session.add(user)
            session.commit()
            session.refresh(user)
            return user.id

    return make
