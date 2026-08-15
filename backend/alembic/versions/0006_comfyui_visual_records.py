"""create comfyui visual records

Revision ID: 0006_comfyui_visual_records
Revises: 0005_voicebox_records
Create Date: 2026-07-26 13:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_comfyui_visual_records"
down_revision: str | None = "0005_voicebox_records"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "comfyui_endpoint_records",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("adapter_type", sa.String(length=64), nullable=False),
        sa.Column("base_url", sa.String(length=1024), nullable=True),
        sa.Column("credential_reference", sa.String(length=512), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("health_status", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_comfyui_endpoint_records_adapter_type",
        "comfyui_endpoint_records",
        ["adapter_type"],
    )
    op.create_index(
        "ix_comfyui_endpoint_records_updated_at",
        "comfyui_endpoint_records",
        ["updated_at"],
    )
    op.create_table(
        "comfyui_workflow_records",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("workflow_type", sa.String(length=128), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("comfyui_endpoint_id", sa.String(length=128), nullable=False),
        sa.Column("output_asset_type", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_comfyui_workflow_records_comfyui_endpoint_id",
        "comfyui_workflow_records",
        ["comfyui_endpoint_id"],
    )
    op.create_index(
        "ix_comfyui_workflow_records_type",
        "comfyui_workflow_records",
        ["workflow_type"],
    )
    op.create_index(
        "ix_comfyui_workflow_records_updated_at",
        "comfyui_workflow_records",
        ["updated_at"],
    )
    op.create_table(
        "visual_profile_records",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("character_name", sa.String(length=256), nullable=False),
        sa.Column("primary_workflow_id", sa.String(length=128), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_visual_profile_records_primary_workflow_id",
        "visual_profile_records",
        ["primary_workflow_id"],
    )
    op.create_index(
        "ix_visual_profile_records_updated_at",
        "visual_profile_records",
        ["updated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_visual_profile_records_updated_at", table_name="visual_profile_records")
    op.drop_index(
        "ix_visual_profile_records_primary_workflow_id",
        table_name="visual_profile_records",
    )
    op.drop_table("visual_profile_records")
    op.drop_index(
        "ix_comfyui_workflow_records_updated_at",
        table_name="comfyui_workflow_records",
    )
    op.drop_index(
        "ix_comfyui_workflow_records_type",
        table_name="comfyui_workflow_records",
    )
    op.drop_index(
        "ix_comfyui_workflow_records_comfyui_endpoint_id",
        table_name="comfyui_workflow_records",
    )
    op.drop_table("comfyui_workflow_records")
    op.drop_index(
        "ix_comfyui_endpoint_records_updated_at",
        table_name="comfyui_endpoint_records",
    )
    op.drop_index(
        "ix_comfyui_endpoint_records_adapter_type",
        table_name="comfyui_endpoint_records",
    )
    op.drop_table("comfyui_endpoint_records")
