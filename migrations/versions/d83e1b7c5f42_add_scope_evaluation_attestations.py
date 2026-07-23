"""add scope evaluation attestations

Revision ID: d83e1b7c5f42
Revises: c42d7e8f1a36
Create Date: 2026-07-23 17:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d83e1b7c5f42"
down_revision: str | None = "c42d7e8f1a36"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scope_evaluations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("wbs_node_id", sa.String(length=128), nullable=False),
        sa.Column("rule_pack_version_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("input_signature", sa.String(length=64), nullable=False),
        sa.Column("supersedes_evaluation_id", sa.String(length=64), nullable=True),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(
            ["rule_pack_version_id"],
            ["controlled_versions.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_scope_evaluations_project_id"),
        "scope_evaluations",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_scope_evaluations_status"),
        "scope_evaluations",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_scope_evaluations_is_current"),
        "scope_evaluations",
        ["is_current"],
        unique=False,
    )
    if op.get_bind().dialect.name == "postgresql":
        op.create_index(
            "uq_scope_evaluations_current_per_wbs",
            "scope_evaluations",
            ["project_id", "wbs_node_id"],
            unique=True,
            postgresql_where=sa.text("is_current"),
        )
    else:
        op.create_index(
            "uq_scope_evaluations_current_per_wbs",
            "scope_evaluations",
            ["project_id", "wbs_node_id"],
            unique=True,
            sqlite_where=sa.text("is_current = 1"),
        )


def downgrade() -> None:
    op.drop_index(
        "uq_scope_evaluations_current_per_wbs",
        table_name="scope_evaluations",
    )
    op.drop_index(
        op.f("ix_scope_evaluations_is_current"),
        table_name="scope_evaluations",
    )
    op.drop_index(
        op.f("ix_scope_evaluations_status"),
        table_name="scope_evaluations",
    )
    op.drop_index(
        op.f("ix_scope_evaluations_project_id"),
        table_name="scope_evaluations",
    )
    op.drop_table("scope_evaluations")
