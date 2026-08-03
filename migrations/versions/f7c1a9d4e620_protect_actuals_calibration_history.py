"""protect actual, variance, and calibration history

Revision ID: f7c1a9d4e620
Revises: e5b8d3f7a642
Create Date: 2026-07-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f7c1a9d4e620"
down_revision: str | None = "e5b8d3f7a642"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_empty_actuals_history(*, operation: str) -> None:
    connection = op.get_bind()
    populated_tables = [
        table_name
        for table_name in (
            "actual_records",
            "variance_records",
            "calibration_examples",
        )
        if connection.execute(sa.text(f"SELECT COUNT(*) FROM {table_name}")).scalar_one()
    ]
    if populated_tables:
        raise RuntimeError(
            f"Cannot {operation} actuals history guards while actuals evidence exists "
            f"in {', '.join(populated_tables)}; preserve and migrate the immutable "
            "review evidence explicitly"
        )


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    _require_empty_actuals_history(operation="install")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION tenderguard_protect_actual_record()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'actual record history cannot be deleted';
            END IF;
            IF NEW.id IS DISTINCT FROM OLD.id
                OR NEW.project_id IS DISTINCT FROM OLD.project_id
                OR NEW.actual_key IS DISTINCT FROM OLD.actual_key
                OR NEW.entity_type IS DISTINCT FROM OLD.entity_type
                OR NEW.entity_id IS DISTINCT FROM OLD.entity_id
                OR NEW.metric IS DISTINCT FROM OLD.metric
                OR NEW.value IS DISTINCT FROM OLD.value
                OR NEW.unit IS DISTINCT FROM OLD.unit
                OR NEW.source_observation_id IS DISTINCT FROM OLD.source_observation_id
                OR NEW.occurred_on IS DISTINCT FROM OLD.occurred_on
                OR NEW.supersedes_actual_id IS DISTINCT FROM OLD.supersedes_actual_id
                OR NEW.created_at IS DISTINCT FROM OLD.created_at
            THEN
                RAISE EXCEPTION 'actual record identity or basis is immutable';
            END IF;
            IF NOT OLD.is_current THEN
                RAISE EXCEPTION 'superseded actual record is immutable';
            END IF;
            IF OLD.is_current AND NOT NEW.is_current
                AND NEW.verified IS NOT DISTINCT FROM OLD.verified
                AND NEW.payload::jsonb = OLD.payload::jsonb
            THEN
                RETURN NEW;
            END IF;
            IF OLD.is_current AND NEW.is_current
                AND OLD.verified = FALSE
                AND OLD.payload::jsonb ->> 'review_status' = 'IN_REVIEW'
                AND NEW.payload::jsonb ->> 'review_decision'
                    IN ('APPROVED', 'REJECTED')
                AND NEW.payload::jsonb ->> 'reviewed_by' IS NOT NULL
                AND NEW.payload::jsonb ->> 'reviewed_at' IS NOT NULL
                AND (
                    NEW.payload::jsonb
                    - ARRAY[
                        'review_decision',
                        'reviewed_by',
                        'reviewed_at',
                        'verified_by',
                        'verified_at'
                    ]
                ) = jsonb_set(
                    OLD.payload::jsonb,
                    '{review_status}',
                    to_jsonb(
                        CASE
                            WHEN NEW.payload::jsonb ->> 'review_decision'
                                = 'APPROVED'
                            THEN 'VERIFIED'
                            ELSE 'REJECTED'
                        END
                    ),
                    false
                )
                AND (
                    (
                        NEW.verified = TRUE
                        AND NEW.payload::jsonb ->> 'review_decision' = 'APPROVED'
                        AND NEW.payload::jsonb ->> 'verified_by'
                            = NEW.payload::jsonb ->> 'reviewed_by'
                        AND NEW.payload::jsonb ->> 'verified_at'
                            = NEW.payload::jsonb ->> 'reviewed_at'
                    )
                    OR (
                        NEW.verified = FALSE
                        AND NEW.payload::jsonb ->> 'review_decision' = 'REJECTED'
                        AND NOT (NEW.payload::jsonb ? 'verified_by')
                        AND NOT (NEW.payload::jsonb ? 'verified_at')
                    )
                )
            THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'invalid actual record transition';
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_actual_records_protect
        BEFORE UPDATE OR DELETE ON actual_records
        FOR EACH ROW EXECUTE FUNCTION tenderguard_protect_actual_record();

        CREATE OR REPLACE FUNCTION tenderguard_protect_variance_record()
        RETURNS trigger AS $$
        DECLARE
            target_status text;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'variance history cannot be deleted';
            END IF;
            IF NEW.id IS DISTINCT FROM OLD.id
                OR NEW.project_id IS DISTINCT FROM OLD.project_id
                OR NEW.actual_record_id IS DISTINCT FROM OLD.actual_record_id
                OR NEW.snapshot_id IS DISTINCT FROM OLD.snapshot_id
                OR NEW.metric IS DISTINCT FROM OLD.metric
                OR NEW.reason IS DISTINCT FROM OLD.reason
                OR NEW.absolute_variance IS DISTINCT FROM OLD.absolute_variance
                OR NEW.relative_variance IS DISTINCT FROM OLD.relative_variance
                OR NEW.classified_by IS DISTINCT FROM OLD.classified_by
                OR NEW.created_at IS DISTINCT FROM OLD.created_at
            THEN
                RAISE EXCEPTION 'variance identity or arithmetic is immutable';
            END IF;
            IF OLD.payload::jsonb -> 'variance' ->> 'status' <> 'IN_REVIEW'
                OR NEW.payload::jsonb ->> 'review_decision'
                    NOT IN ('APPROVED', 'REJECTED')
            THEN
                RAISE EXCEPTION 'variance is not in a legal review transition';
            END IF;
            target_status := CASE
                WHEN NEW.payload::jsonb ->> 'review_decision' = 'APPROVED'
                THEN 'VERIFIED'
                ELSE 'REJECTED'
            END;
            IF (
                    NEW.payload::jsonb
                    - ARRAY[
                        'variance',
                        'review_decision',
                        'reviewed_by',
                        'reviewed_at'
                    ]
                ) <> (OLD.payload::jsonb - 'variance')
                OR NEW.payload::jsonb ->> 'reviewed_by' IS NULL
                OR NEW.payload::jsonb ->> 'reviewed_at' IS NULL
                OR NEW.payload::jsonb -> 'variance'
                    <> jsonb_set(
                        jsonb_set(
                            OLD.payload::jsonb -> 'variance',
                            '{status}',
                            to_jsonb(target_status),
                            false
                        ),
                        '{reviewed_by}',
                        to_jsonb(NEW.payload::jsonb ->> 'reviewed_by'),
                        true
                    )
            THEN
                RAISE EXCEPTION 'invalid variance review mutation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_variance_records_protect
        BEFORE UPDATE OR DELETE ON variance_records
        FOR EACH ROW EXECUTE FUNCTION tenderguard_protect_variance_record();

        CREATE OR REPLACE FUNCTION tenderguard_protect_calibration_example()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'calibration example history cannot be deleted';
            END IF;
            IF NEW.id IS DISTINCT FROM OLD.id
                OR NEW.project_id IS DISTINCT FROM OLD.project_id
                OR NEW.actual_record_id IS DISTINCT FROM OLD.actual_record_id
                OR NEW.variance_record_id IS DISTINCT FROM OLD.variance_record_id
                OR NEW.features_snapshot_id
                    IS DISTINCT FROM OLD.features_snapshot_id
                OR NEW.metric IS DISTINCT FROM OLD.metric
                OR NEW.target_value IS DISTINCT FROM OLD.target_value
                OR NEW.unit IS DISTINCT FROM OLD.unit
                OR NEW.created_at IS DISTINCT FROM OLD.created_at
                OR OLD.payload::jsonb ->> 'review_status' <> 'IN_REVIEW'
                OR NEW.payload::jsonb ->> 'review_decision'
                    NOT IN ('APPROVED', 'REJECTED')
                OR NEW.payload::jsonb ->> 'reviewed_by' IS NULL
                OR NEW.payload::jsonb ->> 'reviewed_at' IS NULL
                OR (
                    NEW.payload::jsonb
                    - ARRAY[
                        'review_decision',
                        'reviewed_by',
                        'reviewed_at',
                        'approved_by',
                        'approved_at',
                        'approval_reason'
                    ]
                ) <> jsonb_set(
                    OLD.payload::jsonb,
                    '{review_status}',
                    to_jsonb(
                        CASE
                            WHEN NEW.payload::jsonb ->> 'review_decision'
                                = 'APPROVED'
                            THEN 'VERIFIED'
                            ELSE 'REJECTED'
                        END
                    ),
                    false
                )
                OR (
                    (
                        NEW.approved = TRUE
                        AND NEW.payload::jsonb ->> 'review_decision' = 'APPROVED'
                        AND NEW.payload::jsonb ->> 'approved_by'
                            = NEW.payload::jsonb ->> 'reviewed_by'
                        AND NEW.payload::jsonb ->> 'approved_at'
                            = NEW.payload::jsonb ->> 'reviewed_at'
                        AND NEW.payload::jsonb ->> 'approval_reason' IS NOT NULL
                    )
                    OR (
                        NEW.approved = FALSE
                        AND NEW.payload::jsonb ->> 'review_decision' = 'REJECTED'
                        AND NOT (NEW.payload::jsonb ? 'approved_by')
                        AND NOT (NEW.payload::jsonb ? 'approved_at')
                        AND NOT (NEW.payload::jsonb ? 'approval_reason')
                    )
                ) IS NOT TRUE
            THEN
                RAISE EXCEPTION 'invalid calibration review mutation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_calibration_examples_protect
        BEFORE UPDATE OR DELETE ON calibration_examples
        FOR EACH ROW EXECUTE FUNCTION tenderguard_protect_calibration_example();
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION tenderguard_validate_actuals_initial_state()
        RETURNS trigger AS $$
        DECLARE
            row_data jsonb;
        BEGIN
            row_data := to_jsonb(NEW);
            IF TG_TABLE_NAME = 'actual_records' THEN
                IF (row_data ->> 'verified')::boolean
                    OR NOT (row_data ->> 'is_current')::boolean
                    OR row_data -> 'payload' ->> 'review_status'
                        IS DISTINCT FROM 'IN_REVIEW'
                    OR jsonb_typeof(
                        row_data -> 'payload' -> 'approval_task_id'
                    ) IS DISTINCT FROM 'string'
                    OR jsonb_typeof(
                        row_data -> 'payload' -> 'created_by'
                    ) IS DISTINCT FROM 'string'
                    OR jsonb_typeof(
                        row_data -> 'payload' -> 'actuals_policy_version_id'
                    ) IS DISTINCT FROM 'string'
                    OR jsonb_typeof(
                        row_data -> 'payload' -> 'actuals_policy_content_hash'
                    ) IS DISTINCT FROM 'string'
                    OR jsonb_typeof(
                        row_data -> 'payload' -> 'review_role'
                    ) IS DISTINCT FROM 'string'
                    OR row_data -> 'payload' ?| ARRAY[
                        'review_decision',
                        'reviewed_by',
                        'reviewed_at',
                        'verified_by',
                        'verified_at'
                    ]
                THEN
                    RAISE EXCEPTION
                        'actual record must enter through a pending governed review';
                END IF;
            ELSIF TG_TABLE_NAME = 'variance_records' THEN
                IF jsonb_typeof(
                        row_data -> 'payload' -> 'variance'
                    ) IS DISTINCT FROM 'object'
                    OR row_data -> 'payload' -> 'variance' ->> 'status'
                        IS DISTINCT FROM 'IN_REVIEW'
                    OR COALESCE(
                        row_data -> 'payload' -> 'variance' -> 'reviewed_by',
                        'null'::jsonb
                    ) IS DISTINCT FROM 'null'::jsonb
                    OR jsonb_typeof(
                        row_data -> 'payload' -> 'approval_task_id'
                    ) IS DISTINCT FROM 'string'
                    OR jsonb_typeof(
                        row_data -> 'payload' -> 'actuals_policy_version_id'
                    ) IS DISTINCT FROM 'string'
                    OR jsonb_typeof(
                        row_data -> 'payload' -> 'actuals_policy_content_hash'
                    ) IS DISTINCT FROM 'string'
                    OR jsonb_typeof(
                        row_data -> 'payload' -> 'review_role'
                    ) IS DISTINCT FROM 'string'
                    OR jsonb_typeof(
                        row_data -> 'payload' -> 'released_by_decision_id'
                    ) IS DISTINCT FROM 'string'
                    OR row_data -> 'payload' ?| ARRAY[
                        'review_decision',
                        'reviewed_by',
                        'reviewed_at'
                    ]
                THEN
                    RAISE EXCEPTION
                        'variance must enter through a pending governed review';
                END IF;
            ELSIF TG_TABLE_NAME = 'calibration_examples' THEN
                IF (row_data ->> 'approved')::boolean
                    OR row_data -> 'payload' ->> 'review_status'
                        IS DISTINCT FROM 'IN_REVIEW'
                    OR jsonb_typeof(
                        row_data -> 'payload' -> 'approval_task_id'
                    ) IS DISTINCT FROM 'string'
                    OR jsonb_typeof(
                        row_data -> 'payload' -> 'created_by'
                    ) IS DISTINCT FROM 'string'
                    OR jsonb_typeof(
                        row_data -> 'payload' -> 'actuals_policy_version_id'
                    ) IS DISTINCT FROM 'string'
                    OR jsonb_typeof(
                        row_data -> 'payload' -> 'actuals_policy_content_hash'
                    ) IS DISTINCT FROM 'string'
                    OR jsonb_typeof(
                        row_data -> 'payload' -> 'approval_role'
                    ) IS DISTINCT FROM 'string'
                    OR jsonb_typeof(
                        row_data -> 'payload' -> 'released_by_decision_id'
                    ) IS DISTINCT FROM 'string'
                    OR row_data -> 'payload' ?| ARRAY[
                        'review_decision',
                        'reviewed_by',
                        'reviewed_at',
                        'approved_by',
                        'approved_at',
                        'approval_reason'
                    ]
                THEN
                    RAISE EXCEPTION
                        'calibration example must enter through a pending governed review';
                END IF;
            ELSE
                RAISE EXCEPTION 'unsupported actuals review table';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_actual_records_initial_state
        BEFORE INSERT ON actual_records
        FOR EACH ROW EXECUTE FUNCTION tenderguard_validate_actuals_initial_state();

        CREATE TRIGGER trg_variance_records_initial_state
        BEFORE INSERT ON variance_records
        FOR EACH ROW EXECUTE FUNCTION tenderguard_validate_actuals_initial_state();

        CREATE TRIGGER trg_calibration_examples_initial_state
        BEFORE INSERT ON calibration_examples
        FOR EACH ROW EXECUTE FUNCTION tenderguard_validate_actuals_initial_state();

        CREATE OR REPLACE FUNCTION tenderguard_validate_actuals_task_initial_state()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.task_type NOT IN (
                'ACTUAL_FACT_REVIEW',
                'VARIANCE_CLASSIFICATION_REVIEW',
                'CALIBRATION_EXAMPLE_REVIEW'
            ) THEN
                RETURN NEW;
            END IF;
            IF NEW.status IS DISTINCT FROM 'PENDING'
                OR NEW.required IS NOT TRUE
                OR jsonb_typeof(
                    NEW.payload::jsonb -> 'created_by'
                ) IS DISTINCT FROM 'string'
            THEN
                RAISE EXCEPTION
                    'actuals review task must enter as a pending governed task';
            END IF;
            IF NEW.task_type = 'ACTUAL_FACT_REVIEW' AND (
                    NEW.entity_type IS DISTINCT FROM 'actual_record'
                    OR NEW.entity_id IS DISTINCT FROM
                        NEW.payload::jsonb ->> 'actual_id'
                    OR NEW.assigned_role IS DISTINCT FROM
                        NEW.payload::jsonb ->> 'review_role'
                    OR jsonb_typeof(
                        NEW.payload::jsonb -> 'actual_submission_hash'
                    ) IS DISTINCT FROM 'string'
                )
            THEN
                RAISE EXCEPTION
                    'actuals review task must enter as a pending governed task';
            ELSIF NEW.task_type = 'VARIANCE_CLASSIFICATION_REVIEW' AND (
                    NEW.entity_type IS DISTINCT FROM 'variance_record'
                    OR NEW.entity_id IS DISTINCT FROM
                        NEW.payload::jsonb ->> 'variance_record_id'
                    OR NEW.assigned_role IS DISTINCT FROM
                        NEW.payload::jsonb ->> 'review_role'
                    OR jsonb_typeof(
                        NEW.payload::jsonb -> 'variance_submission_hash'
                    ) IS DISTINCT FROM 'string'
                )
            THEN
                RAISE EXCEPTION
                    'actuals review task must enter as a pending governed task';
            ELSIF NEW.task_type = 'CALIBRATION_EXAMPLE_REVIEW' AND (
                    NEW.entity_type IS DISTINCT FROM 'calibration_example'
                    OR NEW.entity_id IS DISTINCT FROM
                        NEW.payload::jsonb ->> 'calibration_example_id'
                    OR NEW.assigned_role IS DISTINCT FROM
                        NEW.payload::jsonb ->> 'approval_role'
                    OR jsonb_typeof(
                        NEW.payload::jsonb -> 'calibration_submission_hash'
                    ) IS DISTINCT FROM 'string'
                )
            THEN
                RAISE EXCEPTION
                    'actuals review task must enter as a pending governed task';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_actuals_tasks_initial_state
        BEFORE INSERT ON approval_tasks
        FOR EACH ROW
        EXECUTE FUNCTION tenderguard_validate_actuals_task_initial_state();

        CREATE OR REPLACE FUNCTION tenderguard_validate_actuals_task_link()
        RETURNS trigger AS $$
        DECLARE
            task_id_value text;
            task_row record;
            row_data jsonb;
            row_payload jsonb;
            row_status text;
            approval_row record;
            approval_count bigint;
            expected_status text;
        BEGIN
            IF TG_TABLE_NAME = 'approval_tasks' THEN
                task_id_value := NEW.id;
            ELSE
                task_id_value := NEW.task_id;
            END IF;
            SELECT *
                INTO task_row
            FROM approval_tasks
            WHERE id = task_id_value;
            IF NOT FOUND OR task_row.task_type NOT IN (
                'ACTUAL_FACT_REVIEW',
                'VARIANCE_CLASSIFICATION_REVIEW',
                'CALIBRATION_EXAMPLE_REVIEW'
            ) THEN
                RETURN NULL;
            END IF;

            IF task_row.task_type = 'ACTUAL_FACT_REVIEW' THEN
                SELECT to_jsonb(actual_records.*)
                    INTO row_data
                FROM actual_records
                WHERE id = task_row.entity_id
                    AND payload::jsonb ->> 'approval_task_id'
                        = task_id_value;
            ELSIF task_row.task_type
                    = 'VARIANCE_CLASSIFICATION_REVIEW'
            THEN
                SELECT to_jsonb(variance_records.*)
                    INTO row_data
                FROM variance_records
                WHERE id = task_row.entity_id
                    AND payload::jsonb ->> 'approval_task_id'
                        = task_id_value;
            ELSE
                SELECT to_jsonb(calibration_examples.*)
                    INTO row_data
                FROM calibration_examples
                WHERE id = task_row.entity_id
                    AND payload::jsonb ->> 'approval_task_id'
                        = task_id_value;
            END IF;
            IF row_data IS NULL THEN
                RAISE EXCEPTION
                    'actuals review task has no matching reviewed entity';
            END IF;

            row_payload := row_data -> 'payload';
            IF task_row.task_type = 'VARIANCE_CLASSIFICATION_REVIEW' THEN
                row_status := row_payload -> 'variance' ->> 'status';
            ELSE
                row_status := row_payload ->> 'review_status';
            END IF;
            SELECT count(*)
                INTO approval_count
            FROM approval_records
            WHERE task_id = task_id_value;
            IF approval_count = 1 THEN
                SELECT *
                    INTO approval_row
                FROM approval_records
                WHERE task_id = task_id_value;
            END IF;

            IF task_row.status = 'PENDING' THEN
                IF row_status IS DISTINCT FROM 'IN_REVIEW'
                    OR approval_count <> 0
                THEN
                    RAISE EXCEPTION
                        'actuals task and reviewed entity state disagree';
                END IF;
                RETURN NULL;
            END IF;
            IF task_row.status IN ('APPROVED', 'REJECTED') THEN
                expected_status := CASE
                    WHEN task_row.status = 'APPROVED'
                        THEN 'VERIFIED'
                    ELSE 'REJECTED'
                END;
                IF approval_count <> 1
                    OR approval_row.decision
                        IS DISTINCT FROM task_row.status
                    OR row_status IS DISTINCT FROM expected_status
                    OR row_payload ->> 'review_decision'
                        IS DISTINCT FROM task_row.status
                    OR row_payload ->> 'reviewed_by'
                        IS DISTINCT FROM approval_row.decided_by
                THEN
                    RAISE EXCEPTION
                        'actuals task and reviewed entity state disagree';
                END IF;
                RETURN NULL;
            END IF;
            IF task_row.task_type = 'ACTUAL_FACT_REVIEW'
                AND task_row.status = 'SUPERSEDED'
                AND NOT (row_data ->> 'is_current')::boolean
                AND (
                    (row_status = 'IN_REVIEW' AND approval_count = 0)
                    OR (
                        row_status IN ('VERIFIED', 'REJECTED')
                        AND approval_count = 1
                    )
                )
            THEN
                RETURN NULL;
            END IF;
            RAISE EXCEPTION
                'actuals task and reviewed entity state disagree';
        END;
        $$ LANGUAGE plpgsql;

        CREATE CONSTRAINT TRIGGER trg_actuals_task_entity_link
        AFTER INSERT OR UPDATE ON approval_tasks
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION tenderguard_validate_actuals_task_link();

        CREATE CONSTRAINT TRIGGER trg_actuals_record_entity_link
        AFTER INSERT ON approval_records
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION tenderguard_validate_actuals_task_link();

        CREATE OR REPLACE FUNCTION tenderguard_validate_actuals_review_link()
        RETURNS trigger AS $$
        DECLARE
            row_data jsonb;
            row_payload jsonb;
            project_value text;
            entity_value text;
            created_by_value text;
            role_value text;
            row_status text;
            task_id_value text;
            task_row record;
            approval_row record;
            approval_count bigint;
            expected_decision text;
            submission_hash_key text;
            policy_count bigint;
            release_count bigint;
            audit_count bigint;
            audit_event_type text;
            audit_entity_key text;
            creation_audit_event_type text;
            creation_audit_entity_key text;
        BEGIN
            IF TG_TABLE_NAME = 'actual_records' THEN
                SELECT to_jsonb(actual_records.*)
                    INTO row_data
                FROM actual_records
                WHERE id = NEW.id;
            ELSIF TG_TABLE_NAME = 'variance_records' THEN
                SELECT to_jsonb(variance_records.*)
                    INTO row_data
                FROM variance_records
                WHERE id = NEW.id;
            ELSIF TG_TABLE_NAME = 'calibration_examples' THEN
                SELECT to_jsonb(calibration_examples.*)
                    INTO row_data
                FROM calibration_examples
                WHERE id = NEW.id;
            ELSE
                RAISE EXCEPTION 'unsupported actuals review table';
            END IF;
            IF row_data IS NULL THEN
                RETURN NULL;
            END IF;

            row_payload := row_data -> 'payload';
            project_value := row_data ->> 'project_id';
            entity_value := row_data ->> 'id';
            task_id_value := row_payload ->> 'approval_task_id';
            IF TG_TABLE_NAME = 'actual_records' THEN
                created_by_value := row_payload ->> 'created_by';
                role_value := row_payload ->> 'review_role';
                row_status := row_payload ->> 'review_status';
                submission_hash_key := 'actual_submission_hash';
                audit_event_type := 'actual_fact_verified';
                audit_entity_key := 'actual_id';
                creation_audit_event_type := 'actual_fact_recorded';
                creation_audit_entity_key := 'actual_id';
            ELSIF TG_TABLE_NAME = 'variance_records' THEN
                created_by_value := row_data ->> 'classified_by';
                role_value := row_payload ->> 'review_role';
                row_status := row_payload -> 'variance' ->> 'status';
                submission_hash_key := 'variance_submission_hash';
                audit_event_type := 'variance_review_decided';
                audit_entity_key := 'variance_record_id';
                creation_audit_event_type :=
                    'forecast_actual_variance_classified';
                creation_audit_entity_key := 'variance_record_id';
            ELSE
                created_by_value := row_payload ->> 'created_by';
                role_value := row_payload ->> 'approval_role';
                row_status := row_payload ->> 'review_status';
                submission_hash_key := 'calibration_submission_hash';
                audit_event_type := 'calibration_example_review_decided';
                audit_entity_key := 'calibration_example_id';
                creation_audit_event_type := 'calibration_example_created';
                creation_audit_entity_key := 'calibration_example_id';
            END IF;

            SELECT count(*)
                INTO policy_count
            FROM controlled_versions
            WHERE id = row_payload ->> 'actuals_policy_version_id'
                AND kind = 'actuals_policy'
                AND content_hash
                    = row_payload ->> 'actuals_policy_content_hash'
                AND status = 'APPROVED'
                AND approved_by IS NOT NULL
                AND approved_at IS NOT NULL;
            IF policy_count <> 1 THEN
                RAISE EXCEPTION 'actuals policy linkage is invalid';
            END IF;

            SELECT *
                INTO task_row
            FROM approval_tasks
            WHERE id = task_id_value;
            IF NOT FOUND
                OR task_row.project_id IS DISTINCT FROM project_value
                OR task_row.entity_id IS DISTINCT FROM entity_value
                OR task_row.assigned_role IS DISTINCT FROM role_value
                OR task_row.required IS NOT TRUE
                OR task_row.payload::jsonb ->> 'created_by'
                    IS DISTINCT FROM created_by_value
                OR jsonb_typeof(
                    task_row.payload::jsonb -> submission_hash_key
                ) IS DISTINCT FROM 'string'
            THEN
                RAISE EXCEPTION 'actuals review task linkage is invalid';
            END IF;

            IF TG_TABLE_NAME = 'actual_records' THEN
                IF task_row.task_type IS DISTINCT FROM 'ACTUAL_FACT_REVIEW'
                    OR task_row.entity_type IS DISTINCT FROM 'actual_record'
                    OR task_row.payload::jsonb ->> 'actual_id'
                        IS DISTINCT FROM entity_value
                    OR task_row.payload::jsonb ->> 'actual_key'
                        IS DISTINCT FROM row_data ->> 'actual_key'
                    OR task_row.payload::jsonb ->> 'source_observation_id'
                        IS DISTINCT FROM row_data ->> 'source_observation_id'
                    OR task_row.payload::jsonb -> 'source_leaf_ids'
                        IS DISTINCT FROM row_payload -> 'source_leaf_ids'
                    OR task_row.payload::jsonb -> 'project_outcome_evidence_ids'
                        IS DISTINCT FROM
                            row_payload -> 'project_outcome_evidence_ids'
                    OR task_row.payload::jsonb ->> 'actuals_policy_version_id'
                        IS DISTINCT FROM
                            row_payload ->> 'actuals_policy_version_id'
                    OR task_row.payload::jsonb ->> 'actuals_policy_content_hash'
                        IS DISTINCT FROM
                            row_payload ->> 'actuals_policy_content_hash'
                    OR task_row.payload::jsonb ->> 'review_role'
                        IS DISTINCT FROM role_value
                THEN
                    RAISE EXCEPTION 'actual review task scope is invalid';
                END IF;
            ELSIF TG_TABLE_NAME = 'variance_records' THEN
                IF task_row.task_type
                        IS DISTINCT FROM 'VARIANCE_CLASSIFICATION_REVIEW'
                    OR task_row.entity_type IS DISTINCT FROM 'variance_record'
                    OR task_row.payload::jsonb ->> 'variance_record_id'
                        IS DISTINCT FROM entity_value
                    OR task_row.payload::jsonb ->> 'actual_record_id'
                        IS DISTINCT FROM row_data ->> 'actual_record_id'
                    OR task_row.payload::jsonb ->> 'actuals_policy_version_id'
                        IS DISTINCT FROM
                            row_payload ->> 'actuals_policy_version_id'
                    OR task_row.payload::jsonb ->> 'actuals_policy_content_hash'
                        IS DISTINCT FROM
                            row_payload ->> 'actuals_policy_content_hash'
                    OR task_row.payload::jsonb ->> 'review_role'
                        IS DISTINCT FROM role_value
                    OR task_row.payload::jsonb
                        ->> 'released_by_decision_id'
                        IS DISTINCT FROM
                            row_payload ->> 'released_by_decision_id'
                THEN
                    RAISE EXCEPTION 'variance review task scope is invalid';
                END IF;
                SELECT count(*)
                    INTO release_count
                FROM release_decisions
                WHERE id = row_payload ->> 'released_by_decision_id'
                    AND project_id = project_value
                    AND snapshot_id = row_data ->> 'snapshot_id'
                    AND allowed IS TRUE
                    AND requested_state = 'APPROVED_FOR_BID'
                    AND resulting_state = 'APPROVED_FOR_BID';
                IF release_count <> 1 THEN
                    RAISE EXCEPTION
                        'variance released forecast linkage is invalid';
                END IF;
            ELSE
                IF task_row.task_type
                        IS DISTINCT FROM 'CALIBRATION_EXAMPLE_REVIEW'
                    OR task_row.entity_type IS DISTINCT FROM
                        'calibration_example'
                    OR task_row.payload::jsonb ->> 'calibration_example_id'
                        IS DISTINCT FROM entity_value
                    OR task_row.payload::jsonb ->> 'actual_record_id'
                        IS DISTINCT FROM row_data ->> 'actual_record_id'
                    OR task_row.payload::jsonb ->> 'variance_record_id'
                        IS DISTINCT FROM row_data ->> 'variance_record_id'
                    OR task_row.payload::jsonb ->> 'actuals_policy_version_id'
                        IS DISTINCT FROM
                            row_payload ->> 'actuals_policy_version_id'
                    OR task_row.payload::jsonb ->> 'actuals_policy_content_hash'
                        IS DISTINCT FROM
                            row_payload ->> 'actuals_policy_content_hash'
                    OR task_row.payload::jsonb ->> 'approval_role'
                        IS DISTINCT FROM role_value
                    OR task_row.payload::jsonb
                        ->> 'released_by_decision_id'
                        IS DISTINCT FROM
                            row_payload ->> 'released_by_decision_id'
                THEN
                    RAISE EXCEPTION 'calibration review task scope is invalid';
                END IF;
                SELECT count(*)
                    INTO release_count
                FROM release_decisions
                JOIN variance_records
                    ON variance_records.id = row_data ->> 'variance_record_id'
                WHERE release_decisions.id
                        = row_payload ->> 'released_by_decision_id'
                    AND release_decisions.project_id = project_value
                    AND release_decisions.snapshot_id
                        = row_data ->> 'features_snapshot_id'
                    AND release_decisions.allowed IS TRUE
                    AND release_decisions.requested_state = 'APPROVED_FOR_BID'
                    AND release_decisions.resulting_state = 'APPROVED_FOR_BID'
                    AND variance_records.project_id = project_value
                    AND variance_records.snapshot_id
                        = release_decisions.snapshot_id
                    AND variance_records.payload::jsonb
                        ->> 'released_by_decision_id'
                        = release_decisions.id;
                IF release_count <> 1 THEN
                    RAISE EXCEPTION
                        'calibration released forecast linkage is invalid';
                END IF;
            END IF;

            SELECT count(*)
                INTO audit_count
            FROM audit_events
            WHERE aggregate_type = 'project'
                AND aggregate_id = project_value
                AND event_type = creation_audit_event_type
                AND actor_id = created_by_value
                AND payload::jsonb ->> creation_audit_entity_key
                    = entity_value
                AND payload::jsonb ->> 'approval_task_id'
                    = task_id_value
                AND payload::jsonb ->> 'actuals_policy_version_id'
                    = row_payload ->> 'actuals_policy_version_id'
                AND payload::jsonb ->> 'actuals_policy_content_hash'
                    = row_payload ->> 'actuals_policy_content_hash'
                AND payload::jsonb ->> submission_hash_key
                    = task_row.payload::jsonb ->> submission_hash_key
                AND (
                    TG_TABLE_NAME NOT IN (
                        'variance_records',
                        'calibration_examples'
                    )
                    OR payload::jsonb ->> 'released_by_decision_id'
                        = row_payload ->> 'released_by_decision_id'
                );
            IF audit_count <> 1 THEN
                RAISE EXCEPTION 'actuals creation audit linkage is invalid';
            END IF;

            SELECT count(*)
                INTO approval_count
            FROM approval_records
            WHERE task_id = task_id_value;
            IF approval_count = 1 THEN
                SELECT *
                    INTO approval_row
                FROM approval_records
                WHERE task_id = task_id_value;
            END IF;

            IF task_row.status = 'PENDING' THEN
                IF row_status IS DISTINCT FROM 'IN_REVIEW'
                    OR approval_count <> 0
                    OR row_payload ?| ARRAY[
                        'review_decision',
                        'reviewed_by',
                        'reviewed_at'
                    ]
                    OR (
                        TG_TABLE_NAME = 'actual_records'
                        AND (
                            (row_data ->> 'verified')::boolean
                            OR NOT (row_data ->> 'is_current')::boolean
                        )
                    )
                    OR (
                        TG_TABLE_NAME = 'calibration_examples'
                        AND (row_data ->> 'approved')::boolean
                    )
                THEN
                    RAISE EXCEPTION 'pending actuals review linkage is invalid';
                END IF;
                RETURN NULL;
            END IF;

            IF task_row.status IN ('APPROVED', 'REJECTED') THEN
                expected_decision := task_row.status;
                IF approval_count <> 1
                    OR row_payload ->> 'review_decision'
                        IS DISTINCT FROM expected_decision
                    OR row_status IS DISTINCT FROM (
                        CASE
                            WHEN expected_decision = 'APPROVED'
                                THEN 'VERIFIED'
                            ELSE 'REJECTED'
                        END
                    )
                    OR approval_row.decision
                        IS DISTINCT FROM expected_decision
                    OR approval_row.decided_by
                        IS DISTINCT FROM row_payload ->> 'reviewed_by'
                    OR NOT (
                        row_payload ->> 'reviewed_by'
                            IS DISTINCT FROM created_by_value
                    )
                THEN
                    RAISE EXCEPTION 'terminal actuals review linkage is invalid';
                END IF;
            ELSIF TG_TABLE_NAME = 'actual_records'
                AND task_row.status = 'SUPERSEDED'
            THEN
                IF (row_data ->> 'is_current')::boolean
                    OR jsonb_typeof(
                        task_row.payload::jsonb -> 'superseded_at'
                    ) IS DISTINCT FROM 'string'
                    OR jsonb_typeof(
                        task_row.payload::jsonb -> 'superseded_by_entity_id'
                    ) IS DISTINCT FROM 'string'
                    OR jsonb_typeof(
                        task_row.payload::jsonb -> 'supersession_reason'
                    ) IS DISTINCT FROM 'string'
                    OR (
                        row_status = 'IN_REVIEW'
                        AND approval_count <> 0
                    )
                    OR (
                        row_status IN ('VERIFIED', 'REJECTED')
                        AND approval_count <> 1
                    )
                    OR row_status NOT IN (
                        'IN_REVIEW',
                        'VERIFIED',
                        'REJECTED'
                    )
                THEN
                    RAISE EXCEPTION 'superseded actual review linkage is invalid';
                END IF;
                RETURN NULL;
            ELSE
                RAISE EXCEPTION 'actuals review task status is invalid';
            END IF;

            IF TG_TABLE_NAME = 'actual_records' THEN
                IF NOT (row_data ->> 'is_current')::boolean
                    OR (
                        expected_decision = 'APPROVED'
                        AND (
                            NOT (row_data ->> 'verified')::boolean
                            OR row_payload ->> 'verified_by'
                                IS DISTINCT FROM approval_row.decided_by
                        )
                    )
                    OR (
                        expected_decision = 'REJECTED'
                        AND (row_data ->> 'verified')::boolean
                    )
                    OR approval_row.payload::jsonb ->> 'actual_id'
                        IS DISTINCT FROM entity_value
                    OR approval_row.payload::jsonb ->> 'actual_key'
                        IS DISTINCT FROM row_data ->> 'actual_key'
                    OR approval_row.payload::jsonb ->> 'source_observation_id'
                        IS DISTINCT FROM row_data ->> 'source_observation_id'
                    OR approval_row.payload::jsonb -> 'source_leaf_ids'
                        IS DISTINCT FROM row_payload -> 'source_leaf_ids'
                    OR approval_row.payload::jsonb
                        -> 'project_outcome_evidence_ids'
                        IS DISTINCT FROM
                            row_payload -> 'project_outcome_evidence_ids'
                    OR approval_row.payload::jsonb
                        ->> 'actuals_policy_version_id'
                        IS DISTINCT FROM
                            row_payload ->> 'actuals_policy_version_id'
                    OR approval_row.payload::jsonb
                        ->> 'actuals_policy_content_hash'
                        IS DISTINCT FROM
                            row_payload ->> 'actuals_policy_content_hash'
                    OR approval_row.payload::jsonb
                        ->> 'actual_submission_hash'
                        IS DISTINCT FROM task_row.payload::jsonb
                            ->> 'actual_submission_hash'
                THEN
                    RAISE EXCEPTION 'actual approval evidence linkage is invalid';
                END IF;
            ELSIF TG_TABLE_NAME = 'variance_records' THEN
                IF approval_row.payload::jsonb ->> 'variance_record_id'
                        IS DISTINCT FROM entity_value
                    OR approval_row.payload::jsonb ->> 'actual_record_id'
                        IS DISTINCT FROM row_data ->> 'actual_record_id'
                    OR approval_row.payload::jsonb
                        ->> 'actuals_policy_version_id'
                        IS DISTINCT FROM
                            row_payload ->> 'actuals_policy_version_id'
                    OR approval_row.payload::jsonb
                        ->> 'actuals_policy_content_hash'
                        IS DISTINCT FROM
                            row_payload ->> 'actuals_policy_content_hash'
                    OR approval_row.payload::jsonb
                        ->> 'variance_submission_hash'
                        IS DISTINCT FROM task_row.payload::jsonb
                            ->> 'variance_submission_hash'
                    OR approval_row.payload::jsonb
                        ->> 'released_by_decision_id'
                        IS DISTINCT FROM
                            row_payload ->> 'released_by_decision_id'
                THEN
                    RAISE EXCEPTION 'variance approval evidence linkage is invalid';
                END IF;
            ELSE
                IF (
                        expected_decision = 'APPROVED'
                        AND (
                            NOT (row_data ->> 'approved')::boolean
                            OR row_payload ->> 'approved_by'
                                IS DISTINCT FROM approval_row.decided_by
                        )
                    )
                    OR (
                        expected_decision = 'REJECTED'
                        AND (row_data ->> 'approved')::boolean
                    )
                    OR approval_row.payload::jsonb
                        ->> 'calibration_example_id'
                        IS DISTINCT FROM entity_value
                    OR approval_row.payload::jsonb ->> 'actual_record_id'
                        IS DISTINCT FROM row_data ->> 'actual_record_id'
                    OR approval_row.payload::jsonb ->> 'variance_record_id'
                        IS DISTINCT FROM row_data ->> 'variance_record_id'
                    OR approval_row.payload::jsonb
                        ->> 'actuals_policy_version_id'
                        IS DISTINCT FROM
                            row_payload ->> 'actuals_policy_version_id'
                    OR approval_row.payload::jsonb
                        ->> 'actuals_policy_content_hash'
                        IS DISTINCT FROM
                            row_payload ->> 'actuals_policy_content_hash'
                    OR approval_row.payload::jsonb
                        ->> 'calibration_submission_hash'
                        IS DISTINCT FROM task_row.payload::jsonb
                            ->> 'calibration_submission_hash'
                    OR approval_row.payload::jsonb
                        ->> 'released_by_decision_id'
                        IS DISTINCT FROM
                            row_payload ->> 'released_by_decision_id'
                THEN
                    RAISE EXCEPTION
                        'calibration approval evidence linkage is invalid';
                END IF;
            END IF;
            SELECT count(*)
                INTO audit_count
            FROM audit_events
            WHERE aggregate_type = 'project'
                AND aggregate_id = project_value
                AND event_type = audit_event_type
                AND actor_id = approval_row.decided_by
                AND payload::jsonb ->> audit_entity_key = entity_value
                AND payload::jsonb ->> 'approval_id' = approval_row.id
                AND payload::jsonb ->> 'approval_task_id'
                    = task_id_value
                AND payload::jsonb ->> 'decision' = expected_decision
                AND payload::jsonb ->> 'actuals_policy_version_id'
                    = row_payload ->> 'actuals_policy_version_id'
                AND payload::jsonb ->> 'actuals_policy_content_hash'
                    = row_payload ->> 'actuals_policy_content_hash'
                AND (
                    TG_TABLE_NAME NOT IN (
                        'variance_records',
                        'calibration_examples'
                    )
                    OR payload::jsonb ->> 'released_by_decision_id'
                        = row_payload ->> 'released_by_decision_id'
                );
            IF audit_count <> 1 THEN
                RAISE EXCEPTION 'actuals approval audit linkage is invalid';
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE CONSTRAINT TRIGGER trg_actual_records_review_link
        AFTER INSERT OR UPDATE ON actual_records
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION tenderguard_validate_actuals_review_link();

        CREATE CONSTRAINT TRIGGER trg_variance_records_review_link
        AFTER INSERT OR UPDATE ON variance_records
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION tenderguard_validate_actuals_review_link();

        CREATE CONSTRAINT TRIGGER trg_calibration_examples_review_link
        AFTER INSERT OR UPDATE ON calibration_examples
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION tenderguard_validate_actuals_review_link();
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    _require_empty_actuals_history(operation="remove")
    op.execute("DROP TRIGGER IF EXISTS trg_actuals_record_entity_link ON approval_records")
    op.execute("DROP TRIGGER IF EXISTS trg_actuals_task_entity_link ON approval_tasks")
    op.execute("DROP FUNCTION IF EXISTS tenderguard_validate_actuals_task_link()")
    op.execute("DROP TRIGGER IF EXISTS trg_actuals_tasks_initial_state ON approval_tasks")
    op.execute("DROP FUNCTION IF EXISTS tenderguard_validate_actuals_task_initial_state()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_calibration_examples_review_link ON calibration_examples"
    )
    op.execute("DROP TRIGGER IF EXISTS trg_variance_records_review_link ON variance_records")
    op.execute("DROP TRIGGER IF EXISTS trg_actual_records_review_link ON actual_records")
    op.execute("DROP FUNCTION IF EXISTS tenderguard_validate_actuals_review_link()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_calibration_examples_initial_state ON calibration_examples"
    )
    op.execute("DROP TRIGGER IF EXISTS trg_variance_records_initial_state ON variance_records")
    op.execute("DROP TRIGGER IF EXISTS trg_actual_records_initial_state ON actual_records")
    op.execute("DROP FUNCTION IF EXISTS tenderguard_validate_actuals_initial_state()")
    op.execute("DROP TRIGGER IF EXISTS trg_calibration_examples_protect ON calibration_examples")
    op.execute("DROP FUNCTION IF EXISTS tenderguard_protect_calibration_example()")
    op.execute("DROP TRIGGER IF EXISTS trg_variance_records_protect ON variance_records")
    op.execute("DROP FUNCTION IF EXISTS tenderguard_protect_variance_record()")
    op.execute("DROP TRIGGER IF EXISTS trg_actual_records_protect ON actual_records")
    op.execute("DROP FUNCTION IF EXISTS tenderguard_protect_actual_record()")
