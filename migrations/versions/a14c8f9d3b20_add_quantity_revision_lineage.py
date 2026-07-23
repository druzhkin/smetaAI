"""add quantity revision lineage

Revision ID: a14c8f9d3b20
Revises: 797ef0df3ed1
Create Date: 2026-07-23 15:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a14c8f9d3b20"
down_revision: str | None = "797ef0df3ed1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "quantities",
        sa.Column("supersedes_quantity_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "quantities",
        sa.Column(
            "is_current",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.execute(
        """
        UPDATE quantities
        SET is_current = TRUE
        WHERE id IN (
            SELECT id
            FROM (
                SELECT
                    id,
                    ROW_NUMBER() OVER (
                        PARTITION BY boq_line_id
                        ORDER BY created_at DESC, id DESC
                    ) AS revision_rank
                FROM quantities
            ) ranked
            WHERE revision_rank = 1
        )
        """
    )
    op.create_index(
        op.f("ix_quantities_is_current"),
        "quantities",
        ["is_current"],
        unique=False,
    )
    if op.get_bind().dialect.name == "postgresql":
        op.create_index(
            "uq_quantities_current_per_boq_line",
            "quantities",
            ["boq_line_id"],
            unique=True,
            postgresql_where=sa.text("is_current"),
        )
    else:
        op.create_index(
            "uq_quantities_current_per_boq_line",
            "quantities",
            ["boq_line_id"],
            unique=True,
            sqlite_where=sa.text("is_current = 1"),
        )


def downgrade() -> None:
    op.drop_index("uq_quantities_current_per_boq_line", table_name="quantities")
    op.drop_index(op.f("ix_quantities_is_current"), table_name="quantities")
    op.drop_column("quantities", "is_current")
    op.drop_column("quantities", "supersedes_quantity_id")
