"""Add curation eval logs, webhook configs, and curation_apply job type.

Revision ID: 002_curation_notifications
Revises: 001_initial
Create Date: 2026-02-27
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "002_curation_notifications"
down_revision: str | None = "001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "curation_eval_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column(
            "playlist_id", sa.Integer(), sa.ForeignKey("playlists.id"), nullable=False, index=True
        ),
        sa.Column("rules_applied", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("tracks_before", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("tracks_after", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column("executed_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "webhook_configs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("events", sa.JSON(), nullable=True),
        sa.Column("secret", sa.String(length=256), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("webhook_configs")
    op.drop_table("curation_eval_logs")
