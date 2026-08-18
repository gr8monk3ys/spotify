"""Scheduling service for automated SpotifyForge jobs.

The single scheduler implementation, shared by the web app (lifespan +
schedule routes) and the CLI (``spotifyforge schedule run``). Jobs are
persisted as :class:`ScheduledJob` rows; this module registers them with
APScheduler and dispatches on :class:`JobType` — the same enum the API
validates against, so every storable job has a real handler.

Each job authenticates as the user who created it: at execution time the
user's stored (encrypted) tokens are loaded and turned into a Spotify
client via :func:`spotifyforge.core.clients.spotify_client_for_user`.
"""

from __future__ import annotations

import logging
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlmodel import select

from spotifyforge.db.engine import get_async_session
from spotifyforge.models.models import JobType, Playlist, ScheduledJob, User, utc_now

logger = logging.getLogger(__name__)


def validate_cron(expression: str) -> CronTrigger | None:
    """Parse a 5-field cron expression (``m h dom mon dow``).

    Returns the trigger, or ``None`` if the expression is invalid — used
    both at registration time and by the API/CLI to reject bad
    expressions at creation time instead of silently never running.
    """
    parts = expression.strip().split()
    if len(parts) != 5:
        return None
    try:
        return CronTrigger(
            minute=parts[0],
            hour=parts[1],
            day=parts[2],
            month=parts[3],
            day_of_week=parts[4],
        )
    except (ValueError, TypeError):
        return None


