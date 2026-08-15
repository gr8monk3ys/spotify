"""Unify scheduled-job types and allow duplicate playlist tracks.

Revision ID: 002_unify_jobs
Revises: 001_initial
Create Date: 2026-08-14

Two fixes from the functionality audit:

* ``playlist_tracks`` had a UNIQUE(playlist_id, track_id) constraint, but a
  Spotify playlist can legally contain the same track at several positions —
  syncing any playlist with duplicates (exactly the ones this product
  targets) failed with IntegrityError. The constraint is dropped.

* ``users.token_hash`` is dropped: it indexed bearer tokens for an auth
  path replaced by signed session cookies.

* ``scheduled_jobs.job_type`` values are converted to the unified job
  vocabulary shared by the API, scheduler, and CLI. ``stats_snapshot`` and
  ``health_check`` rows are deleted: no handler for them ever existed, so
  no such job has ever run.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "002_unify_jobs"
down_revision: str | None = "001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JOB_TYPE_FORWARD = {
    "playlist_sync": "sync",
    "playlist_update": "sync",
    "playlist_archive": "archive",
    "discovery_refresh": "genre_refresh",
}


def upgrade() -> None:
    with op.batch_alter_table("playlist_tracks") as batch:
        batch.drop_constraint("uq_playlist_track", type_="unique")

    # token_hash supported the removed bearer-token auth path; sessions are
    # signed cookies now and Spotify tokens live in the *_enc columns.
    with op.batch_alter_table("users") as batch:
        batch.drop_index("ix_users_token_hash")
        batch.drop_column("token_hash")

    for old, new in _JOB_TYPE_FORWARD.items():
        op.execute(
            sa.text("UPDATE scheduled_jobs SET job_type = :new WHERE job_type = :old").bindparams(
                new=new, old=old
            )
        )
    op.execute(
        sa.text("DELETE FROM scheduled_jobs WHERE job_type IN ('stats_snapshot', 'health_check')")
    )


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("token_hash", sa.String(), nullable=True))
        batch.create_index("ix_users_token_hash", ["token_hash"])

    for old, new in _JOB_TYPE_FORWARD.items():
        if old == "playlist_update":  # sync maps back to playlist_sync, not playlist_update
            continue
        op.execute(
            sa.text("UPDATE scheduled_jobs SET job_type = :old WHERE job_type = :new").bindparams(
                new=new, old=old
            )
        )

    with op.batch_alter_table("playlist_tracks") as batch:
        batch.create_unique_constraint("uq_playlist_track", ["playlist_id", "track_id"])
