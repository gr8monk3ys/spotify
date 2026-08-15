"""Tests for the unified scheduler service.

The scheduler was the most broken module in the audit (0% coverage, three
disconnected implementations, no overlap between job vocabularies). These
tests pin the rewritten behavior: one service, one JobType enum, handlers
that do real work against the fake Spotify backend.
"""

from __future__ import annotations

import pytest

from spotifyforge.core.scheduler import SchedulerService, validate_cron
from spotifyforge.models.models import JobType, ScheduledJob


class TestValidateCron:
    @pytest.mark.parametrize(
        "expr",
        ["0 8 * * 1", "*/5 * * * *", "0 0 1 1 0", "30 23 * * mon-fri"],
    )
    def test_valid_expressions(self, expr):
        assert validate_cron(expr) is not None

    @pytest.mark.parametrize(
        "expr",
        ["", "0 8 * *", "0 8 * * 1 6", "not a cron", "99 99 99 99 99", "* * * * * *"],
    )
    def test_invalid_expressions(self, expr):
        assert validate_cron(expr) is None


class TestJobRegistration:
    def _job(self, **overrides) -> ScheduledJob:
        defaults = dict(
            id=1,
            user_id=1,
            name="test job",
            job_type=JobType.sync,
            playlist_id=1,
            cron_expression="0 8 * * 1",
            enabled=True,
        )
        defaults.update(overrides)
        return ScheduledJob(**defaults)

    def test_add_job_with_bad_cron_raises(self):
        service = SchedulerService()
        with pytest.raises(ValueError, match="Invalid cron"):
            service.add_job(self._job(cron_expression="nope"))

    async def test_add_start_next_run_remove(self):
        service = SchedulerService()
        service.add_job(self._job(id=42))
        service.start()
        try:
            assert service.is_running
            assert service.next_run_time(42) is not None
            assert service.next_run_time(999) is None
            service.remove_job(42)
            assert service.next_run_time(42) is None
            # Removing an unregistered job is a no-op, not an error.
            service.remove_job(42)
        finally:
            service.stop(wait=False)

    async def test_replace_existing_job(self):
        service = SchedulerService()
        service.add_job(self._job(id=7, cron_expression="0 8 * * 1"))
        service.add_job(self._job(id=7, cron_expression="0 9 * * 2"))  # replaces
        service.start()
        try:
            assert service.next_run_time(7) is not None
        finally:
            service.stop(wait=False)


