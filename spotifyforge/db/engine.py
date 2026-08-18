"""Database engine setup and session management for SpotifyForge."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlmodel import Session, SQLModel, create_engine

from spotifyforge.config import settings

# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
_engine = None
_async_engine = None


def _base_url() -> str:
    """Resolve the configured database URL with any driver suffix stripped.

    Accepts ``sqlite://``, ``sqlite+aiosqlite://``, ``postgresql://`` and
    ``postgresql+asyncpg://`` forms; sync and async engines each re-attach
    the driver they need, so either URL form works for both engines.
    """
    if settings.database_url:
        url = settings.database_url
        return url.replace("sqlite+aiosqlite://", "sqlite://", 1).replace(
            "postgresql+asyncpg://", "postgresql://", 1
        )
    db_path = settings.db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{db_path}"


def get_engine():
    """Return a singleton synchronous database engine."""
    global _engine  # noqa: PLW0603

    if _engine is None:
        url = _base_url()
        kwargs: dict = {"echo": False, "pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        _engine = create_engine(url, **kwargs)

    return _engine


def _get_async_engine():
    """Return a singleton async database engine (aiosqlite / asyncpg)."""
    global _async_engine  # noqa: PLW0603

    if _async_engine is None:
        url = _base_url()
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        elif url.startswith("sqlite://"):
            url = url.replace("sqlite://", "sqlite+aiosqlite://", 1)

        kwargs: dict = {"echo": False, "pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        elif url.startswith("postgresql"):
            kwargs["pool_size"] = 5
            kwargs["max_overflow"] = 10

        _async_engine = create_async_engine(url, **kwargs)

    return _async_engine


def reset_engines() -> None:
    """Drop the cached engines so the next call rebuilds from settings.

    Needed by tests that point ``database_url`` at a fresh database.
    """
    global _engine, _async_engine  # noqa: PLW0603
    _engine = None
    _async_engine = None


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
