"""create participant profile records

Revision ID: 0003_participant_profile_records
Revises: 0002_model_endpoint_records
Create Date: 2026-07-26 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_participant_profile_records"
down_revision: str | None = "0002_model_endpoint_records"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "participant_profile_records",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("display_name", sa.String(length=256), nullable=False),
        sa.Column("participant_type", sa.String(length=64), nullable=False, index=True),
        sa.Column("model_endpoint_id", sa.String(length=128), nullable=False, index=True),
        sa.Column("model_id", sa.String(length=256), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_participant_profile_records_updated_at",
        "participant_profile_records",
        ["updated_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_participant_profile_records_updated_at",
        table_name="participant_profile_records",
    )
    op.drop_table("participant_profile_records")
