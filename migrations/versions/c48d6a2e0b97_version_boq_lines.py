"""version boq lines

Revision ID: c48d6a2e0b97
Revises: b37c5f1d9a86
Create Date: 2026-07-23 22:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c48d6a2e0b97"
down_revision: str | None = "b37c5f1d9a86"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "boq_lines",
        sa.Column("line_key", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "boq_lines",
        sa.Column("supersedes_line_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "boq_lines",
        sa.Column(
            "is_current",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.execute("UPDATE boq_lines SET line_key = id")
    with op.batch_alter_table("boq_lines") as batch_op:
        batch_op.alter_column(
            "line_key",
            existing_type=sa.String(length=128),
            nullable=False,
        )
    op.create_index(
        op.f("ix_boq_lines_is_current"),
        "boq_lines",
        ["is_current"],
        unique=False,
    )
    if op.get_bind().dialect.name == "postgresql":
        op.create_index(
            "uq_boq_lines_current_per_key",
            "boq_lines",
            ["project_id", "line_key"],
            unique=True,
            postgresql_where=sa.text("is_current"),
        )
    else:
        op.create_index(
            "uq_boq_lines_current_per_key",
            "boq_lines",
            ["project_id", "line_key"],
            unique=True,
            sqlite_where=sa.text("is_current = 1"),
        )


def downgrade() -> None:
    op.drop_index("uq_boq_lines_current_per_key", table_name="boq_lines")
    op.drop_index(op.f("ix_boq_lines_is_current"), table_name="boq_lines")
    op.drop_column("boq_lines", "is_current")
    op.drop_column("boq_lines", "supersedes_line_id")
    op.drop_column("boq_lines", "line_key")
