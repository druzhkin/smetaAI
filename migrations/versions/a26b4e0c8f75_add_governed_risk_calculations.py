"""add governed risk calculations

Revision ID: a26b4e0c8f75
Revises: f15a3d9b7e64
Create Date: 2026-07-23 20:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a26b4e0c8f75"
down_revision: str | None = "f15a3d9b7e64"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "risk_items",
        sa.Column("risk_key", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "risk_items",
        sa.Column("supersedes_risk_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "risk_items",
        sa.Column(
            "is_current",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.execute("UPDATE risk_items SET risk_key = id")
    with op.batch_alter_table("risk_items") as batch_op:
        batch_op.alter_column(
            "risk_key",
            existing_type=sa.String(length=128),
            nullable=False,
        )
    op.execute(
        """
        UPDATE risk_items
        SET is_current = TRUE
        WHERE id IN (
            SELECT id
            FROM (
                SELECT
                    id,
                    ROW_NUMBER() OVER (
                        PARTITION BY project_id, risk_key
                        ORDER BY updated_at DESC, created_at DESC, id DESC
                    ) AS revision_rank
                FROM risk_items
            ) ranked
            WHERE revision_rank = 1
        )
        """
    )
    op.create_index(
        op.f("ix_risk_items_is_current"),
        "risk_items",
        ["is_current"],
        unique=False,
    )
    if op.get_bind().dialect.name == "postgresql":
        op.create_index(
            "uq_risk_items_current_per_key",
            "risk_items",
            ["project_id", "risk_key"],
            unique=True,
            postgresql_where=sa.text("is_current"),
        )
    else:
        op.create_index(
            "uq_risk_items_current_per_key",
            "risk_items",
            ["project_id", "risk_key"],
            unique=True,
            sqlite_where=sa.text("is_current = 1"),
        )
    op.create_table(
        "risk_calculations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("policy_version_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("expected_reserve", sa.Numeric(precision=38, scale=12), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("unit", sa.String(length=64), nullable=False),
        sa.Column("supersedes_calculation_id", sa.String(length=64), nullable=True),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["policy_version_id"],
            ["controlled_versions.id"],
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_risk_calculations_project_id"),
        "risk_calculations",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_risk_calculations_status"),
        "risk_calculations",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_risk_calculations_is_current"),
        "risk_calculations",
        ["is_current"],
        unique=False,
    )
    if op.get_bind().dialect.name == "postgresql":
        op.create_index(
            "uq_risk_calculations_current_per_project",
            "risk_calculations",
            ["project_id"],
            unique=True,
            postgresql_where=sa.text("is_current"),
        )
    else:
        op.create_index(
            "uq_risk_calculations_current_per_project",
            "risk_calculations",
            ["project_id"],
            unique=True,
            sqlite_where=sa.text("is_current = 1"),
        )


def downgrade() -> None:
    op.drop_index(
        "uq_risk_calculations_current_per_project",
        table_name="risk_calculations",
    )
    op.drop_index(
        op.f("ix_risk_calculations_is_current"),
        table_name="risk_calculations",
    )
    op.drop_index(
        op.f("ix_risk_calculations_status"),
        table_name="risk_calculations",
    )
    op.drop_index(
        op.f("ix_risk_calculations_project_id"),
        table_name="risk_calculations",
    )
    op.drop_table("risk_calculations")
    op.drop_index("uq_risk_items_current_per_key", table_name="risk_items")
    op.drop_index(op.f("ix_risk_items_is_current"), table_name="risk_items")
    op.drop_column("risk_items", "is_current")
    op.drop_column("risk_items", "supersedes_risk_id")
    op.drop_column("risk_items", "risk_key")
