"""add durable document jobs

Revision ID: f42d8a1b6c53
Revises: e31c9f0a7b42
Create Date: 2026-07-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f42d8a1b6c53"
down_revision: str | None = "e31c9f0a7b42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_no_active_processing() -> None:
    active = (
        op.get_bind()
        .execute(sa.text("SELECT COUNT(*) FROM quarantined_uploads WHERE status = 'PROCESSING'"))
        .scalar_one()
    )
    if active:
        raise RuntimeError(
            "Cannot migrate while document uploads are PROCESSING; "
            "stop workers and resolve active jobs first"
        )


def _require_no_durable_job_history() -> None:
    connection = op.get_bind()
    upload_history = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM quarantined_uploads "
            "WHERE processing_attempts <> 0 "
            "OR processing_worker_id IS NOT NULL "
            "OR processing_lease_token IS NOT NULL "
            "OR processing_lease_expires_at IS NOT NULL "
            "OR processing_deadline_at IS NOT NULL "
            "OR processing_started_at IS NOT NULL "
            "OR processing_dead_lettered_at IS NOT NULL"
        )
    ).scalar_one()
    outbox_history = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM outbox_events "
            "WHERE locked_by IS NOT NULL "
            "OR lease_token IS NOT NULL "
            "OR lease_expires_at IS NOT NULL "
            "OR last_attempt_at IS NOT NULL "
            "OR dead_lettered_at IS NOT NULL"
        )
    ).scalar_one()
    if upload_history or outbox_history:
        raise RuntimeError(
            "Cannot downgrade durable document jobs after operational history exists; "
            "preserve and migrate job evidence explicitly"
        )


def _create_active_upload_index() -> None:
    op.create_index(
        "uq_active_quarantined_upload_per_logical",
        "quarantined_uploads",
        ["project_id", "logical_key"],
        unique=True,
        sqlite_where=sa.text(
            "status IN ('QUARANTINED', 'CLEAN', 'SCAN_FAILED', 'PROCESSING', "
            "'PROCESSING_FAILED', 'PROCESSING_DEAD_LETTERED')"
        ),
        postgresql_where=sa.text(
            "status IN ('QUARANTINED', 'CLEAN', 'SCAN_FAILED', 'PROCESSING', "
            "'PROCESSING_FAILED', 'PROCESSING_DEAD_LETTERED')"
        ),
    )


def upgrade() -> None:
    _require_no_active_processing()
    op.drop_index(
        "uq_active_quarantined_upload_per_logical",
        table_name="quarantined_uploads",
    )
    with op.batch_alter_table("quarantined_uploads") as batch_op:
        batch_op.add_column(
            sa.Column(
                "processing_attempts",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.add_column(sa.Column("processing_worker_id", sa.String(length=128)))
        batch_op.add_column(sa.Column("processing_lease_token", sa.String(length=64)))
        batch_op.add_column(sa.Column("processing_lease_expires_at", sa.DateTime(timezone=True)))
        batch_op.add_column(sa.Column("processing_deadline_at", sa.DateTime(timezone=True)))
        batch_op.add_column(sa.Column("processing_started_at", sa.DateTime(timezone=True)))
        batch_op.add_column(sa.Column("processing_dead_lettered_at", sa.DateTime(timezone=True)))
        batch_op.create_check_constraint(
            "ck_quarantined_upload_processing_lease",
            "("
            "status = 'PROCESSING' AND processing_worker_id IS NOT NULL "
            "AND processing_lease_token IS NOT NULL "
            "AND processing_lease_expires_at IS NOT NULL "
            "AND processing_deadline_at IS NOT NULL"
            ") OR ("
            "status <> 'PROCESSING' AND processing_worker_id IS NULL "
            "AND processing_lease_token IS NULL "
            "AND processing_lease_expires_at IS NULL "
            "AND processing_deadline_at IS NULL"
            ")",
        )
    _create_active_upload_index()

    with op.batch_alter_table("outbox_events") as batch_op:
        batch_op.add_column(sa.Column("locked_by", sa.String(length=128)))
        batch_op.add_column(sa.Column("lease_token", sa.String(length=64)))
        batch_op.add_column(sa.Column("lease_expires_at", sa.DateTime(timezone=True)))
        batch_op.add_column(sa.Column("last_attempt_at", sa.DateTime(timezone=True)))
        batch_op.add_column(sa.Column("dead_lettered_at", sa.DateTime(timezone=True)))
        batch_op.create_check_constraint(
            "ck_outbox_lease_complete",
            "("
            "locked_by IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL"
            ") OR ("
            "locked_by IS NOT NULL AND lease_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL"
            ")",
        )
        batch_op.create_check_constraint(
            "ck_outbox_single_terminal_state",
            "published_at IS NULL OR dead_lettered_at IS NULL",
        )
    op.create_index(
        "ix_outbox_delivery_ready",
        "outbox_events",
        [
            "topic",
            "published_at",
            "dead_lettered_at",
            "available_at",
            "lease_expires_at",
        ],
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            CREATE FUNCTION tenderguard_protect_terminal_outbox()
            RETURNS trigger AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'outbox events cannot be deleted';
                END IF;
                IF OLD.published_at IS NOT NULL OR OLD.dead_lettered_at IS NOT NULL THEN
                    RAISE EXCEPTION 'terminal outbox events are immutable';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_outbox_terminal_immutable
            BEFORE UPDATE OR DELETE ON outbox_events
            FOR EACH ROW EXECUTE FUNCTION tenderguard_protect_terminal_outbox();
            """
        )


def downgrade() -> None:
    _require_no_durable_job_history()
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_outbox_terminal_immutable ON outbox_events")
        op.execute("DROP FUNCTION IF EXISTS tenderguard_protect_terminal_outbox()")
    op.drop_index("ix_outbox_delivery_ready", table_name="outbox_events")
    with op.batch_alter_table("outbox_events") as batch_op:
        batch_op.drop_constraint("ck_outbox_single_terminal_state", type_="check")
        batch_op.drop_constraint("ck_outbox_lease_complete", type_="check")
        batch_op.drop_column("dead_lettered_at")
        batch_op.drop_column("last_attempt_at")
        batch_op.drop_column("lease_expires_at")
        batch_op.drop_column("lease_token")
        batch_op.drop_column("locked_by")

    op.drop_index(
        "uq_active_quarantined_upload_per_logical",
        table_name="quarantined_uploads",
    )
    with op.batch_alter_table("quarantined_uploads") as batch_op:
        batch_op.drop_constraint("ck_quarantined_upload_processing_lease", type_="check")
        batch_op.drop_column("processing_dead_lettered_at")
        batch_op.drop_column("processing_started_at")
        batch_op.drop_column("processing_deadline_at")
        batch_op.drop_column("processing_lease_expires_at")
        batch_op.drop_column("processing_lease_token")
        batch_op.drop_column("processing_worker_id")
        batch_op.drop_column("processing_attempts")
    op.create_index(
        "uq_active_quarantined_upload_per_logical",
        "quarantined_uploads",
        ["project_id", "logical_key"],
        unique=True,
        sqlite_where=sa.text(
            "status IN ('QUARANTINED', 'CLEAN', 'SCAN_FAILED', 'PROCESSING', 'PROCESSING_FAILED')"
        ),
        postgresql_where=sa.text(
            "status IN ('QUARANTINED', 'CLEAN', 'SCAN_FAILED', 'PROCESSING', 'PROCESSING_FAILED')"
        ),
    )
