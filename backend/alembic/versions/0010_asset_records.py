"""create asset records

Revision ID: 0010_asset_records
Revises: 0009_research_evidence_projections
Create Date: 2026-07-27 12:10:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_asset_records"
down_revision: str | None = "0009_research_evidence_projections"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "asset_records",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("episode_id", sa.String(length=36), nullable=False),
        sa.Column("asset_type", sa.String(length=64), nullable=False),
        sa.Column("language", sa.String(length=12), nullable=True),
        sa.Column("source_entity_type", sa.String(length=128), nullable=False),
        sa.Column("source_entity_id", sa.String(length=128), nullable=False),
        sa.Column("storage_uri", sa.String(length=2048), nullable=True),
        sa.Column("mime_type", sa.String(length=256), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("fps", sa.Float(), nullable=True),
        sa.Column("checksum", sa.String(length=256), nullable=True),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_asset_records_asset_type", "asset_records", ["asset_type"])
    op.create_index("ix_asset_records_episode_id", "asset_records", ["episode_id"])
    op.create_index("ix_asset_records_language", "asset_records", ["language"])
    op.create_index(
        "ix_asset_records_source_entity_id",
        "asset_records",
        ["source_entity_id"],
    )
    op.create_index(
        "ix_asset_records_source_entity_type",
        "asset_records",
        ["source_entity_type"],
    )
    op.create_index("ix_asset_records_status", "asset_records", ["status"])
    op.create_index("ix_asset_records_updated_at", "asset_records", ["updated_at"])


def downgrade() -> None:
    op.drop_index("ix_asset_records_updated_at", table_name="asset_records")
    op.drop_index("ix_asset_records_status", table_name="asset_records")
    op.drop_index("ix_asset_records_source_entity_type", table_name="asset_records")
    op.drop_index("ix_asset_records_source_entity_id", table_name="asset_records")
    op.drop_index("ix_asset_records_language", table_name="asset_records")
    op.drop_index("ix_asset_records_episode_id", table_name="asset_records")
    op.drop_index("ix_asset_records_asset_type", table_name="asset_records")
    op.drop_table("asset_records")
