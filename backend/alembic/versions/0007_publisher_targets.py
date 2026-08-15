"""create publisher target records

Revision ID: 0007_publisher_targets
Revises: 0006_comfyui_visual_records
Create Date: 2026-07-26 17:50:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_publisher_targets"
down_revision: str | None = "0006_comfyui_visual_records"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "publisher_target_records",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("platform", sa.String(length=64), nullable=False),
        sa.Column("adapter_type", sa.String(length=64), nullable=False),
        sa.Column("base_url", sa.String(length=1024), nullable=True),
        sa.Column("credential_reference", sa.String(length=512), nullable=True),
        sa.Column("channel_id", sa.String(length=256), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("health_status", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_publisher_target_records_adapter_type",
        "publisher_target_records",
        ["adapter_type"],
    )
    op.create_index(
        "ix_publisher_target_records_platform",
        "publisher_target_records",
        ["platform"],
    )
    op.create_index(
        "ix_publisher_target_records_updated_at",
        "publisher_target_records",
        ["updated_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_publisher_target_records_updated_at",
        table_name="publisher_target_records",
    )
    op.drop_index(
        "ix_publisher_target_records_platform",
        table_name="publisher_target_records",
    )
    op.drop_index(
        "ix_publisher_target_records_adapter_type",
        table_name="publisher_target_records",
    )
    op.drop_table("publisher_target_records")
