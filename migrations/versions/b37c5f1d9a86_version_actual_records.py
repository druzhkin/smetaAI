"""version actual records

Revision ID: b37c5f1d9a86
Revises: a26b4e0c8f75
Create Date: 2026-07-23 21:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b37c5f1d9a86"
down_revision: str | None = "a26b4e0c8f75"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "actual_records",
        sa.Column("actual_key", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "actual_records",
        sa.Column("supersedes_actual_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "actual_records",
        sa.Column(
            "is_current",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.execute("UPDATE actual_records SET actual_key = id")
    with op.batch_alter_table("actual_records") as batch_op:
        batch_op.alter_column(
            "actual_key",
            existing_type=sa.String(length=128),
            nullable=False,
        )
    op.execute(
        """
        UPDATE actual_records
        SET is_current = TRUE
        WHERE id IN (
            SELECT id
            FROM (
                SELECT
                    id,
                    ROW_NUMBER() OVER (
                        PARTITION BY project_id, actual_key
                        ORDER BY created_at DESC, id DESC
                    ) AS revision_rank
                FROM actual_records
            ) ranked
            WHERE revision_rank = 1
        )
        """
    )
    op.create_index(
        op.f("ix_actual_records_is_current"),
        "actual_records",
        ["is_current"],
        unique=False,
    )
    if op.get_bind().dialect.name == "postgresql":
        op.create_index(
            "uq_actual_records_current_per_key",
            "actual_records",
            ["project_id", "actual_key"],
            unique=True,
            postgresql_where=sa.text("is_current"),
        )
    else:
        op.create_index(
            "uq_actual_records_current_per_key",
            "actual_records",
            ["project_id", "actual_key"],
            unique=True,
            sqlite_where=sa.text("is_current = 1"),
        )


def downgrade() -> None:
    op.drop_index(
        "uq_actual_records_current_per_key",
        table_name="actual_records",
    )
    op.drop_index(
        op.f("ix_actual_records_is_current"),
        table_name="actual_records",
    )
    op.drop_column("actual_records", "is_current")
    op.drop_column("actual_records", "supersedes_actual_id")
    op.drop_column("actual_records", "actual_key")
