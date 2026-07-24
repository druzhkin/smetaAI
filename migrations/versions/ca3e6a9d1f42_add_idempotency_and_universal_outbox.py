"""add idempotency and universal outbox

Revision ID: ca3e6a9d1f42
Revises: b92d5f8c0e31
Create Date: 2026-07-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ca3e6a9d1f42"
down_revision: str | None = "b92d5f8c0e31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_no_new_reliability_evidence() -> None:
    connection = op.get_bind()
    idempotency_count = connection.execute(
        sa.text("SELECT COUNT(*) FROM idempotency_records")
    ).scalar_one()
    universal_event_count = connection.execute(
        sa.text("SELECT COUNT(*) FROM outbox_events WHERE topic = 'audit.event.recorded'")
    ).scalar_one()
    if idempotency_count or universal_event_count:
        raise RuntimeError("Cannot downgrade after idempotency or universal outbox evidence exists")


def _install_postgresql_guards() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        CREATE OR REPLACE FUNCTION tenderguard_protect_terminal_outbox()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'outbox events cannot be deleted';
            END IF;
            IF OLD.id IS DISTINCT FROM NEW.id
               OR OLD.deduplication_key IS DISTINCT FROM NEW.deduplication_key
               OR OLD.topic IS DISTINCT FROM NEW.topic
               OR OLD.aggregate_id IS DISTINCT FROM NEW.aggregate_id
               OR OLD.payload::text IS DISTINCT FROM NEW.payload::text
               OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
                RAISE EXCEPTION 'outbox event identity and payload are immutable';
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
    op.execute(
        """
        CREATE FUNCTION tenderguard_protect_idempotency_record()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'idempotency records cannot be deleted';
            END IF;
            IF OLD.status = 'COMPLETED' THEN
                RAISE EXCEPTION 'completed idempotency records are immutable';
            END IF;
            IF OLD.status = 'PENDING'
               AND NEW.status = 'COMPLETED'
               AND OLD.id IS NOT DISTINCT FROM NEW.id
               AND OLD.organization_id IS NOT DISTINCT FROM NEW.organization_id
               AND OLD.actor_id IS NOT DISTINCT FROM NEW.actor_id
               AND OLD.idempotency_key IS NOT DISTINCT FROM NEW.idempotency_key
               AND OLD.request_method IS NOT DISTINCT FROM NEW.request_method
               AND OLD.request_path IS NOT DISTINCT FROM NEW.request_path
               AND OLD.request_hash IS NOT DISTINCT FROM NEW.request_hash
               AND OLD.initial_request_id IS NOT DISTINCT FROM NEW.initial_request_id
               AND OLD.created_at IS NOT DISTINCT FROM NEW.created_at THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'invalid idempotency record transition';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_idempotency_record_protected
        BEFORE UPDATE OR DELETE ON idempotency_records
        FOR EACH ROW EXECUTE FUNCTION tenderguard_protect_idempotency_record();
        """
    )


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_outbox_terminal_immutable ON outbox_events")
    with op.batch_alter_table("outbox_events") as batch_op:
        batch_op.add_column(sa.Column("deduplication_key", sa.String(length=200)))
    op.execute(
        sa.text(
            "UPDATE outbox_events "
            "SET deduplication_key = :prefix || id "
            "WHERE deduplication_key IS NULL"
        ).bindparams(prefix="legacy-outbox:")
    )
    with op.batch_alter_table("outbox_events") as batch_op:
        batch_op.alter_column(
            "deduplication_key",
            existing_type=sa.String(length=200),
            nullable=False,
        )
        batch_op.create_unique_constraint(
            "uq_outbox_deduplication_key",
            ["deduplication_key"],
        )

    op.create_table(
        "idempotency_records",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("organization_id", sa.String(length=64), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_method", sa.String(length=16), nullable=False),
        sa.Column("request_path", sa.String(length=1000), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("initial_request_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("response_status", sa.Integer()),
        sa.Column("response_media_type", sa.String(length=200)),
        sa.Column("response_payload", sa.JSON()),
        sa.Column("response_has_body", sa.Boolean()),
        sa.Column("response_headers", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "organization_id",
            "actor_id",
            "idempotency_key",
            name="uq_idempotency_actor_key",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'COMPLETED')",
            name="ck_idempotency_status",
        ),
        sa.CheckConstraint(
            "("
            "status = 'PENDING' AND response_status IS NULL "
            "AND response_has_body IS NULL AND response_headers IS NULL "
            "AND completed_at IS NULL"
            ") OR ("
            "status = 'COMPLETED' AND response_status IS NOT NULL "
            "AND response_has_body IS NOT NULL AND response_headers IS NOT NULL "
            "AND completed_at IS NOT NULL"
            ")",
            name="ck_idempotency_completion",
        ),
    )
    op.create_index(
        "ix_idempotency_created_at",
        "idempotency_records",
        ["created_at"],
    )
    _install_postgresql_guards()


def downgrade() -> None:
    _require_no_new_reliability_evidence()
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_outbox_terminal_immutable ON outbox_events")
        op.execute("DROP TRIGGER IF EXISTS trg_idempotency_record_protected ON idempotency_records")
        op.execute("DROP FUNCTION IF EXISTS tenderguard_protect_idempotency_record()")
        op.execute(
            """
            CREATE OR REPLACE FUNCTION tenderguard_protect_terminal_outbox()
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
    op.drop_index("ix_idempotency_created_at", table_name="idempotency_records")
    op.drop_table("idempotency_records")
    with op.batch_alter_table("outbox_events") as batch_op:
        batch_op.drop_constraint(
            "uq_outbox_deduplication_key",
            type_="unique",
        )
        batch_op.drop_column("deduplication_key")