class SchedulerService:
    """Manages scheduled automation jobs backed by APScheduler."""

    def __init__(self) -> None:
        self._scheduler = AsyncIOScheduler()
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the scheduler. Safe to call repeatedly."""
        if self._running:
            return
        self._scheduler.start()
        self._running = True
        logger.info("Scheduler started")

    def stop(self, wait: bool = True) -> None:
        """Shut down the scheduler gracefully."""
        if not self._running:
            return
        self._scheduler.shutdown(wait=wait)
        self._running = False
        logger.info("Scheduler stopped")

    @property
    def is_running(self) -> bool:
        return self._running

    # ------------------------------------------------------------------
    # Job registration
    # ------------------------------------------------------------------

    def add_job(self, scheduled_job: ScheduledJob) -> None:
        """Register a :class:`ScheduledJob` with APScheduler.

        Raises
        ------
        ValueError
            If the cron expression is invalid. (The job type cannot be
            invalid: ``ScheduledJob.job_type`` is the same enum this
            service dispatches on.)
        """
        trigger = validate_cron(scheduled_job.cron_expression)
        if trigger is None:
            raise ValueError(f"Invalid cron expression: {scheduled_job.cron_expression!r}")

        job_id = f"spotifyforge_job_{scheduled_job.id}"
        self._scheduler.add_job(
            self._execute_job,
            trigger=trigger,
            id=job_id,
            name=scheduled_job.name or job_id,
            kwargs={"job_id": scheduled_job.id},
            replace_existing=True,
        )
        logger.info(
            "Registered job %s (%s) with cron '%s'",
            job_id,
            scheduled_job.job_type,
            scheduled_job.cron_expression,
        )

    def remove_job(self, job_pk: int) -> None:
        """Remove a job by its database primary key. No-op if absent."""
        job_id = f"spotifyforge_job_{job_pk}"
        try:
            self._scheduler.remove_job(job_id)
            logger.info("Removed job %s", job_id)
        except Exception:
            logger.debug("Job %s was not registered", job_id)

    def next_run_time(self, job_pk: int) -> Any:
        """Return the next fire time for a registered job, or ``None``."""
        job = self._scheduler.get_job(f"spotifyforge_job_{job_pk}")
        return job.next_run_time if job is not None else None

    async def load_jobs_from_db(self) -> int:
        """Register every enabled :class:`ScheduledJob`. Returns the count."""
        async with get_async_session() as session:
            result = await session.execute(
                select(ScheduledJob).where(ScheduledJob.enabled == True)  # noqa: E712
            )
            jobs = list(result.scalars().all())

        registered = 0
        for job in jobs:
            try:
                self.add_job(job)
                registered += 1
            except Exception:
                logger.exception("Failed to register job %s (%s)", job.id, job.name)

        logger.info("Loaded %d/%d enabled jobs from the database", registered, len(jobs))
        return registered

    # ------------------------------------------------------------------
    # Job execution
    # ------------------------------------------------------------------

    async def _execute_job(self, job_id: int) -> None:
        """Load the job, authenticate as its owner, and dispatch."""
        logger.info("Executing scheduled job %d", job_id)

        async with get_async_session() as session:
            job = await session.get(ScheduledJob, job_id)
            if job is None:
                logger.error("Scheduled job %d not found in database", job_id)
                return
            if not job.enabled:
                logger.info("Scheduled job %d is disabled — skipping", job_id)
                return

            user = await session.get(User, job.user_id)
            if user is None:
                await self._record_failure(job_id, f"Owner user {job.user_id} not found")
                return

            playlist_spotify_id: str | None = None
            if job.playlist_id is not None:
                playlist = await session.get(Playlist, job.playlist_id)
                playlist_spotify_id = playlist.spotify_id if playlist else None

            job_type = job.job_type
            config: dict[str, Any] = dict(job.config) if job.config else {}
            user_id = user.id

            try:
                from spotifyforge.core.clients import spotify_client_for_user

                spotify = await spotify_client_for_user(user, session)
            except Exception as exc:
                logger.exception("Job %d: could not authenticate as user %s", job_id, job.user_id)
                await self._record_failure(job_id, f"Authentication failed: {exc}")
                return

        try:
            await self._dispatch(job_type, spotify, user_id, playlist_spotify_id, config)
        except Exception as exc:
            logger.exception("Job %d (%s) failed", job_id, job_type)
            await self._record_failure(job_id, str(exc))
            return

        async with get_async_session() as session:
            job = await session.get(ScheduledJob, job_id)
            if job is not None:
                job.last_run_at = utc_now()
                job.updated_at = utc_now()
                job.failure_count = 0
                job.last_error = None
                session.add(job)
                await session.commit()

        logger.info("Scheduled job %d (%s) completed successfully", job_id, job_type)

    async def _record_failure(self, job_id: int, error: str) -> None:
        async with get_async_session() as session:
            job = await session.get(ScheduledJob, job_id)
            if job is not None:
                job.failure_count += 1
                job.last_error = error[:1000]
                job.updated_at = utc_now()
                session.add(job)
                await session.commit()

    async def _dispatch(
        self,
        job_type: JobType,
        spotify: Any,
        user_id: int | None,
        playlist_spotify_id: str | None,
        config: dict[str, Any],
    ) -> None:
        if job_type is JobType.sync:
            await self._handle_sync(spotify, user_id, playlist_spotify_id)
        elif job_type is JobType.archive:
            await self._handle_archive(spotify, playlist_spotify_id, config)
        elif job_type is JobType.deduplicate:
            await self._handle_deduplicate(spotify, playlist_spotify_id)
        elif job_type is JobType.genre_refresh:
            await self._handle_genre_refresh(spotify, playlist_spotify_id, config)
        elif job_type is JobType.time_capsule:
            await self._handle_time_capsule(spotify, user_id, config)
        else:  # pragma: no cover — JobType is exhaustive
            raise ValueError(f"Unhandled job type: {job_type}")

    # ------------------------------------------------------------------
    # Individual job handlers
    # ------------------------------------------------------------------

    async def _handle_sync(
        self, spotify: Any, user_id: int | None, playlist_spotify_id: str | None
    ) -> None:
        """Sync a playlist from Spotify to the local database."""
        from spotifyforge.core.playlist_manager import PlaylistManager

        if not playlist_spotify_id or user_id is None:
            raise ValueError("sync job requires a playlist and an owner")

        await PlaylistManager(spotify).sync_playlist(playlist_spotify_id, owner_id=user_id)

    async def _handle_archive(
        self,
        spotify: Any,
        playlist_spotify_id: str | None,
        config: dict[str, Any],
    ) -> None:
        """Append the contents of a source playlist into the target playlist.

        Config: ``source_playlist_id`` — the Spotify ID to archive from
        (e.g. Discover Weekly).
        """
        from spotifyforge.core.playlist_manager import PlaylistManager

        source_id = config.get("source_playlist_id")
        if not source_id:
            raise ValueError("archive job requires 'source_playlist_id' in config")
        if not playlist_spotify_id:
            raise ValueError("archive job requires a target playlist")

        manager = PlaylistManager(spotify)
        items = await manager.get_playlist_tracks(source_id)
        uris = [i.track.uri for i in items if i.track is not None and i.track.uri is not None]

        if uris:
            await manager.add_tracks(playlist_spotify_id, uris)
            logger.info(
                "Archived %d tracks from %s to %s", len(uris), source_id, playlist_spotify_id
            )
        else:
            logger.warning("No tracks found in source playlist %s", source_id)

    async def _handle_deduplicate(self, spotify: Any, playlist_spotify_id: str | None) -> None:
        """Remove duplicate tracks from a playlist."""
        from spotifyforge.core.playlist_manager import PlaylistManager

        if not playlist_spotify_id:
            raise ValueError("deduplicate job requires a playlist")

        removed = await PlaylistManager(spotify).deduplicate(playlist_spotify_id)
        logger.info("Deduplicated %s — removed %d duplicates", playlist_spotify_id, removed)

    async def _handle_genre_refresh(
        self,
        spotify: Any,
        playlist_spotify_id: str | None,
        config: dict[str, Any],
    ) -> None:
        """Refresh a playlist with fresh tracks from a genre search.

        Config: ``genre`` (required), ``limit`` (default 50), ``replace``
        (default true). Replace mode adds the new tracks *before* removing
        stale ones, so a partial failure never leaves the playlist empty.
        """
        from spotifyforge.core.playlist_manager import PlaylistManager

        genre = config.get("genre")
        if not genre:
            raise ValueError("genre_refresh job requires 'genre' in config")
        if not playlist_spotify_id:
            raise ValueError("genre_refresh job requires a playlist")

        limit = int(config.get("limit", 50))
        replace = bool(config.get("replace", True))

        from spotifyforge.core.discovery import DiscoveryEngine

        tracks = await DiscoveryEngine(spotify).build_genre_playlist(genre=genre, limit=limit)
        if not tracks:
            logger.warning("No tracks found for genre '%s'", genre)
            return

        manager = PlaylistManager(spotify)
        new_uris = [t.uri for t in tracks if t.uri is not None]

        if not replace:
            if new_uris:
                await manager.add_tracks(playlist_spotify_id, new_uris)
            return

        existing_items = await manager.get_playlist_tracks(playlist_spotify_id)
        existing_uris = {
            i.track.uri for i in existing_items if i.track is not None and i.track.uri is not None
        }
        new_set = set(new_uris)

        # Add first, remove after: the playlist is never empty mid-refresh,
        # and removing only (existing - new) can't delete freshly added
        # copies (remove-by-URI removes every occurrence of a URI).
        to_add = [u for u in new_uris if u not in existing_uris]
        if to_add:
            await manager.add_tracks(playlist_spotify_id, to_add)
        to_remove = sorted(existing_uris - new_set)
        if to_remove:
            await manager.remove_tracks(playlist_spotify_id, to_remove)

        logger.info(
            "Refreshed genre playlist %s: +%d / -%d ('%s')",
            playlist_spotify_id,
            len(to_add),
            len(to_remove),
            genre,
        )

    async def _handle_time_capsule(
        self, spotify: Any, user_id: int | None, config: dict[str, Any]
    ) -> None:
        """Create a snapshot playlist of the user's current top tracks.

        Config: ``time_range`` (default ``short_term``),
        ``playlist_name_template`` (default ``Time Capsule - {date}``).
        """
        from spotifyforge.core.discovery import DiscoveryEngine
        from spotifyforge.core.playlist_manager import PlaylistManager

        if user_id is None:
            raise ValueError("time_capsule job requires an owner")

        time_range = config.get("time_range", "short_term")
        name_template = config.get("playlist_name_template", "Time Capsule - {date}")

        tracks = await DiscoveryEngine(spotify).build_time_capsule(time_range=time_range)
        if not tracks:
            logger.warning("No tracks returned for time capsule (%s)", time_range)
            return

        playlist_name = name_template.format(date=utc_now().strftime("%Y-%m-%d"))
        await PlaylistManager(spotify).create_playlist_with_tracks(
            name=playlist_name,
            owner_id=user_id,
            tracks=tracks,
            description=f"Auto-generated time capsule ({time_range})",
            public=False,
        )

        logger.info("Created time capsule '%s' with %d tracks", playlist_name, len(tracks))


# ---------------------------------------------------------------------------
# Module-level singleton, shared by the web app and CLI
# ---------------------------------------------------------------------------

_service: SchedulerService | None = None


def get_scheduler_service() -> SchedulerService:
    """Return the process-wide :class:`SchedulerService` singleton."""
    global _service  # noqa: PLW0603
    if _service is None:
        _service = SchedulerService()
    return _service


def register_job(job: ScheduledJob) -> None:
    """Register *job* with the running scheduler singleton."""
    get_scheduler_service().add_job(job)


def unregister_job(job_pk: int) -> None:
    """Remove the job with database primary key *job_pk* from the scheduler."""
    get_scheduler_service().remove_job(job_pk)
