"""create project records

Revision ID: 0008_project_records
Revises: 0007_publisher_targets
Create Date: 2026-07-27 02:45:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_project_records"
down_revision: str | None = "0007_publisher_targets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_records",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("description", sa.String(length=2048), nullable=False),
        sa.Column("default_language", sa.String(length=12), nullable=False),
        sa.Column("default_show_format_id", sa.String(length=128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_project_records_name", "project_records", ["name"])
    op.create_index("ix_project_records_updated_at", "project_records", ["updated_at"])


def downgrade() -> None:
    op.drop_index("ix_project_records_updated_at", table_name="project_records")
    op.drop_index("ix_project_records_name", table_name="project_records")
    op.drop_table("project_records")
