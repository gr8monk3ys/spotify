"""Database engine setup and session management for SpotifyForge."""

from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from typing import AsyncGenerator, Generator

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlmodel import Session, SQLModel, create_engine

from spotifyforge.config import settings

# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
_engine = None
_async_engine = None


def get_engine():
    """Return a singleton SQLite engine, creating the db directory if needed.

    The database path is read from ``Settings.db_path``.  The parent directory
    is created automatically so callers never have to worry about it.
    """
    global _engine  # noqa: PLW0603

    if _engine is None:
        db_path = settings.db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)

        _engine = create_engine(
            f"sqlite:///{db_path}",
            echo=False,
            connect_args={"check_same_thread": False},
        )

    return _engine


def _get_async_engine():
    """Return a singleton async (aiosqlite) engine."""
    global _async_engine  # noqa: PLW0603

    if _async_engine is None:
        db_path = settings.db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)

        _async_engine = create_async_engine(
            f"sqlite+aiosqlite:///{db_path}",
            echo=False,
            connect_args={"check_same_thread": False},
        )

    return _async_engine


# ---------------------------------------------------------------------------
# Table initialisation
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Create the SQLite database file and all registered SQLModel tables.

    Models are imported here so that their metadata is registered with
    ``SQLModel.metadata`` before ``create_all`` is called.
    """
    # Import models to trigger table registration with SQLModel.metadata.
    import spotifyforge.models.models as _models  # noqa: F401

    engine = get_engine()
    SQLModel.metadata.create_all(engine)


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

@contextmanager
def get_session() -> Generator[Session, None, None]:
    """Yield a synchronous SQLModel ``Session`` and close it on exit.

    Usage::

        with get_session() as session:
            session.exec(select(Track))
    """
    engine = get_engine()
    with Session(engine) as session:
        yield session


@asynccontextmanager
async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield an asynchronous ``AsyncSession`` (backed by *aiosqlite*) and
    close it on exit.

    Usage::

        async with get_async_session() as session:
            result = await session.execute(select(Track))
    """
    engine = _get_async_engine()
    async with AsyncSession(engine) as session:
        yield session
