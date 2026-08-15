"""create research evidence projection records

Revision ID: 0009_research_evidence_projections
Revises: 0008_project_records
Create Date: 2026-07-27 11:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_research_evidence_projections"
down_revision: str | None = "0008_project_records"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "research_source_records",
        sa.Column("id", sa.String(length=256), primary_key=True),
        sa.Column("episode_id", sa.String(length=36), primary_key=True),
        sa.Column("url", sa.String(length=2048), nullable=True),
        sa.Column("title", sa.String(length=1024), nullable=False),
        sa.Column("publisher", sa.String(length=512), nullable=True),
        sa.Column("published_at", sa.String(length=128), nullable=True),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(length=256), nullable=True),
        sa.Column("source_type", sa.String(length=128), nullable=False),
        sa.Column("credibility_score", sa.Float(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_research_source_records_episode_id",
        "research_source_records",
        ["episode_id"],
    )
    op.create_index(
        "ix_research_source_records_source_type",
        "research_source_records",
        ["source_type"],
    )
    op.create_index(
        "ix_research_source_records_updated_at",
        "research_source_records",
        ["updated_at"],
    )

    op.create_table(
        "evidence_claim_records",
        sa.Column("id", sa.String(length=256), primary_key=True),
        sa.Column("episode_id", sa.String(length=36), primary_key=True),
        sa.Column("statement", sa.String(length=4096), nullable=False),
        sa.Column("claim_type", sa.String(length=128), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_evidence_claim_records_claim_type",
        "evidence_claim_records",
        ["claim_type"],
    )
    op.create_index(
        "ix_evidence_claim_records_episode_id",
        "evidence_claim_records",
        ["episode_id"],
    )
    op.create_index(
        "ix_evidence_claim_records_status",
        "evidence_claim_records",
        ["status"],
    )
    op.create_index(
        "ix_evidence_claim_records_updated_at",
        "evidence_claim_records",
        ["updated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_evidence_claim_records_updated_at", table_name="evidence_claim_records")
    op.drop_index("ix_evidence_claim_records_status", table_name="evidence_claim_records")
    op.drop_index("ix_evidence_claim_records_episode_id", table_name="evidence_claim_records")
    op.drop_index("ix_evidence_claim_records_claim_type", table_name="evidence_claim_records")
    op.drop_table("evidence_claim_records")

    op.drop_index("ix_research_source_records_updated_at", table_name="research_source_records")
    op.drop_index("ix_research_source_records_source_type", table_name="research_source_records")
    op.drop_index("ix_research_source_records_episode_id", table_name="research_source_records")
    op.drop_table("research_source_records")