class TestJobHandlers:
    """Handler behavior against the fake Spotify backend."""

    async def test_archive_appends_source_into_target(self, fake_spotify):
        fake = fake_spotify
        fake.add_user("user1")
        fake.add_track("src1")
        fake.add_track("src2")
        fake.add_track("kept")
        fake.add_playlist("source_pl", track_ids=["src1", "src2"])
        fake.add_playlist("target_pl", track_ids=["kept"])

        service = SchedulerService()
        sp = fake.async_client("user1")
        try:
            await service._handle_archive(sp, "target_pl", {"source_playlist_id": "source_pl"})
        finally:
            await sp.close()

        assert fake.playlist_tracks["target_pl"] == ["kept", "src1", "src2"]
        # The source is untouched.
        assert fake.playlist_tracks["source_pl"] == ["src1", "src2"]

    async def test_archive_requires_config_and_target(self, fake_spotify):
        service = SchedulerService()
        sp = fake_spotify.async_client("user1")
        try:
            with pytest.raises(ValueError, match="source_playlist_id"):
                await service._handle_archive(sp, "target", {})
            with pytest.raises(ValueError, match="target playlist"):
                await service._handle_archive(sp, None, {"source_playlist_id": "s"})
        finally:
            await sp.close()

    async def test_genre_refresh_adds_before_removing(self, fake_spotify):
        """Replace mode must never leave the playlist empty mid-refresh."""
        fake = fake_spotify
        fake.add_user("user1")
        # Genre search matches tracks by name; seed matching and stale tracks.
        fake.add_track("g1", name="rock anthem one")
        fake.add_track("g2", name="rock anthem two")
        fake.add_track("stale1", name="old ballad")
        fake.add_track("overlap", name="rock overlap")
        fake.add_playlist("genre_pl", track_ids=["stale1", "overlap"])

        service = SchedulerService()
        sp = fake.async_client("user1")
        try:
            await service._handle_genre_refresh(sp, "genre_pl", {"genre": "rock", "limit": 10})
        finally:
            await sp.close()

        # Final contents: exactly the genre matches; the overlap track kept,
        # the stale track removed.
        assert set(fake.playlist_tracks["genre_pl"]) == {"g1", "g2", "overlap"}

        # Ordering proof: the add (POST tracks) happened before the remove
        # (DELETE tracks), so the playlist was never empty.
        playlist_ops = [
            (method, path)
            for method, path in fake.requests
            if path == "/v1/playlists/genre_pl/tracks" and method in ("POST", "DELETE")
        ]
        assert [m for m, _ in playlist_ops] == ["POST", "DELETE"]

    async def test_genre_refresh_no_replace_appends_only(self, fake_spotify):
        fake = fake_spotify
        fake.add_user("user1")
        fake.add_track("g1", name="jazz one")
        fake.add_track("existing", name="keeper")
        fake.add_playlist("jazz_pl", track_ids=["existing"])

        service = SchedulerService()
        sp = fake.async_client("user1")
        try:
            await service._handle_genre_refresh(sp, "jazz_pl", {"genre": "jazz", "replace": False})
        finally:
            await sp.close()

        assert fake.playlist_tracks["jazz_pl"][0] == "existing"
        assert "g1" in fake.playlist_tracks["jazz_pl"]

    async def test_genre_refresh_requires_genre(self, fake_spotify):
        service = SchedulerService()
        sp = fake_spotify.async_client("user1")
        try:
            with pytest.raises(ValueError, match="genre"):
                await service._handle_genre_refresh(sp, "pl", {})
        finally:
            await sp.close()

    async def test_time_capsule_creates_owned_playlist(self, fake_spotify, isolated_db):
        fake = fake_spotify
        fake.add_user("user1")
        fake.add_track("top1")
        fake.add_track("top2")
        fake.top_tracks["user1"] = ["top1", "top2"]

        service = SchedulerService()
        sp = fake.async_client("user1")
        try:
            await service._handle_time_capsule(sp, 5, {"time_range": "short_term"})
        finally:
            await sp.close()

        # A new playlist exists on (fake) Spotify with the top tracks...
        created = [p for p in fake.playlists if p.startswith("pl_created_")]
        assert len(created) == 1
        assert fake.playlist_tracks[created[0]] == ["top1", "top2"]
        assert fake.playlists[created[0]]["public"] is False

        # ...and the local row belongs to the job's owner, not owner 0.
        from sqlmodel import Session, select

        from spotifyforge.db.engine import get_engine
        from spotifyforge.models.models import Playlist

        with Session(get_engine()) as session:
            row = session.exec(select(Playlist).where(Playlist.spotify_id == created[0])).one()
            assert row.owner_id == 5

    async def test_deduplicate_handler(self, fake_spotify):
        fake = fake_spotify
        fake.add_user("user1")
        for t in ("a", "b", "c"):
            fake.add_track(t)
        fake.add_playlist("dup_pl", track_ids=["a", "b", "a", "c", "b"])

        service = SchedulerService()
        sp = fake.async_client("user1")
        try:
            await service._handle_deduplicate(sp, "dup_pl")
        finally:
            await sp.close()

        assert fake.playlist_tracks["dup_pl"] == ["a", "b", "c"]

    async def test_sync_handler_requires_playlist_and_owner(self, fake_spotify):
        service = SchedulerService()
        sp = fake_spotify.async_client("user1")
        try:
            with pytest.raises(ValueError):
                await service._handle_sync(sp, None, "pl")
            with pytest.raises(ValueError):
                await service._handle_sync(sp, 1, None)
        finally:
            await sp.close()


class TestModuleSingleton:
    def test_get_scheduler_service_is_singleton(self, monkeypatch):
        from spotifyforge.core import scheduler as mod

        monkeypatch.setattr(mod, "_service", None)
        first = mod.get_scheduler_service()
        assert mod.get_scheduler_service() is first
