"""add immutable final expert rework requests

Revision ID: c1e4a7f9b263
Revises: a9d2c4e6f813
Create Date: 2026-07-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c1e4a7f9b263"
down_revision: str | None = "a9d2c4e6f813"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "expert_rework_requests",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("snapshot_id", sa.String(length=64), nullable=False),
        sa.Column("requested_state", sa.String(length=64), nullable=False),
        sa.Column("gate_hash", sa.String(length=64), nullable=False),
        sa.Column("target_stage", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("requested_by", sa.String(length=128), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "requested_state IN ('APPROVED_FOR_BID', 'APPROVED_FOR_INTERNAL_USE')",
            name="ck_expert_rework_requested_state",
        ),
        sa.CheckConstraint(
            "target_stage IN ("
            "'EXTRACTION_IN_PROGRESS', 'BOQ_IN_PROGRESS', "
            "'PRICING_IN_PROGRESS', 'CALCULATION_IN_PROGRESS', 'BLOCKED'"
            ")",
            name="ck_expert_rework_target_stage",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["snapshot_id"], ["calculation_snapshots.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_expert_rework_requests_project_id",
        "expert_rework_requests",
        ["project_id"],
    )
    op.create_index(
        "ix_expert_rework_requests_snapshot_id",
        "expert_rework_requests",
        ["snapshot_id"],
    )
    op.create_index(
        "ix_expert_rework_requests_gate_hash",
        "expert_rework_requests",
        ["gate_hash"],
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            CREATE TRIGGER trg_expert_rework_requests_immutable
            BEFORE UPDATE OR DELETE ON expert_rework_requests
            FOR EACH ROW EXECUTE FUNCTION tenderguard_reject_immutable_mutation();
            """
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_expert_rework_requests_immutable ON expert_rework_requests"
        )
    op.drop_table("expert_rework_requests")
