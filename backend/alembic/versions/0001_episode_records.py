"""create episode records

Revision ID: 0001_episode_records
Revises:
Create Date: 2026-07-26 09:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_episode_records"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "episode_records",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("slug", sa.String(length=512), nullable=False, unique=True),
        sa.Column("status", sa.String(length=64), nullable=False, index=True),
        sa.Column("source_language", sa.String(length=12), nullable=False),
        sa.Column("target_duration_seconds", sa.Integer(), nullable=False),
        sa.Column("minimum_duration_seconds", sa.Integer(), nullable=False),
        sa.Column("maximum_duration_seconds", sa.Integer(), nullable=False),
        sa.Column("current_workflow_id", sa.String(length=128), nullable=True),
        sa.Column("canonical_transcript_version_id", sa.String(length=36), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_episode_records_updated_at", "episode_records", ["updated_at"])


def downgrade() -> None:
    op.drop_index("ix_episode_records_updated_at", table_name="episode_records")
    op.drop_table("episode_records")
