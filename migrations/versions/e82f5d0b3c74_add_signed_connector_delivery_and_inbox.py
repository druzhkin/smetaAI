"""add signed connector delivery and inbox

Revision ID: e82f5d0b3c74
Revises: d71e4c9a2b63
Create Date: 2026-07-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e82f5d0b3c74"
down_revision: str | None = "d71e4c9a2b63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


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
               OR OLD.delivery_deduplication_key
                  IS DISTINCT FROM NEW.delivery_deduplication_key
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
        CREATE FUNCTION tenderguard_reject_integration_evidence_change()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'integration evidence is immutable';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    for table_name, trigger_name in (
        ("connector_delivery_attempts", "trg_connector_delivery_attempt_immutable"),
        ("outbox_replays", "trg_outbox_replay_immutable"),
        ("integration_inbox_messages", "trg_integration_inbox_message_immutable"),
    ):
        op.execute(
            f"""
            CREATE TRIGGER {trigger_name}
            BEFORE UPDATE OR DELETE ON {table_name}
            FOR EACH ROW EXECUTE FUNCTION tenderguard_reject_integration_evidence_change();
            """
        )
    op.execute(
        """
        CREATE FUNCTION tenderguard_protect_inbox_processing()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'integration inbox processing evidence cannot be deleted';
            END IF;
            IF OLD.id IS DISTINCT FROM NEW.id
               OR OLD.message_id IS DISTINCT FROM NEW.message_id
               OR OLD.generation IS DISTINCT FROM NEW.generation
               OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
                RAISE EXCEPTION 'integration inbox processing identity is immutable';
            END IF;
            IF OLD.handler_qualification_id IS NOT NULL
               AND OLD.handler_qualification_id
                   IS DISTINCT FROM NEW.handler_qualification_id THEN
                RAISE EXCEPTION 'integration inbox handler qualification is immutable';
            END IF;
            IF OLD.status IN ('CONSUMED', 'DEAD_LETTERED') THEN
                RAISE EXCEPTION 'terminal integration inbox processing is immutable';
            END IF;
            IF OLD.status = 'PENDING'
               AND NEW.status IN ('PENDING', 'CONSUMED', 'DEAD_LETTERED') THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'invalid integration inbox processing transition';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_integration_inbox_processing_protected
        BEFORE UPDATE OR DELETE ON integration_inbox_processings
        FOR EACH ROW EXECUTE FUNCTION tenderguard_protect_inbox_processing();
        """
    )


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_outbox_terminal_immutable ON outbox_events")
    with op.batch_alter_table("outbox_events") as batch_op:
        batch_op.add_column(sa.Column("delivery_deduplication_key", sa.String(length=200)))
    op.execute(
        "UPDATE outbox_events "
        "SET delivery_deduplication_key = deduplication_key "
        "WHERE delivery_deduplication_key IS NULL"
    )
    with op.batch_alter_table("outbox_events") as batch_op:
        batch_op.alter_column(
            "delivery_deduplication_key",
            existing_type=sa.String(length=200),
            nullable=False,
        )

    op.create_table(
        "connector_delivery_attempts",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "outbox_event_id",
            sa.String(length=64),
            sa.ForeignKey("outbox_events.id"),
            nullable=False,
        ),
        sa.Column(
            "connector_qualification_id",
            sa.String(length=128),
            sa.ForeignKey("adapter_qualifications.id"),
            nullable=False,
        ),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("envelope_hash", sa.String(length=64), nullable=False),
        sa.Column("receipt_hash", sa.String(length=64)),
        sa.Column("external_message_id", sa.String(length=200)),
        sa.Column("error_code", sa.String(length=200)),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "outbox_event_id",
            "attempt_number",
            name="uq_connector_delivery_attempt",
        ),
        sa.CheckConstraint(
            "status IN ('ACCEPTED', 'DUPLICATE', 'RETRYABLE_FAILURE', 'PERMANENT_FAILURE')",
            name="ck_connector_delivery_attempt_status",
        ),
        sa.CheckConstraint(
            "("
            "status IN ('ACCEPTED', 'DUPLICATE') AND receipt_hash IS NOT NULL "
            "AND external_message_id IS NOT NULL AND error_code IS NULL"
            ") OR ("
            "status IN ('RETRYABLE_FAILURE', 'PERMANENT_FAILURE') "
            "AND error_code IS NOT NULL AND receipt_hash IS NULL "
            "AND external_message_id IS NULL"
            ")",
            name="ck_connector_delivery_attempt_result",
        ),
        sa.CheckConstraint(
            "attempt_number >= 1 AND completed_at >= started_at",
            name="ck_connector_delivery_attempt_timing",
        ),
    )
    op.create_index(
        "ix_connector_delivery_attempts_outbox_event_id",
        "connector_delivery_attempts",
        ["outbox_event_id"],
    )
    op.create_index(
        "ix_connector_delivery_attempts_connector_qualification_id",
        "connector_delivery_attempts",
        ["connector_qualification_id"],
    )
    op.create_index(
        "ix_connector_delivery_attempts_status",
        "connector_delivery_attempts",
        ["status"],
    )
    op.create_index(
        "ix_connector_delivery_attempt_completed",
        "connector_delivery_attempts",
        ["connector_qualification_id", "completed_at"],
    )

    op.create_table(
        "outbox_replays",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("organization_id", sa.String(length=64), nullable=False),
        sa.Column(
            "source_outbox_event_id",
            sa.String(length=64),
            sa.ForeignKey("outbox_events.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "replay_outbox_event_id",
            sa.String(length=64),
            sa.ForeignKey("outbox_events.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "delivery_deduplication_key",
            sa.String(length=200),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("replayed_by", sa.String(length=128), nullable=False),
        sa.Column("replayed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_outbox_replays_organization_id",
        "outbox_replays",
        ["organization_id"],
    )

    op.create_table(
        "integration_inbox_messages",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "source_qualification_id",
            sa.String(length=128),
            sa.ForeignKey("adapter_qualifications.id"),
            nullable=False,
        ),
        sa.Column("organization_id", sa.String(length=64), nullable=False),
        sa.Column("source_message_id", sa.String(length=128), nullable=False),
        sa.Column(
            "delivery_deduplication_key",
            sa.String(length=200),
            nullable=False,
        ),
        sa.Column("topic", sa.String(length=200), nullable=False),
        sa.Column("aggregate_id", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.String(length=100), nullable=False),
        sa.Column("core_hash", sa.String(length=64), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("envelope", sa.JSON(), nullable=False),
        sa.Column("receipt", sa.JSON(), nullable=False),
        sa.Column("qualification_snapshot", sa.JSON(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "source_qualification_id",
            "source_message_id",
            name="uq_inbox_source_message",
        ),
        sa.UniqueConstraint(
            "source_qualification_id",
            "delivery_deduplication_key",
            name="uq_inbox_source_deduplication",
        ),
    )
    op.create_index(
        "ix_integration_inbox_messages_source_qualification_id",
        "integration_inbox_messages",
        ["source_qualification_id"],
    )
    op.create_index(
        "ix_integration_inbox_messages_organization_id",
        "integration_inbox_messages",
        ["organization_id"],
    )
    op.create_index(
        "ix_integration_inbox_messages_topic",
        "integration_inbox_messages",
        ["topic"],
    )
    op.create_index(
        "ix_inbox_organization_received",
        "integration_inbox_messages",
        ["organization_id", "received_at"],
    )

    op.create_table(
        "integration_inbox_processings",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "message_id",
            sa.String(length=64),
            sa.ForeignKey("integration_inbox_messages.id"),
            nullable=False,
        ),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_by", sa.String(length=128)),
        sa.Column("lease_token", sa.String(length=64)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.String(length=200)),
        sa.Column(
            "handler_qualification_id",
            sa.String(length=128),
            sa.ForeignKey("adapter_qualifications.id"),
        ),
        sa.Column("result_reference", sa.String(length=500)),
        sa.Column("result_hash", sa.String(length=64)),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("dead_lettered_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "message_id",
            "generation",
            name="uq_inbox_processing_generation",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'CONSUMED', 'DEAD_LETTERED')",
            name="ck_inbox_processing_status",
        ),
        sa.CheckConstraint(
            "generation >= 1 AND ("
            "(attempts = 0 AND last_attempt_at IS NULL "
            "AND handler_qualification_id IS NULL) OR "
            "(attempts >= 1 AND last_attempt_at IS NOT NULL "
            "AND handler_qualification_id IS NOT NULL)"
            ")",
            name="ck_inbox_processing_counters",
        ),
        sa.CheckConstraint(
            "("
            "locked_by IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL"
            ") OR ("
            "locked_by IS NOT NULL AND lease_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL"
            ")",
            name="ck_inbox_processing_lease_complete",
        ),
        sa.CheckConstraint(
            "("
            "status = 'PENDING' AND consumed_at IS NULL AND dead_lettered_at IS NULL "
            "AND result_reference IS NULL AND result_hash IS NULL"
            ") OR ("
            "status = 'CONSUMED' AND consumed_at IS NOT NULL "
            "AND dead_lettered_at IS NULL AND result_reference IS NOT NULL "
            "AND result_hash IS NOT NULL AND handler_qualification_id IS NOT NULL "
            "AND locked_by IS NULL"
            ") OR ("
            "status = 'DEAD_LETTERED' AND dead_lettered_at IS NOT NULL "
            "AND consumed_at IS NULL AND result_reference IS NULL "
            "AND result_hash IS NULL AND locked_by IS NULL"
            ")",
            name="ck_inbox_processing_terminal_state",
        ),
    )
    op.create_index(
        "ix_integration_inbox_processings_message_id",
        "integration_inbox_processings",
        ["message_id"],
    )
    op.create_index(
        "ix_integration_inbox_processings_status",
        "integration_inbox_processings",
        ["status"],
    )
    op.create_index(
        "uq_inbox_processing_pending_message",
        "integration_inbox_processings",
        ["message_id"],
        unique=True,
        sqlite_where=sa.text("status = 'PENDING'"),
        postgresql_where=sa.text("status = 'PENDING'"),
    )
    op.create_index(
        "ix_inbox_processing_ready",
        "integration_inbox_processings",
        ["status", "available_at", "lease_expires_at"],
    )
    _install_postgresql_guards()


def downgrade() -> None:
    connection = op.get_bind()
    for table_name in (
        "connector_delivery_attempts",
        "outbox_replays",
        "integration_inbox_messages",
        "integration_inbox_processings",
    ):
        if connection.execute(sa.text(f"SELECT COUNT(*) FROM {table_name}")).scalar_one():
            raise RuntimeError("Cannot downgrade after integration delivery evidence exists")
    if connection.dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_integration_inbox_processing_protected "
            "ON integration_inbox_processings"
        )
        op.execute("DROP FUNCTION IF EXISTS tenderguard_protect_inbox_processing()")
        for table_name, trigger_name in (
            ("connector_delivery_attempts", "trg_connector_delivery_attempt_immutable"),
            ("outbox_replays", "trg_outbox_replay_immutable"),
            ("integration_inbox_messages", "trg_integration_inbox_message_immutable"),
        ):
            op.execute(f"DROP TRIGGER IF EXISTS {trigger_name} ON {table_name}")
        op.execute("DROP FUNCTION IF EXISTS tenderguard_reject_integration_evidence_change()")
    op.drop_index(
        "ix_inbox_processing_ready",
        table_name="integration_inbox_processings",
    )
    op.drop_index(
        "uq_inbox_processing_pending_message",
        table_name="integration_inbox_processings",
    )
    op.drop_index(
        "ix_integration_inbox_processings_status",
        table_name="integration_inbox_processings",
    )
    op.drop_index(
        "ix_integration_inbox_processings_message_id",
        table_name="integration_inbox_processings",
    )
    op.drop_table("integration_inbox_processings")
    op.drop_index(
        "ix_inbox_organization_received",
        table_name="integration_inbox_messages",
    )
    op.drop_index(
        "ix_integration_inbox_messages_topic",
        table_name="integration_inbox_messages",
    )
    op.drop_index(
        "ix_integration_inbox_messages_organization_id",
        table_name="integration_inbox_messages",
    )
    op.drop_index(
        "ix_integration_inbox_messages_source_qualification_id",
        table_name="integration_inbox_messages",
    )
    op.drop_table("integration_inbox_messages")
    op.drop_index("ix_outbox_replays_organization_id", table_name="outbox_replays")
    op.drop_table("outbox_replays")
    op.drop_index(
        "ix_connector_delivery_attempt_completed",
        table_name="connector_delivery_attempts",
    )
    op.drop_index(
        "ix_connector_delivery_attempts_status",
        table_name="connector_delivery_attempts",
    )
    op.drop_index(
        "ix_connector_delivery_attempts_connector_qualification_id",
        table_name="connector_delivery_attempts",
    )
    op.drop_index(
        "ix_connector_delivery_attempts_outbox_event_id",
        table_name="connector_delivery_attempts",
    )
    op.drop_table("connector_delivery_attempts")
    if connection.dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_outbox_terminal_immutable ON outbox_events")
    with op.batch_alter_table("outbox_events") as batch_op:
        batch_op.drop_column("delivery_deduplication_key")
    if connection.dialect.name == "postgresql":
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
