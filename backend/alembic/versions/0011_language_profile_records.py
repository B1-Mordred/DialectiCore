"""create language profile records

Revision ID: 0011_language_profile_records
Revises: 0010_asset_records
Create Date: 2026-07-28 09:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_language_profile_records"
down_revision: str | None = "0010_asset_records"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "language_profile_records",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("bcp47_tag", sa.String(length=12), nullable=False),
        sa.Column("default_mode", sa.String(length=64), nullable=False),
        sa.Column("subtitle_direction", sa.String(length=16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_language_profile_records_bcp47_tag",
        "language_profile_records",
        ["bcp47_tag"],
    )
    op.create_index(
        "ix_language_profile_records_default_mode",
        "language_profile_records",
        ["default_mode"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_language_profile_records_default_mode",
        table_name="language_profile_records",
    )
    op.drop_index(
        "ix_language_profile_records_bcp47_tag",
        table_name="language_profile_records",
    )
    op.drop_table("language_profile_records")
