"""Scheduling service for automated SpotifyForge jobs.

Wraps APScheduler (v3.x) to provide lifecycle management, dynamic job
registration from the database, and dispatch to the appropriate core
service methods when jobs fire.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlmodel import Session, select

from spotifyforge.models.models import Playlist, ScheduledJob

if TYPE_CHECKING:
    from tekore import Spotify

logger = logging.getLogger(__name__)

# Job types recognised by the scheduler dispatcher.
SUPPORTED_JOB_TYPES: frozenset[str] = frozenset(
    {
        "sync_playlist",
        "discover_weekly_archive",
        "time_capsule",
        "deduplicate",
        "genre_refresh",
    }
)


class SchedulerService:
    """Manages scheduled automation jobs backed by APScheduler.

    Parameters
    ----------
    spotify:
        An authenticated :class:`tekore.Spotify` client.  Passed through to
        the core service classes that execute actual work.
    """

    def __init__(self, spotify: Spotify) -> None:
        self._sp = spotify
        self._scheduler = AsyncIOScheduler()
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the APScheduler event loop.

        Safe to call multiple times; subsequent calls are no-ops.
        """
        if self._running:
            logger.debug("Scheduler is already running")
            return

        self._scheduler.start()
        self._running = True
        logger.info("Scheduler started")

    def stop(self, wait: bool = True) -> None:
        """Shut down the scheduler gracefully.

        Parameters
        ----------
        wait:
            If *True* (default), wait for currently executing jobs to
            complete before returning.
        """
        if not self._running:
            logger.debug("Scheduler is not running")
            return

        self._scheduler.shutdown(wait=wait)
        self._running = False
        logger.info("Scheduler stopped (wait=%s)", wait)

    @property
    def is_running(self) -> bool:
        """Whether the scheduler event loop is active."""
        return self._running

    # ------------------------------------------------------------------
    # Job registration
    # ------------------------------------------------------------------

    def add_job(self, scheduled_job: ScheduledJob) -> None:
        """Register a :class:`ScheduledJob` with APScheduler.

        The job's ``cron_expression`` is parsed into an APScheduler
        :class:`CronTrigger`.  Standard 5-field cron expressions are
        supported (``minute hour day month day_of_week``).

        If a job with the same ID already exists in the scheduler it is
        replaced silently.
        """
        job_id = self._make_job_id(scheduled_job)

        if scheduled_job.job_type not in SUPPORTED_JOB_TYPES:
            logger.warning(
                "Unsupported job type '%s' for job %s — skipping",
                scheduled_job.job_type,
                job_id,
            )
            return

        trigger = self._parse_cron(scheduled_job.cron_expression)
        if trigger is None:
            logger.error(
                "Invalid cron expression '%s' for job %s — skipping",
                scheduled_job.cron_expression,
                job_id,
            )
            return

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

    def remove_job(self, job_id: int) -> None:
        """Remove a scheduled job by its database primary key.

        If the job is not currently registered with the scheduler this is a
        no-op (a warning is logged).
        """
        scheduler_job_id = f"spotifyforge_job_{job_id}"
        try:
            self._scheduler.remove_job(scheduler_job_id)
            logger.info("Removed job %s", scheduler_job_id)
        except Exception:
            logger.warning("Job %s was not found in the scheduler", scheduler_job_id)

    # ------------------------------------------------------------------
    # Bulk loading from the database
    # ------------------------------------------------------------------

    def load_jobs_from_db(self, session: Session) -> int:
        """Load all enabled :class:`ScheduledJob` records and register them.

        Parameters
        ----------
        session:
            A synchronous SQLModel session.

        Returns
        -------
        The number of jobs successfully registered.
        """
        statement = select(ScheduledJob).where(ScheduledJob.enabled == True)  # noqa: E712
        results = session.exec(statement)
        jobs: list[ScheduledJob] = list(results.all())

        registered = 0
        for job in jobs:
            try:
                self.add_job(job)
                registered += 1
            except Exception as exc:
                logger.error("Failed to register job %s: %s", job.id, exc)

        logger.info("Loaded %d/%d enabled jobs from the database", registered, len(jobs))
        return registered

    # ------------------------------------------------------------------
    # Job execution dispatcher
    # ------------------------------------------------------------------

    async def _execute_job(self, job_id: int) -> None:
        """Callback invoked by APScheduler when a job triggers.

        Loads the :class:`ScheduledJob` row from the DB, determines the
        job type, and dispatches to the appropriate core service method.
        """
        from spotifyforge.db.engine import get_session

        logger.info("Executing scheduled job %d", job_id)

        with get_session() as session:
            scheduled_job = session.get(ScheduledJob, job_id)
            if scheduled_job is None:
                logger.error("Scheduled job %d not found in database", job_id)
                return

            if not scheduled_job.enabled:
                logger.info("Scheduled job %d is disabled — skipping", job_id)
                return

            job_type: str = scheduled_job.job_type
            playlist_id: str | None = self._resolve_playlist_spotify_id(
                session, scheduled_job
            )
            config: dict[str, Any] = (
                scheduled_job.config if scheduled_job.config else {}
            )

        # Dispatch to the appropriate handler.
        try:
            if job_type == "sync_playlist":
                await self._handle_sync_playlist(playlist_id)

            elif job_type == "discover_weekly_archive":
                await self._handle_discover_weekly_archive(playlist_id, config)

            elif job_type == "time_capsule":
                await self._handle_time_capsule(config)

            elif job_type == "deduplicate":
                await self._handle_deduplicate(playlist_id)

            elif job_type == "genre_refresh":
                await self._handle_genre_refresh(playlist_id, config)

            else:
                logger.error("Unhandled job type: %s", job_type)
                return

        except Exception as exc:
            logger.exception("Job %d (%s) failed: %s", job_id, job_type, exc)
            return

        # Update last_run_at timestamp.
        with get_session() as session:
            scheduled_job = session.get(ScheduledJob, job_id)
            if scheduled_job is not None:
                scheduled_job.last_run_at = datetime.utcnow()
                scheduled_job.updated_at = datetime.utcnow()
                session.add(scheduled_job)
                session.commit()

        logger.info("Scheduled job %d (%s) completed successfully", job_id, job_type)

    # ------------------------------------------------------------------
    # Individual job handlers
    # ------------------------------------------------------------------

    async def _handle_sync_playlist(self, playlist_id: str | None) -> None:
        """Sync a playlist from Spotify to the local database."""
        from spotifyforge.core.playlist_manager import PlaylistManager

        if not playlist_id:
            logger.error("sync_playlist job requires a playlist_id")
            return

        manager = PlaylistManager(self._sp)
        await manager.sync_playlist(playlist_id)

    async def _handle_discover_weekly_archive(
        self,
        playlist_id: str | None,
        config: dict[str, Any],
    ) -> None:
        """Archive the current Discover Weekly into a target playlist.

        Expected config keys:
        - ``source_playlist_id``: The Spotify ID of the Discover Weekly
          playlist to archive from.
        """
        from spotifyforge.core.playlist_manager import PlaylistManager

        source_id = config.get("source_playlist_id")
        if not source_id:
            logger.error(
                "discover_weekly_archive requires 'source_playlist_id' in config"
            )
            return
        if not playlist_id:
            logger.error(
                "discover_weekly_archive requires a target playlist_id"
            )
            return

        manager = PlaylistManager(self._sp)
        items = await manager.get_playlist_tracks(source_id)
        uris = [
            item.track.uri
            for item in items
            if item.track is not None and item.track.uri is not None
        ]

        if uris:
            await manager.add_tracks(playlist_id, uris)
            logger.info(
                "Archived %d tracks from %s to %s", len(uris), source_id, playlist_id
            )
        else:
            logger.warning("No tracks found in source playlist %s", source_id)

    async def _handle_time_capsule(self, config: dict[str, Any]) -> None:
        """Create a time capsule playlist from the user's top tracks.

        Expected config keys:
        - ``time_range``: One of "short_term", "medium_term", "long_term".
          Defaults to "short_term".
        - ``playlist_name_template``: Optional name template for the new
          playlist (defaults to "Time Capsule - {date}").
        """
        from spotifyforge.core.discovery import DiscoveryEngine
        from spotifyforge.core.playlist_manager import PlaylistManager

        time_range = config.get("time_range", "short_term")
        name_template = config.get(
            "playlist_name_template", "Time Capsule - {date}"
        )

        engine = DiscoveryEngine(self._sp)
        tracks = await engine.build_time_capsule(time_range=time_range)

        if not tracks:
            logger.warning("No tracks returned for time capsule (%s)", time_range)
            return

        playlist_name = name_template.format(
            date=datetime.utcnow().strftime("%Y-%m-%d")
        )

        manager = PlaylistManager(self._sp)
        new_playlist = await manager.create_playlist(
            name=playlist_name,
            description=f"Auto-generated time capsule ({time_range})",
            public=False,
        )

        uris = [t.uri for t in tracks if t.uri is not None]
        if uris:
            await manager.add_tracks(new_playlist.spotify_id, uris)

        logger.info(
            "Created time capsule '%s' with %d tracks", playlist_name, len(uris)
        )

    async def _handle_deduplicate(self, playlist_id: str | None) -> None:
        """Remove duplicate tracks from a playlist."""
        from spotifyforge.core.playlist_manager import PlaylistManager

        if not playlist_id:
            logger.error("deduplicate job requires a playlist_id")
            return

        manager = PlaylistManager(self._sp)
        removed = await manager.deduplicate(playlist_id)
        logger.info("Deduplicated playlist %s — removed %d duplicates", playlist_id, removed)

    async def _handle_genre_refresh(
        self,
        playlist_id: str | None,
        config: dict[str, Any],
    ) -> None:
        """Refresh a playlist with fresh tracks from a genre search.

        Expected config keys:
        - ``genre``: The genre seed string (e.g. "indie-rock").
        - ``limit``: Maximum tracks to fetch (default 50).
        - ``replace``: If true, remove existing tracks first (default true).
        """
        from spotifyforge.core.discovery import DiscoveryEngine
        from spotifyforge.core.playlist_manager import PlaylistManager

        genre = config.get("genre")
        if not genre:
            logger.error("genre_refresh requires 'genre' in config")
            return
        if not playlist_id:
            logger.error("genre_refresh requires a playlist_id")
            return

        limit = int(config.get("limit", 50))
        replace = bool(config.get("replace", True))

        engine = DiscoveryEngine(self._sp)
        tracks = await engine.build_genre_playlist(genre=genre, limit=limit)

        if not tracks:
            logger.warning("No tracks found for genre '%s'", genre)
            return

        manager = PlaylistManager(self._sp)

        if replace:
            # Remove all existing tracks first.
            existing_items = await manager.get_playlist_tracks(playlist_id)
            existing_uris = [
                item.track.uri
                for item in existing_items
                if item.track is not None and item.track.uri is not None
            ]
            if existing_uris:
                await manager.remove_tracks(playlist_id, existing_uris)

        new_uris = [t.uri for t in tracks if t.uri is not None]
        if new_uris:
            await manager.add_tracks(playlist_id, new_uris)

        logger.info(
            "Refreshed genre playlist %s with %d '%s' tracks",
            playlist_id,
            len(new_uris),
            genre,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_job_id(scheduled_job: ScheduledJob) -> str:
        """Construct a stable APScheduler job ID from the DB model."""
        return f"spotifyforge_job_{scheduled_job.id}"

    @staticmethod
    def _parse_cron(expression: str) -> CronTrigger | None:
        """Parse a 5-field cron expression into an APScheduler CronTrigger.

        Expected format: ``minute hour day month day_of_week``

        Returns *None* if the expression cannot be parsed.
        """
        parts = expression.strip().split()
        if len(parts) != 5:
            logger.error(
                "Cron expression must have 5 fields, got %d: '%s'",
                len(parts),
                expression,
            )
            return None

        try:
            return CronTrigger(
                minute=parts[0],
                hour=parts[1],
                day=parts[2],
                month=parts[3],
                day_of_week=parts[4],
            )
        except (ValueError, TypeError) as exc:
            logger.error("Failed to parse cron expression '%s': %s", expression, exc)
            return None

    @staticmethod
    def _resolve_playlist_spotify_id(
        session: Session,
        scheduled_job: ScheduledJob,
    ) -> str | None:
        """Resolve the Spotify playlist ID from a ScheduledJob.

        The ``ScheduledJob.playlist_id`` is a foreign key to the local
        ``Playlist`` table.  This helper looks up the corresponding
        ``Playlist.spotify_id``.
        """
        if scheduled_job.playlist_id is None:
            return None

        playlist = session.get(Playlist, scheduled_job.playlist_id)
        if playlist is None:
            logger.warning(
                "Playlist row %d referenced by job %d not found",
                scheduled_job.playlist_id,
                scheduled_job.id,
            )
            return None

        return playlist.spotify_id
