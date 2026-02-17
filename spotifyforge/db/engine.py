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
    """Return a singleton database engine.

    If ``Settings.database_url`` is set, it is used directly.  Otherwise a
    SQLite URL is constructed from ``Settings.db_path`` (creating the parent
    directory automatically).
    """
    global _engine  # noqa: PLW0603

    if _engine is None:
        if settings.database_url:
            url = settings.database_url
        else:
            db_path = settings.db_path
            db_path.parent.mkdir(parents=True, exist_ok=True)
            url = f"sqlite:///{db_path}"

        kwargs: dict = {"echo": False, "pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}

        _engine = create_engine(url, **kwargs)

    return _engine


def _get_async_engine():
    """Return a singleton async database engine.

    If ``Settings.database_url`` is set the URL is adapted for async drivers
    (``asyncpg`` for PostgreSQL, ``aiosqlite`` for SQLite).  Otherwise a
    SQLite+aiosqlite URL is constructed from ``Settings.db_path``.
    """
    global _async_engine  # noqa: PLW0603

    if _async_engine is None:
        if settings.database_url:
            url = settings.database_url
            if url.startswith("postgresql://"):
                url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
            elif url.startswith("sqlite://"):
                url = url.replace("sqlite://", "sqlite+aiosqlite://", 1)
        else:
            db_path = settings.db_path
            db_path.parent.mkdir(parents=True, exist_ok=True)
            url = f"sqlite+aiosqlite:///{db_path}"

        kwargs: dict = {"echo": False, "pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        elif url.startswith("postgresql"):
            kwargs["pool_size"] = 5
            kwargs["max_overflow"] = 10

        _async_engine = create_async_engine(url, **kwargs)

    return _async_engine


# ---------------------------------------------------------------------------
# Table initialisation
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Create all registered SQLModel tables in the configured database.

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
