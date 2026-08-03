"""add immutable automatic rework dispatch evidence

Revision ID: d2f6a8c1e405
Revises: c1e4a7f9b263
Create Date: 2026-08-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d2f6a8c1e405"
down_revision: str | None = "c1e4a7f9b263"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "automation_rework_dispatches",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("rework_request_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("source_outbox_event_id", sa.String(length=64), nullable=False),
        sa.Column("command_outbox_event_id", sa.String(length=64), nullable=True),
        sa.Column("target_stage", sa.String(length=64), nullable=False),
        sa.Column("command_topic", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("dispatch_hash", sa.String(length=64), nullable=False),
        sa.Column("worker_qualification_id", sa.String(length=128), nullable=False),
        sa.Column("worker_actor_id", sa.String(length=128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "target_stage IN ("
            "'EXTRACTION_IN_PROGRESS', 'BOQ_IN_PROGRESS', "
            "'PRICING_IN_PROGRESS', 'CALCULATION_IN_PROGRESS', 'BLOCKED'"
            ")",
            name="ck_automation_rework_dispatch_target_stage",
        ),
        sa.CheckConstraint(
            "status IN ('STAGE_COMMAND_QUEUED', 'BLOCKED')",
            name="ck_automation_rework_dispatch_status",
        ),
        sa.CheckConstraint(
            "(status = 'STAGE_COMMAND_QUEUED' AND command_outbox_event_id IS NOT NULL "
            "AND command_topic IS NOT NULL) OR "
            "(status = 'BLOCKED' AND command_outbox_event_id IS NULL "
            "AND command_topic IS NULL)",
            name="ck_automation_rework_dispatch_command",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["rework_request_id"], ["expert_rework_requests.id"]),
        sa.ForeignKeyConstraint(["source_outbox_event_id"], ["outbox_events.id"]),
        sa.ForeignKeyConstraint(["command_outbox_event_id"], ["outbox_events.id"]),
        sa.ForeignKeyConstraint(
            ["worker_qualification_id"],
            ["adapter_qualifications.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "rework_request_id",
            name="uq_automation_rework_dispatch_request",
        ),
        sa.UniqueConstraint(
            "source_outbox_event_id",
            name="uq_automation_rework_dispatch_source_event",
        ),
        sa.UniqueConstraint(
            "command_outbox_event_id",
            name="uq_automation_rework_dispatch_command_event",
        ),
        sa.UniqueConstraint(
            "dispatch_hash",
            name="uq_automation_rework_dispatch_hash",
        ),
    )
    op.create_index(
        "ix_automation_rework_dispatches_rework_request_id",
        "automation_rework_dispatches",
        ["rework_request_id"],
    )
    op.create_index(
        "ix_automation_rework_dispatches_project_id",
        "automation_rework_dispatches",
        ["project_id"],
    )
    op.create_index(
        "ix_automation_rework_dispatches_status",
        "automation_rework_dispatches",
        ["status"],
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            CREATE TRIGGER trg_automation_rework_dispatches_immutable
            BEFORE UPDATE OR DELETE ON automation_rework_dispatches
            FOR EACH ROW EXECUTE FUNCTION tenderguard_reject_immutable_mutation();
            """
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_automation_rework_dispatches_immutable "
            "ON automation_rework_dispatches"
        )
    op.drop_table("automation_rework_dispatches")
