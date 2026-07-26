"""protect approval task and decision evidence

Revision ID: fb7d2a9c4e16
Revises: fa2c5d7e9014
Create Date: 2026-07-26
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "fb7d2a9c4e16"
down_revision: str | None = "fa2c5d7e9014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_approval_records_task_id",
        "approval_records",
        ["task_id"],
        unique=True,
    )
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        CREATE TRIGGER trg_approval_records_immutable
        BEFORE UPDATE OR DELETE ON approval_records
        FOR EACH ROW
        EXECUTE FUNCTION tenderguard_reject_immutable_mutation();
        """
    )
    op.execute(
        """
        CREATE FUNCTION tenderguard_protect_approval_task()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'approval task cannot be deleted';
            END IF;
            IF NEW.id IS DISTINCT FROM OLD.id
                OR NEW.project_id IS DISTINCT FROM OLD.project_id
                OR NEW.task_type IS DISTINCT FROM OLD.task_type
                OR NEW.entity_type IS DISTINCT FROM OLD.entity_type
                OR NEW.entity_id IS DISTINCT FROM OLD.entity_id
                OR NEW.assigned_role IS DISTINCT FROM OLD.assigned_role
                OR NEW.required IS DISTINCT FROM OLD.required
                OR NEW.created_at IS DISTINCT FROM OLD.created_at
                OR NEW.updated_at <= OLD.updated_at
            THEN
                RAISE EXCEPTION 'approval task identity or timestamp is invalid';
            END IF;
            IF OLD.status = 'PENDING'
                AND NEW.status IN ('APPROVED', 'REJECTED', 'CHANGES_REQUESTED')
                AND NEW.payload::jsonb = OLD.payload::jsonb
            THEN
                RETURN NEW;
            END IF;
            IF OLD.status <> 'SUPERSEDED'
                AND NEW.status = 'SUPERSEDED'
                AND NEW.payload::jsonb @> OLD.payload::jsonb
                AND jsonb_typeof(NEW.payload::jsonb -> 'invalidated_at') = 'string'
                AND jsonb_typeof(
                    NEW.payload::jsonb -> 'invalidated_by_document_revision_id'
                ) = 'string'
                AND jsonb_typeof(
                    NEW.payload::jsonb -> 'invalidated_document_set_revision_id'
                ) = 'string'
            THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'invalid approval task transition';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_approval_tasks_protected
        BEFORE UPDATE OR DELETE ON approval_tasks
        FOR EACH ROW
        EXECUTE FUNCTION tenderguard_protect_approval_task();
        """
    )
    op.execute(
        """
        CREATE FUNCTION tenderguard_validate_approval_decision()
        RETURNS trigger AS $$
        DECLARE
            decision_task_id varchar;
            decision_value varchar;
            task_status varchar;
            decision_count bigint;
        BEGIN
            IF TG_TABLE_NAME = 'approval_tasks' THEN
                decision_task_id := NEW.id;
                decision_value := NEW.status;
            ELSE
                decision_task_id := NEW.task_id;
                decision_value := NEW.decision;
            END IF;
            IF decision_value NOT IN (
                'APPROVED', 'REJECTED', 'CHANGES_REQUESTED'
            ) THEN
                RAISE EXCEPTION 'invalid approval decision value';
            END IF;
            SELECT status INTO task_status
            FROM approval_tasks
            WHERE id = decision_task_id;
            SELECT count(*) INTO decision_count
            FROM approval_records
            WHERE task_id = decision_task_id
                AND decision = decision_value;
            IF task_status IS DISTINCT FROM decision_value
                OR decision_count <> 1
            THEN
                RAISE EXCEPTION
                    'terminal approval task and immutable decision record must agree';
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_approval_task_decision_complete
        AFTER UPDATE ON approval_tasks
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW
        WHEN (
            NEW.status IN ('APPROVED', 'REJECTED', 'CHANGES_REQUESTED')
        )
        EXECUTE FUNCTION tenderguard_validate_approval_decision();
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_approval_record_decision_complete
        AFTER INSERT ON approval_records
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW
        EXECUTE FUNCTION tenderguard_validate_approval_decision();
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        op.drop_index(
            "uq_approval_records_task_id",
            table_name="approval_records",
        )
        return
    op.execute("DROP TRIGGER IF EXISTS trg_approval_record_decision_complete ON approval_records")
    op.execute("DROP TRIGGER IF EXISTS trg_approval_task_decision_complete ON approval_tasks")
    op.execute("DROP FUNCTION IF EXISTS tenderguard_validate_approval_decision()")
    op.execute("DROP TRIGGER IF EXISTS trg_approval_tasks_protected ON approval_tasks")
    op.execute("DROP FUNCTION IF EXISTS tenderguard_protect_approval_task()")
    op.execute("DROP TRIGGER IF EXISTS trg_approval_records_immutable ON approval_records")
    op.drop_index(
        "uq_approval_records_task_id",
        table_name="approval_records",
    )
