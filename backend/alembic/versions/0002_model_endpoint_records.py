"""create model endpoint records

Revision ID: 0002_model_endpoint_records
Revises: 0001_episode_records
Create Date: 2026-07-26 09:45:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_model_endpoint_records"
down_revision: str | None = "0001_episode_records"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_endpoint_records",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("provider_type", sa.String(length=64), nullable=False, index=True),
        sa.Column("base_url", sa.String(length=1024), nullable=True),
        sa.Column("credential_reference", sa.String(length=512), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("health_status", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_model_endpoint_records_updated_at",
        "model_endpoint_records",
        ["updated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_model_endpoint_records_updated_at", table_name="model_endpoint_records")
    op.drop_table("model_endpoint_records")
