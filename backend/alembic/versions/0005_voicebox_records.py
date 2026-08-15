"""create voicebox records

Revision ID: 0005_voicebox_records
Revises: 0004_audit_event_records
Create Date: 2026-07-26 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_voicebox_records"
down_revision: str | None = "0004_audit_event_records"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "voicebox_endpoint_records",
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
        "ix_voicebox_endpoint_records_adapter_type",
        "voicebox_endpoint_records",
        ["adapter_type"],
    )
    op.create_index(
        "ix_voicebox_endpoint_records_updated_at",
        "voicebox_endpoint_records",
        ["updated_at"],
    )
    op.create_table(
        "voice_profile_records",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("voicebox_endpoint_id", sa.String(length=128), nullable=False),
        sa.Column("voice_id", sa.String(length=256), nullable=False),
        sa.Column("language", sa.String(length=12), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_voice_profile_records_language",
        "voice_profile_records",
        ["language"],
    )
    op.create_index(
        "ix_voice_profile_records_updated_at",
        "voice_profile_records",
        ["updated_at"],
    )
    op.create_index(
        "ix_voice_profile_records_voicebox_endpoint_id",
        "voice_profile_records",
        ["voicebox_endpoint_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_voice_profile_records_voicebox_endpoint_id",
        table_name="voice_profile_records",
    )
    op.drop_index("ix_voice_profile_records_updated_at", table_name="voice_profile_records")
    op.drop_index("ix_voice_profile_records_language", table_name="voice_profile_records")
    op.drop_table("voice_profile_records")
    op.drop_index(
        "ix_voicebox_endpoint_records_updated_at",
        table_name="voicebox_endpoint_records",
    )
    op.drop_index(
        "ix_voicebox_endpoint_records_adapter_type",
        table_name="voicebox_endpoint_records",
    )
    op.drop_table("voicebox_endpoint_records")
