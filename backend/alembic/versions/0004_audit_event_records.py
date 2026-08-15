"""create audit event records

Revision ID: 0004_audit_event_records
Revises: 0003_participant_profile_records
Create Date: 2026-07-26 11:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_audit_event_records"
down_revision: str | None = "0003_participant_profile_records"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_event_records",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("episode_id", sa.String(length=36), nullable=True),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False, server_default="system"),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_audit_event_records_created_at",
        "audit_event_records",
        ["created_at"],
    )
    op.create_index(
        "ix_audit_event_records_episode_id",
        "audit_event_records",
        ["episode_id"],
    )
    op.create_index(
        "ix_audit_event_records_event_type",
        "audit_event_records",
        ["event_type"],
    )


def downgrade() -> None:
    op.drop_index("ix_audit_event_records_event_type", table_name="audit_event_records")
    op.drop_index("ix_audit_event_records_episode_id", table_name="audit_event_records")
    op.drop_index("ix_audit_event_records_created_at", table_name="audit_event_records")
    op.drop_table("audit_event_records")
