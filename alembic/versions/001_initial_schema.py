"""Initial schema.

Revision ID: 001_initial
Revises:
Create Date: 2026-02-16
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Users
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("spotify_id", sa.String(length=64), nullable=False, unique=True, index=True),
        sa.Column("display_name", sa.String(length=256), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("access_token_enc", sa.Text(), nullable=True, index=True),
        sa.Column("refresh_token_enc", sa.Text(), nullable=True),
        sa.Column("token_expiry", sa.DateTime(), nullable=True),
        sa.Column("token_hash", sa.String(), nullable=True, index=True),
        sa.Column("is_premium", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )

    # Tracks
    op.create_table(
        "tracks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("spotify_id", sa.String(length=64), nullable=False, unique=True, index=True),
        sa.Column("name", sa.String(length=512), nullable=False),
        sa.Column("artist_names", sa.JSON(), nullable=True),
        sa.Column("album_name", sa.String(length=512), nullable=True),
        sa.Column("album_id", sa.String(length=64), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("popularity", sa.Integer(), nullable=True),
        sa.Column("isrc", sa.String(length=16), nullable=True),
        sa.Column("cached_at", sa.DateTime(), nullable=False),
    )

    # Artists
    op.create_table(
        "artists",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("spotify_id", sa.String(length=64), nullable=False, unique=True, index=True),
        sa.Column("name", sa.String(length=512), nullable=False),
        sa.Column("genres", sa.JSON(), nullable=True),
        sa.Column("popularity", sa.Integer(), nullable=True),
        sa.Column("cached_at", sa.DateTime(), nullable=False),
    )

    # Albums
    op.create_table(
        "albums",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("spotify_id", sa.String(length=64), nullable=False, unique=True, index=True),
        sa.Column("name", sa.String(length=512), nullable=False),
        sa.Column("artist_ids", sa.JSON(), nullable=True),
        sa.Column("release_date", sa.String(length=16), nullable=True),
        sa.Column("total_tracks", sa.Integer(), nullable=True),
        sa.Column("cached_at", sa.DateTime(), nullable=False),
    )

    # Audio Features
    op.create_table(
        "audio_features",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("track_id", sa.Integer(), sa.ForeignKey("tracks.id"), nullable=False, index=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("danceability", sa.Float(), nullable=True),
        sa.Column("energy", sa.Float(), nullable=True),
        sa.Column("valence", sa.Float(), nullable=True),
        sa.Column("tempo", sa.Float(), nullable=True),
        sa.Column("key", sa.Integer(), nullable=True),
        sa.Column("mode", sa.Integer(), nullable=True),
        sa.Column("loudness", sa.Float(), nullable=True),
        sa.Column("speechiness", sa.Float(), nullable=True),
        sa.Column("acousticness", sa.Float(), nullable=True),
        sa.Column("instrumentalness", sa.Float(), nullable=True),
        sa.Column("liveness", sa.Float(), nullable=True),
        sa.Column("cached_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("track_id", "source", name="uq_audio_features_track_source"),
    )

    # Playlists
    op.create_table(
        "playlists",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("spotify_id", sa.String(length=64), nullable=False, index=True),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("name", sa.String(length=512), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("public", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("collaborative", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("snapshot_id", sa.String(length=128), nullable=True),
        sa.Column("follower_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("track_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_synced_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True, index=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )

    # Playlist-Track association
    op.create_table(
        "playlist_tracks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "playlist_id", sa.Integer(), sa.ForeignKey("playlists.id"), nullable=False, index=True
        ),
        sa.Column("track_id", sa.Integer(), sa.ForeignKey("tracks.id"), nullable=False, index=True),
        sa.Column("position", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("added_at", sa.DateTime(), nullable=True),
        sa.Column("added_by", sa.String(length=64), nullable=True),
        sa.UniqueConstraint("playlist_id", "track_id", name="uq_playlist_track"),
        sa.Index("ix_playlist_tracks_playlist_position", "playlist_id", "position"),
    )

    # Scheduled Jobs
    op.create_table(
        "scheduled_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("job_type", sa.String(), nullable=False),
        sa.Column(
            "playlist_id", sa.Integer(), sa.ForeignKey("playlists.id"), nullable=True, index=True
        ),
        sa.Column("config", sa.JSON(), nullable=True),
        sa.Column("cron_expression", sa.String(length=128), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1"), index=True),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.Column("next_run_at", sa.DateTime(), nullable=True),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )

    # Curation Rules
    op.create_table(
        "curation_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column(
            "playlist_id", sa.Integer(), sa.ForeignKey("playlists.id"), nullable=True, index=True
        ),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("rule_type", sa.String(), nullable=False),
        sa.Column("conditions", sa.JSON(), nullable=True),
        sa.Column("actions", sa.JSON(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("curation_rules")
    op.drop_table("scheduled_jobs")
    op.drop_table("playlist_tracks")
    op.drop_table("playlists")
    op.drop_table("audio_features")
    op.drop_table("albums")
    op.drop_table("artists")
    op.drop_table("tracks")
    op.drop_table("users")
