"""Initial schema.

Revision ID: 001_initial
Revises:
Create Date: 2026-02-16
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
import sqlmodel

revision: str = "001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Users
    op.create_table(
        "user",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("spotify_id", sa.String(), nullable=False, unique=True, index=True),
        sa.Column("display_name", sa.String(), nullable=True),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("is_premium", sa.Boolean(), default=False),
        sa.Column("access_token_enc", sa.String(), nullable=True, index=True),
        sa.Column("refresh_token_enc", sa.String(), nullable=True),
        sa.Column("token_hash", sa.String(), nullable=True, index=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )

    # Artists
    op.create_table(
        "artist",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("spotify_id", sa.String(), nullable=False, unique=True, index=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("genres", sa.Text(), nullable=True),
        sa.Column("popularity", sa.Integer(), nullable=True),
        sa.Column("followers", sa.Integer(), nullable=True),
        sa.Column("image_url", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )

    # Tracks
    op.create_table(
        "track",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("spotify_id", sa.String(), nullable=False, unique=True, index=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("artist_ids", sa.Text(), nullable=True),
        sa.Column("album_name", sa.String(), nullable=True),
        sa.Column("album_id", sa.String(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("popularity", sa.Integer(), nullable=True),
        sa.Column("explicit", sa.Boolean(), default=False),
        sa.Column("preview_url", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )

    # Audio Features
    op.create_table(
        "audiofeatures",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("track_id", sa.String(), nullable=False, unique=True, index=True),
        sa.Column("danceability", sa.Float(), nullable=True),
        sa.Column("energy", sa.Float(), nullable=True),
        sa.Column("key", sa.Integer(), nullable=True),
        sa.Column("loudness", sa.Float(), nullable=True),
        sa.Column("mode", sa.Integer(), nullable=True),
        sa.Column("speechiness", sa.Float(), nullable=True),
        sa.Column("acousticness", sa.Float(), nullable=True),
        sa.Column("instrumentalness", sa.Float(), nullable=True),
        sa.Column("liveness", sa.Float(), nullable=True),
        sa.Column("valence", sa.Float(), nullable=True),
        sa.Column("tempo", sa.Float(), nullable=True),
        sa.Column("time_signature", sa.Integer(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
    )

    # Playlists
    op.create_table(
        "playlist",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("spotify_id", sa.String(), nullable=False, index=True),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False, index=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("public", sa.Boolean(), default=True),
        sa.Column("snapshot_id", sa.String(), nullable=True),
        sa.Column("track_count", sa.Integer(), default=0),
        sa.Column("deleted_at", sa.DateTime(), nullable=True, index=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )

    # Playlist-Track association
    op.create_table(
        "playlisttrack",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("playlist_id", sa.Integer(), sa.ForeignKey("playlist.id"), nullable=False, index=True),
        sa.Column("track_id", sa.Integer(), sa.ForeignKey("track.id"), nullable=False, index=True),
        sa.Column("position", sa.Integer(), nullable=True),
        sa.Column("added_at", sa.DateTime(), nullable=True),
    )

    # Scheduled Jobs
    op.create_table(
        "scheduledjob",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False, index=True),
        sa.Column("playlist_id", sa.Integer(), sa.ForeignKey("playlist.id"), nullable=True, index=True),
        sa.Column("job_type", sa.String(), nullable=False),
        sa.Column("cron_expression", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), default=True, index=True),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.Column("failure_count", sa.Integer(), default=0),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("scheduledjob")
    op.drop_table("playlisttrack")
    op.drop_table("playlist")
    op.drop_table("audiofeatures")
    op.drop_table("track")
    op.drop_table("artist")
    op.drop_table("user")
