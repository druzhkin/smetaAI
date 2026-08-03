from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from tenderguard.config import get_settings
from tenderguard.domain.audit import AuditEvent, append_event
from tenderguard.domain.common import canonical_data


def _require_postgresql() -> str:
    database_url = get_settings().database_url
    if not database_url.startswith("postgresql"):
        pytest.skip("Actuals history guards require PostgreSQL")
    return database_url


def _assert_rejected(
    database_url: str,
    statement: str,
    parameters: dict[str, object],
    expected_message: str,
) -> None:
    engine = create_engine(database_url)
    with pytest.raises(DBAPIError) as exc_info, engine.begin() as connection:
        connection.execute(text(statement), parameters)
    engine.dispose()
    assert expected_message in str(exc_info.value)


def _insert_review_task(
    connection: Connection,
    *,
    task_id: str,
    project_id: str,
    task_type: str,
    entity_type: str,
    entity_id: str,
    assigned_role: str,
    payload: dict[str, object],
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO approval_tasks (
                id, project_id, task_type, entity_type, entity_id,
                assigned_role, status, required, payload, created_at, updated_at
            ) VALUES (
                :task_id, :project_id, :task_type, :entity_type, :entity_id,
                :assigned_role, 'PENDING', true, CAST(:payload AS json),
                now() - interval '1 second', now() - interval '1 second'
            )
            """
        ),
        {
            "task_id": task_id,
            "project_id": project_id,
            "task_type": task_type,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "assigned_role": assigned_role,
            "payload": json.dumps(payload),
        },
    )


def _approve_review_task(
    connection: Connection,
    *,
    task_id: str,
    approval_id: str,
    reviewer: str,
    reason: str,
    payload: dict[str, object],
) -> None:
    connection.execute(
        text(
            """
            UPDATE approval_tasks
            SET status='APPROVED', updated_at=now()
            WHERE id=:task_id
            """
        ),
        {"task_id": task_id},
    )
    connection.execute(
        text(
            """
            INSERT INTO approval_records (
                id, task_id, decision, decided_by, reason, payload, decided_at
            ) VALUES (
                :approval_id, :task_id, 'APPROVED', :reviewer, :reason,
                CAST(:payload AS json), now()
            )
            """
        ),
        {
            "approval_id": approval_id,
            "task_id": task_id,
            "reviewer": reviewer,
            "reason": reason,
            "payload": json.dumps(payload),
        },
    )


def _insert_audit_event(connection: Connection, event: AuditEvent) -> None:
    connection.execute(
        text(
            """
            INSERT INTO audit_events (
                id, aggregate_type, aggregate_id, sequence, event_type,
                actor_id, actor_roles, request_id, reason, payload,
                previous_hash, signing_key_id, signature_version, event_hash,
                signature, occurred_at
            ) VALUES (
                :id, :aggregate_type, :aggregate_id, :sequence, :event_type,
                :actor_id, CAST(:actor_roles AS json), :request_id, :reason,
                CAST(:payload AS json), :previous_hash, :signing_key_id,
                :signature_version, :event_hash, :signature, :occurred_at
            )
            """
        ),
        {
            "id": event.event_id,
            "aggregate_type": event.aggregate_type,
            "aggregate_id": event.aggregate_id,
            "sequence": event.sequence,
            "event_type": event.event_type,
            "actor_id": event.actor_id,
            "actor_roles": json.dumps(event.actor_roles),
            "request_id": event.request_id,
            "reason": event.reason,
            "payload": json.dumps(canonical_data(event.payload)),
            "previous_hash": event.previous_hash,
            "signing_key_id": event.signing_key_id,
            "signature_version": event.signature_version,
            "event_hash": event.event_hash,
            "signature": event.signature,
            "occurred_at": event.occurred_at,
        },
    )


def test_postgresql_actuals_history_allows_only_formal_review_transitions() -> None:
    database_url = _require_postgresql()
    suffix = uuid4().hex
    project_id = f"project-actuals-guard-{suffix}"
    document_id = f"document-actuals-guard-{suffix}"
    revision_id = f"revision-actuals-guard-{suffix}"
    observation_id = f"observation-actuals-guard-{suffix}"
    actual_id = f"actual-guard-{suffix}"
    run_id = f"run-actuals-guard-{suffix}"
    snapshot_id = f"snapshot-actuals-guard-{suffix}"
    variance_id = f"variance-guard-{suffix}"
    release_decision_id = f"release-guard-{suffix}"
    calibration_id = f"calibration-guard-{suffix}"
    actual_task_id = f"task-actual-guard-{suffix}"
    variance_task_id = f"task-variance-guard-{suffix}"
    calibration_task_id = f"task-calibration-guard-{suffix}"
    actual_approval_id = f"approval-actual-guard-{suffix}"
    variance_approval_id = f"approval-variance-guard-{suffix}"
    calibration_approval_id = f"approval-calibration-guard-{suffix}"
    actuals_policy_version_id = f"actuals-policy-{suffix}"
    actuals_policy_content_hash = "9" * 64
    actual_submission_hash = "1" * 64
    variance_submission_hash = "2" * 64
    calibration_submission_hash = "3" * 64
    source_leaf_ids = [observation_id]
    outcome_evidence_ids = [f"outcome-evidence-{suffix}"]
    reviewed_at = "2026-07-27T10:00:00+00:00"
    audit_occurred_at = datetime(2026, 7, 27, 10, tzinfo=UTC)
    audit_signing_key = b"actuals-guard-local-verification-key-at-least-32-bytes"
    audit_signing_key_id = "actuals-guard-test-key"

    actual_evidence_value: dict[str, object] = {
        "actual_key": "supplier-invoice-total",
        "entity_type": "PROJECT",
        "entity_id": project_id,
        "metric": "project_cost_total",
        "value": "125.00",
        "unit": "RUB",
        "source_class": "SUPPLIER_INVOICE",
        "occurred_on": "2026-07-20",
    }
    actual_draft: dict[str, object] = {
        "evidence_value": actual_evidence_value,
        "source_leaf_ids": source_leaf_ids,
        "project_outcome_evidence_ids": outcome_evidence_ids,
        "review_status": "IN_REVIEW",
        "created_by": "actual-author",
        "actuals_policy_version_id": actuals_policy_version_id,
        "actuals_policy_content_hash": actuals_policy_content_hash,
        "review_role": "AUDITOR",
        "approval_task_id": actual_task_id,
    }
    actual_reviewed = {
        **actual_draft,
        "review_status": "VERIFIED",
        "review_decision": "APPROVED",
        "reviewed_by": "actual-reviewer",
        "reviewed_at": reviewed_at,
        "verified_by": "actual-reviewer",
        "verified_at": reviewed_at,
    }
    variance_value: dict[str, object] = {
        "variance_id": variance_id,
        "actual_record_id": actual_id,
        "snapshot_id": snapshot_id,
        "metric": "project_cost_total",
        "forecast_value": "100.00",
        "actual_value": "125.00",
        "unit": "RUB",
        "absolute_variance": "25.00",
        "relative_variance": "0.250000",
        "reason": "PRICE_VARIANCE",
        "reason_detail": "Supplier invoice exceeded the released forecast.",
        "classified_by": "variance-classifier",
        "status": "IN_REVIEW",
        "reviewed_by": None,
    }
    variance_draft = {
        "variance": variance_value,
        "forecast_content_hash": "a" * 64,
        "released_by_decision_id": release_decision_id,
        "actuals_policy_version_id": actuals_policy_version_id,
        "actuals_policy_content_hash": actuals_policy_content_hash,
        "review_role": "REVIEWER",
        "approval_task_id": variance_task_id,
    }
    variance_reviewed = {
        **variance_draft,
        "variance": {
            **variance_value,
            "status": "VERIFIED",
            "reviewed_by": "variance-reviewer",
        },
        "review_decision": "APPROVED",
        "reviewed_by": "variance-reviewer",
        "reviewed_at": reviewed_at,
    }
    calibration_draft = {
        "review_status": "IN_REVIEW",
        "variance_content_hash": "b" * 64,
        "actual_content_hash": "c" * 64,
        "created_by": "calibration-author",
        "released_by_decision_id": release_decision_id,
        "actuals_policy_version_id": actuals_policy_version_id,
        "actuals_policy_content_hash": actuals_policy_content_hash,
        "approval_role": "METHODOLOGY_OWNER",
        "approval_task_id": calibration_task_id,
    }
    calibration_reviewed = {
        **calibration_draft,
        "review_status": "VERIFIED",
        "review_decision": "APPROVED",
        "reviewed_by": "methodology-owner",
        "reviewed_at": reviewed_at,
        "approved_by": "methodology-owner",
        "approved_at": reviewed_at,
        "approval_reason": "Verified fact and independently classified variance.",
    }

    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO projects (
                    id, organization_id, code, name, state, blocked_resume_state,
                    current_document_set_revision_id, row_version,
                    created_at, updated_at
                ) VALUES (
                    :project_id, 'ci-org', :code, 'Actuals guard CI', 'DRAFT',
                    NULL, NULL, 1, now(), now()
                )
                """
            ),
            {"project_id": project_id, "code": f"ACTUALS-GUARD-{suffix}"},
        )
        connection.execute(
            text(
                """
                INSERT INTO controlled_versions (
                    id, kind, version_label, content_hash, status, payload,
                    approved_by, approved_at
                ) VALUES (
                    :version_id, 'actuals_policy', :version_label,
                    :content_hash, 'DRAFT', CAST('{}' AS json), NULL, NULL
                )
                """
            ),
            {
                "version_id": actuals_policy_version_id,
                "version_label": f"actuals-guard-{suffix}",
                "content_hash": actuals_policy_content_hash,
            },
        )
        connection.execute(
            text(
                """
                UPDATE controlled_versions
                SET status='APPROVED', approved_by='policy-owner',
                    approved_at=now()
                WHERE id=:version_id
                """
            ),
            {"version_id": actuals_policy_version_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO documents (
                    id, project_id, logical_key, title, document_type,
                    critical, cancelled, created_at, updated_at
                ) VALUES (
                    :document_id, :project_id, 'supplier-invoice',
                    'Supplier invoice', 'FINANCIAL', true, false, now(), now()
                )
                """
            ),
            {"document_id": document_id, "project_id": project_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO document_revisions (
                    id, document_id, revision_label, issue_date, object_hash,
                    object_key, original_filename, media_type, size_bytes,
                    supersedes_revision_id, is_current, corrupt, protected,
                    inspection_payload, created_at, updated_at
                ) VALUES (
                    :revision_id, :document_id, 'R1', DATE '2026-07-20',
                    :object_hash, 'evidence/invoice.pdf', 'invoice.pdf',
                    'application/pdf', 128, NULL, true, false, false,
                    CAST('{}' AS json), now(), now()
                )
                """
            ),
            {
                "revision_id": revision_id,
                "document_id": document_id,
                "object_hash": "d" * 64,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO evidence_observations (
                    id, project_id, document_revision_id, field_name, method,
                    method_version, status, payload, created_at
                ) VALUES (
                    :observation_id, :project_id, :revision_id,
                    'actual.project_cost_total', 'financial-import',
                    '1.0.0', 'VERIFIED', CAST(:payload AS json), now()
                )
                """
            ),
            {
                "observation_id": observation_id,
                "project_id": project_id,
                "revision_id": revision_id,
                "payload": json.dumps(actual_evidence_value),
            },
        )
        _insert_review_task(
            connection,
            task_id=actual_task_id,
            project_id=project_id,
            task_type="ACTUAL_FACT_REVIEW",
            entity_type="actual_record",
            entity_id=actual_id,
            assigned_role="AUDITOR",
            payload={
                "created_by": "actual-author",
                "actual_id": actual_id,
                "actual_key": "supplier-invoice-total",
                "actual_submission_hash": actual_submission_hash,
                "source_observation_id": observation_id,
                "source_leaf_ids": source_leaf_ids,
                "project_outcome_evidence_ids": outcome_evidence_ids,
                "actuals_policy_version_id": actuals_policy_version_id,
                "actuals_policy_content_hash": actuals_policy_content_hash,
                "review_role": "AUDITOR",
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO actual_records (
                    id, project_id, actual_key, entity_type, entity_id, metric,
                    value, unit, verified, source_observation_id, occurred_on,
                    payload, supersedes_actual_id, is_current, created_at
                ) VALUES (
                    :actual_id, :project_id, 'supplier-invoice-total', 'PROJECT',
                    :project_id, 'project_cost_total', 125.00, 'RUB', false,
                    :observation_id, DATE '2026-07-20', CAST(:payload AS json),
                    NULL, true, now()
                )
                """
            ),
            {
                "actual_id": actual_id,
                "project_id": project_id,
                "observation_id": observation_id,
                "payload": json.dumps(actual_draft),
            },
        )
        actual_recorded_event = append_event(
            previous=None,
            event_id=f"audit-actual-recorded-{suffix}",
            aggregate_type="project",
            aggregate_id=project_id,
            event_type="actual_fact_recorded",
            actor_id="actual-author",
            actor_roles=("PROCUREMENT",),
            request_id=f"request-actual-recorded-{suffix}",
            reason="Recorded controlled supplier invoice evidence.",
            occurred_at=audit_occurred_at,
            payload={
                "actual_id": actual_id,
                "actuals_policy_version_id": actuals_policy_version_id,
                "actuals_policy_content_hash": actuals_policy_content_hash,
                "actual_submission_hash": actual_submission_hash,
                "approval_task_id": actual_task_id,
            },
            signing_key=audit_signing_key,
            signing_key_id=audit_signing_key_id,
        )
        _insert_audit_event(connection, actual_recorded_event)
        connection.execute(
            text(
                """
                UPDATE actual_records
                SET verified=true, payload=CAST(:payload AS json)
                WHERE id=:actual_id
                """
            ),
            {
                "actual_id": actual_id,
                "payload": json.dumps(actual_reviewed),
            },
        )
        _approve_review_task(
            connection,
            task_id=actual_task_id,
            approval_id=actual_approval_id,
            reviewer="actual-reviewer",
            reason="Verified immutable supplier invoice evidence.",
            payload={
                "project_id": project_id,
                "actual_id": actual_id,
                "actual_key": "supplier-invoice-total",
                "source_observation_id": observation_id,
                "source_leaf_ids": source_leaf_ids,
                "project_outcome_evidence_ids": outcome_evidence_ids,
                "actuals_policy_version_id": actuals_policy_version_id,
                "actuals_policy_content_hash": actuals_policy_content_hash,
                "actual_submission_hash": actual_submission_hash,
            },
        )
        actual_audit_event = append_event(
            previous=actual_recorded_event,
            event_id=f"audit-actual-guard-{suffix}",
            aggregate_type="project",
            aggregate_id=project_id,
            event_type="actual_fact_verified",
            actor_id="actual-reviewer",
            actor_roles=("AUDITOR",),
            request_id=f"request-actual-guard-{suffix}",
            reason="Verified immutable supplier invoice evidence.",
            occurred_at=audit_occurred_at,
            payload={
                "approval_id": actual_approval_id,
                "approval_task_id": actual_task_id,
                "decision": "APPROVED",
                "actual_id": actual_id,
                "actuals_policy_version_id": actuals_policy_version_id,
                "actuals_policy_content_hash": actuals_policy_content_hash,
            },
            signing_key=audit_signing_key,
            signing_key_id=audit_signing_key_id,
        )
        _insert_audit_event(connection, actual_audit_event)
        connection.execute(
            text(
                """
                INSERT INTO calculation_runs (
                    id, project_id, engine_version, status, currency,
                    grand_total, payload, created_at
                ) VALUES (
                    :run_id, :project_id, 'guard-1.0.0', 'CALCULATED', 'RUB',
                    100.00, CAST('{}' AS json), now()
                )
                """
            ),
            {"run_id": run_id, "project_id": project_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO calculation_snapshots (
                    id, project_id, calculation_run_id, document_set_revision_id,
                    input_hash, output_hash, snapshot_hash, fixed, object_key,
                    created_by, created_at
                ) VALUES (
                    :snapshot_id, :project_id, :run_id, 'document-set-guard',
                    :input_hash, :output_hash, :snapshot_hash, true,
                    'snapshots/guard.json', 'calculation-author', now()
                )
                """
            ),
            {
                "snapshot_id": snapshot_id,
                "project_id": project_id,
                "run_id": run_id,
                "input_hash": "e" * 64,
                "output_hash": "f" * 64,
                "snapshot_hash": suffix.ljust(64, "0")[:64],
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO release_decisions (
                    id, project_id, snapshot_id, requested_state,
                    resulting_state, allowed, payload, decided_by, decided_at
                ) VALUES (
                    :release_id, :project_id, :snapshot_id,
                    'APPROVED_FOR_BID', 'APPROVED_FOR_BID', true,
                    CAST(:payload AS json), 'bid-approver', now()
                )
                """
            ),
            {
                "release_id": release_decision_id,
                "project_id": project_id,
                "snapshot_id": snapshot_id,
                "payload": json.dumps({"allowed": True}),
            },
        )
        _insert_review_task(
            connection,
            task_id=variance_task_id,
            project_id=project_id,
            task_type="VARIANCE_CLASSIFICATION_REVIEW",
            entity_type="variance_record",
            entity_id=variance_id,
            assigned_role="REVIEWER",
            payload={
                "created_by": "variance-classifier",
                "variance_record_id": variance_id,
                "actual_record_id": actual_id,
                "variance_submission_hash": variance_submission_hash,
                "released_by_decision_id": release_decision_id,
                "actuals_policy_version_id": actuals_policy_version_id,
                "actuals_policy_content_hash": actuals_policy_content_hash,
                "review_role": "REVIEWER",
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO variance_records (
                    id, project_id, actual_record_id, snapshot_id, metric,
                    reason, absolute_variance, relative_variance, payload,
                    classified_by, created_at
                ) VALUES (
                    :variance_id, :project_id, :actual_id, :snapshot_id,
                    'project_cost_total', 'PRICE_VARIANCE', 25.00, 0.250000,
                    CAST(:payload AS json), 'variance-classifier', now()
                )
                """
            ),
            {
                "variance_id": variance_id,
                "project_id": project_id,
                "actual_id": actual_id,
                "snapshot_id": snapshot_id,
                "payload": json.dumps(variance_draft),
            },
        )
        variance_classified_event = append_event(
            previous=actual_audit_event,
            event_id=f"audit-variance-classified-{suffix}",
            aggregate_type="project",
            aggregate_id=project_id,
            event_type="forecast_actual_variance_classified",
            actor_id="variance-classifier",
            actor_roles=("REVIEWER",),
            request_id=f"request-variance-classified-{suffix}",
            reason="Classified the released forecast variance.",
            occurred_at=audit_occurred_at,
            payload={
                "variance_record_id": variance_id,
                "actuals_policy_version_id": actuals_policy_version_id,
                "actuals_policy_content_hash": actuals_policy_content_hash,
                "variance_submission_hash": variance_submission_hash,
                "released_by_decision_id": release_decision_id,
                "approval_task_id": variance_task_id,
            },
            signing_key=audit_signing_key,
            signing_key_id=audit_signing_key_id,
        )
        _insert_audit_event(connection, variance_classified_event)
        connection.execute(
            text(
                """
                UPDATE variance_records
                SET payload=CAST(:payload AS json)
                WHERE id=:variance_id
                """
            ),
            {
                "variance_id": variance_id,
                "payload": json.dumps(variance_reviewed),
            },
        )
        _approve_review_task(
            connection,
            task_id=variance_task_id,
            approval_id=variance_approval_id,
            reviewer="variance-reviewer",
            reason="Independently checked the classified variance.",
            payload={
                "project_id": project_id,
                "variance_record_id": variance_id,
                "released_by_decision_id": release_decision_id,
                "actual_record_id": actual_id,
                "forecast_id": f"forecast-{suffix}",
                "variance_submission_hash": variance_submission_hash,
                "actuals_policy_version_id": actuals_policy_version_id,
                "actuals_policy_content_hash": actuals_policy_content_hash,
            },
        )
        variance_audit_event = append_event(
            previous=variance_classified_event,
            event_id=f"audit-variance-guard-{suffix}",
            aggregate_type="project",
            aggregate_id=project_id,
            event_type="variance_review_decided",
            actor_id="variance-reviewer",
            actor_roles=("REVIEWER",),
            request_id=f"request-variance-guard-{suffix}",
            reason="Independently checked the classified variance.",
            occurred_at=audit_occurred_at,
            payload={
                "approval_id": variance_approval_id,
                "approval_task_id": variance_task_id,
                "decision": "APPROVED",
                "variance_record_id": variance_id,
                "released_by_decision_id": release_decision_id,
                "actuals_policy_version_id": actuals_policy_version_id,
                "actuals_policy_content_hash": actuals_policy_content_hash,
            },
            signing_key=audit_signing_key,
            signing_key_id=audit_signing_key_id,
        )
        _insert_audit_event(connection, variance_audit_event)
        _insert_review_task(
            connection,
            task_id=calibration_task_id,
            project_id=project_id,
            task_type="CALIBRATION_EXAMPLE_REVIEW",
            entity_type="calibration_example",
            entity_id=calibration_id,
            assigned_role="METHODOLOGY_OWNER",
            payload={
                "created_by": "calibration-author",
                "calibration_example_id": calibration_id,
                "actual_record_id": actual_id,
                "variance_record_id": variance_id,
                "released_by_decision_id": release_decision_id,
                "calibration_submission_hash": calibration_submission_hash,
                "actuals_policy_version_id": actuals_policy_version_id,
                "actuals_policy_content_hash": actuals_policy_content_hash,
                "approval_role": "METHODOLOGY_OWNER",
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO calibration_examples (
                    id, project_id, actual_record_id, variance_record_id,
                    features_snapshot_id, metric, target_value, unit, approved,
                    payload, created_at
                ) VALUES (
                    :calibration_id, :project_id, :actual_id, :variance_id,
                    :snapshot_id, 'project_cost_total', 125.00, 'RUB', false,
                    CAST(:payload AS json), now()
                )
                """
            ),
            {
                "calibration_id": calibration_id,
                "project_id": project_id,
                "actual_id": actual_id,
                "variance_id": variance_id,
                "snapshot_id": snapshot_id,
                "payload": json.dumps(calibration_draft),
            },
        )
        calibration_created_event = append_event(
            previous=variance_audit_event,
            event_id=f"audit-calibration-created-{suffix}",
            aggregate_type="project",
            aggregate_id=project_id,
            event_type="calibration_example_created",
            actor_id="calibration-author",
            actor_roles=("METHODOLOGY_OWNER",),
            request_id=f"request-calibration-created-{suffix}",
            reason="Created a governed calibration candidate.",
            occurred_at=audit_occurred_at,
            payload={
                "calibration_example_id": calibration_id,
                "released_by_decision_id": release_decision_id,
                "actuals_policy_version_id": actuals_policy_version_id,
                "actuals_policy_content_hash": actuals_policy_content_hash,
                "calibration_submission_hash": calibration_submission_hash,
                "approval_task_id": calibration_task_id,
            },
            signing_key=audit_signing_key,
            signing_key_id=audit_signing_key_id,
        )
        _insert_audit_event(connection, calibration_created_event)
        connection.execute(
            text(
                """
                UPDATE calibration_examples
                SET approved=true, payload=CAST(:payload AS json)
                WHERE id=:calibration_id
                """
            ),
            {
                "calibration_id": calibration_id,
                "payload": json.dumps(calibration_reviewed),
            },
        )
        _approve_review_task(
            connection,
            task_id=calibration_task_id,
            approval_id=calibration_approval_id,
            reviewer="methodology-owner",
            reason="Approved a verified fact and independently classified variance.",
            payload={
                "project_id": project_id,
                "calibration_example_id": calibration_id,
                "actual_record_id": actual_id,
                "variance_record_id": variance_id,
                "released_by_decision_id": release_decision_id,
                "calibration_submission_hash": calibration_submission_hash,
                "actuals_policy_version_id": actuals_policy_version_id,
                "actuals_policy_content_hash": actuals_policy_content_hash,
            },
        )
        calibration_audit_event = append_event(
            previous=calibration_created_event,
            event_id=f"audit-calibration-guard-{suffix}",
            aggregate_type="project",
            aggregate_id=project_id,
            event_type="calibration_example_review_decided",
            actor_id="methodology-owner",
            actor_roles=("METHODOLOGY_OWNER",),
            request_id=f"request-calibration-guard-{suffix}",
            reason="Approved a verified fact and independently classified variance.",
            occurred_at=audit_occurred_at,
            payload={
                "approval_id": calibration_approval_id,
                "approval_task_id": calibration_task_id,
                "decision": "APPROVED",
                "calibration_example_id": calibration_id,
                "released_by_decision_id": release_decision_id,
                "actuals_policy_version_id": actuals_policy_version_id,
                "actuals_policy_content_hash": actuals_policy_content_hash,
            },
            signing_key=audit_signing_key,
            signing_key_id=audit_signing_key_id,
        )
        _insert_audit_event(connection, calibration_audit_event)

    orphan_actual_id = f"actual-orphan-guard-{suffix}"
    orphan_task_id = f"task-actual-orphan-guard-{suffix}"
    orphan_actual_key = "orphan-direct-transition"
    orphan_draft = {
        **actual_draft,
        "evidence_value": {
            **actual_evidence_value,
            "actual_key": orphan_actual_key,
        },
        "approval_task_id": orphan_task_id,
    }
    orphan_reviewed = {
        **orphan_draft,
        "review_status": "VERIFIED",
        "review_decision": "APPROVED",
        "reviewed_by": "actual-reviewer",
        "reviewed_at": reviewed_at,
        "verified_by": "actual-reviewer",
        "verified_at": reviewed_at,
    }
    with engine.begin() as connection:
        _insert_review_task(
            connection,
            task_id=orphan_task_id,
            project_id=project_id,
            task_type="ACTUAL_FACT_REVIEW",
            entity_type="actual_record",
            entity_id=orphan_actual_id,
            assigned_role="AUDITOR",
            payload={
                "created_by": "actual-author",
                "actual_id": orphan_actual_id,
                "actual_key": orphan_actual_key,
                "actual_submission_hash": "4" * 64,
                "source_observation_id": observation_id,
                "source_leaf_ids": source_leaf_ids,
                "project_outcome_evidence_ids": outcome_evidence_ids,
                "actuals_policy_version_id": actuals_policy_version_id,
                "actuals_policy_content_hash": actuals_policy_content_hash,
                "review_role": "AUDITOR",
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO actual_records (
                    id, project_id, actual_key, entity_type, entity_id, metric,
                    value, unit, verified, source_observation_id, occurred_on,
                    payload, supersedes_actual_id, is_current, created_at
                ) VALUES (
                    :actual_id, :project_id, :actual_key, 'PROJECT',
                    :project_id, 'project_cost_total', 125.00, 'RUB', false,
                    :observation_id, DATE '2026-07-20', CAST(:payload AS json),
                    NULL, true, now()
                )
                """
            ),
            {
                "actual_id": orphan_actual_id,
                "project_id": project_id,
                "actual_key": orphan_actual_key,
                "observation_id": observation_id,
                "payload": json.dumps(orphan_draft),
            },
        )
        orphan_recorded_event = append_event(
            previous=calibration_audit_event,
            event_id=f"audit-orphan-recorded-{suffix}",
            aggregate_type="project",
            aggregate_id=project_id,
            event_type="actual_fact_recorded",
            actor_id="actual-author",
            actor_roles=("PROCUREMENT",),
            request_id=f"request-orphan-recorded-{suffix}",
            reason="Recorded another pending controlled actual.",
            occurred_at=audit_occurred_at,
            payload={
                "actual_id": orphan_actual_id,
                "actuals_policy_version_id": actuals_policy_version_id,
                "actuals_policy_content_hash": actuals_policy_content_hash,
                "actual_submission_hash": "4" * 64,
                "approval_task_id": orphan_task_id,
            },
            signing_key=audit_signing_key,
            signing_key_id=audit_signing_key_id,
        )
        _insert_audit_event(connection, orphan_recorded_event)

    _assert_rejected(
        database_url,
        """
        UPDATE actual_records
        SET verified=true, payload=CAST(:payload AS json)
        WHERE id=:actual_id
        """,
        {
            "actual_id": orphan_actual_id,
            "payload": json.dumps(orphan_reviewed),
        },
        "pending actuals review linkage is invalid",
    )
    bypass_engine = create_engine(database_url)
    with (
        pytest.raises(DBAPIError) as error_info,
        bypass_engine.begin() as connection,
    ):
        connection.execute(
            text(
                """
                UPDATE approval_tasks
                SET status='APPROVED', updated_at=now() + interval '1 second'
                WHERE id=:task_id
                """
            ),
            {"task_id": orphan_task_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO approval_records (
                    id, task_id, decision, decided_by, reason, payload, decided_at
                ) VALUES (
                    :approval_id, :task_id, 'APPROVED', 'actual-reviewer',
                    'Attempted task-only approval', CAST(:payload AS json), now()
                )
                """
            ),
            {
                "approval_id": f"approval-task-only-{suffix}",
                "task_id": orphan_task_id,
                "payload": json.dumps(
                    {
                        "project_id": project_id,
                        "actual_id": orphan_actual_id,
                        "actual_key": orphan_actual_key,
                        "source_observation_id": observation_id,
                        "source_leaf_ids": source_leaf_ids,
                        "project_outcome_evidence_ids": outcome_evidence_ids,
                        "actuals_policy_version_id": actuals_policy_version_id,
                        "actuals_policy_content_hash": actuals_policy_content_hash,
                        "actual_submission_hash": "4" * 64,
                    }
                ),
            },
        )
    bypass_engine.dispose()
    assert "actuals task and reviewed entity state disagree" in str(error_info.value)
    missing_audit_engine = create_engine(database_url)
    with (
        pytest.raises(DBAPIError) as error_info,
        missing_audit_engine.begin() as connection,
    ):
        connection.execute(
            text(
                """
                UPDATE actual_records
                SET verified=true, payload=CAST(:payload AS json)
                WHERE id=:actual_id
                """
            ),
            {
                "actual_id": orphan_actual_id,
                "payload": json.dumps(orphan_reviewed),
            },
        )
        connection.execute(
            text(
                """
                UPDATE approval_tasks
                SET status='APPROVED', updated_at=now() + interval '1 second'
                WHERE id=:task_id
                """
            ),
            {"task_id": orphan_task_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO approval_records (
                    id, task_id, decision, decided_by, reason, payload, decided_at
                ) VALUES (
                    :approval_id, :task_id, 'APPROVED', 'actual-reviewer',
                    'Attempted unaudited approval', CAST(:payload AS json), now()
                )
                """
            ),
            {
                "approval_id": f"approval-missing-audit-{suffix}",
                "task_id": orphan_task_id,
                "payload": json.dumps(
                    {
                        "project_id": project_id,
                        "actual_id": orphan_actual_id,
                        "actual_key": orphan_actual_key,
                        "source_observation_id": observation_id,
                        "source_leaf_ids": source_leaf_ids,
                        "project_outcome_evidence_ids": outcome_evidence_ids,
                        "actuals_policy_version_id": actuals_policy_version_id,
                        "actuals_policy_content_hash": actuals_policy_content_hash,
                        "actual_submission_hash": "4" * 64,
                    }
                ),
            },
        )
    missing_audit_engine.dispose()
    assert "actuals approval audit linkage is invalid" in str(error_info.value)
    _assert_rejected(
        database_url,
        """
        INSERT INTO actual_records (
            id, project_id, actual_key, entity_type, entity_id, metric,
            value, unit, verified, source_observation_id, occurred_on,
            payload, supersedes_actual_id, is_current, created_at
        ) VALUES (
            :actual_id, :project_id, 'direct-terminal-insert', 'PROJECT',
            :project_id, 'project_cost_total', 125.00, 'RUB', true,
            :observation_id, DATE '2026-07-20', CAST(:payload AS json),
            NULL, true, now()
        )
        """,
        {
            "actual_id": f"actual-terminal-insert-{suffix}",
            "project_id": project_id,
            "observation_id": observation_id,
            "payload": json.dumps(actual_reviewed),
        },
        "actual record must enter through a pending governed review",
    )
    _assert_rejected(
        database_url,
        """
        INSERT INTO approval_tasks (
            id, project_id, task_type, entity_type, entity_id, assigned_role,
            status, required, payload, created_at, updated_at
        ) VALUES (
            :task_id, :project_id, 'ACTUAL_FACT_REVIEW', 'actual_record',
            :actual_id, 'AUDITOR', 'APPROVED', true, CAST(:payload AS json),
            now(), now()
        )
        """,
        {
            "task_id": f"task-terminal-insert-{suffix}",
            "project_id": project_id,
            "actual_id": f"actual-terminal-task-{suffix}",
            "payload": json.dumps(
                {
                    "created_by": "actual-author",
                    "actual_id": f"actual-terminal-task-{suffix}",
                    "review_role": "AUDITOR",
                    "actual_submission_hash": "5" * 64,
                }
            ),
        },
        "actuals review task must enter as a pending governed task",
    )
    unaudited_actual_id = f"actual-unaudited-{suffix}"
    unaudited_task_id = f"task-unaudited-{suffix}"
    unaudited_payload = {
        **actual_draft,
        "evidence_value": {
            **actual_evidence_value,
            "actual_key": "unaudited-pending-actual",
        },
        "approval_task_id": unaudited_task_id,
    }
    unaudited_engine = create_engine(database_url)
    with (
        pytest.raises(DBAPIError) as error_info,
        unaudited_engine.begin() as connection,
    ):
        _insert_review_task(
            connection,
            task_id=unaudited_task_id,
            project_id=project_id,
            task_type="ACTUAL_FACT_REVIEW",
            entity_type="actual_record",
            entity_id=unaudited_actual_id,
            assigned_role="AUDITOR",
            payload={
                "created_by": "actual-author",
                "actual_id": unaudited_actual_id,
                "actual_key": "unaudited-pending-actual",
                "actual_submission_hash": "8" * 64,
                "source_observation_id": observation_id,
                "source_leaf_ids": source_leaf_ids,
                "project_outcome_evidence_ids": outcome_evidence_ids,
                "actuals_policy_version_id": actuals_policy_version_id,
                "actuals_policy_content_hash": actuals_policy_content_hash,
                "review_role": "AUDITOR",
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO actual_records (
                    id, project_id, actual_key, entity_type, entity_id, metric,
                    value, unit, verified, source_observation_id, occurred_on,
                    payload, supersedes_actual_id, is_current, created_at
                ) VALUES (
                    :actual_id, :project_id, 'unaudited-pending-actual',
                    'PROJECT', :project_id, 'project_cost_total', 125.00, 'RUB',
                    false, :observation_id, DATE '2026-07-20',
                    CAST(:payload AS json), NULL, true, now()
                )
                """
            ),
            {
                "actual_id": unaudited_actual_id,
                "project_id": project_id,
                "observation_id": observation_id,
                "payload": json.dumps(unaudited_payload),
            },
        )
    unaudited_engine.dispose()
    assert "actuals creation audit linkage is invalid" in str(error_info.value)
    invalid_policy_actual_id = f"actual-invalid-policy-{suffix}"
    invalid_policy_task_id = f"task-invalid-policy-{suffix}"
    invalid_policy_payload = {
        **actual_draft,
        "evidence_value": {
            **actual_evidence_value,
            "actual_key": "invalid-policy-link",
        },
        "actuals_policy_version_id": f"missing-policy-{suffix}",
        "actuals_policy_content_hash": "6" * 64,
        "approval_task_id": invalid_policy_task_id,
    }
    invalid_policy_engine = create_engine(database_url)
    with (
        pytest.raises(DBAPIError) as error_info,
        invalid_policy_engine.begin() as connection,
    ):
        _insert_review_task(
            connection,
            task_id=invalid_policy_task_id,
            project_id=project_id,
            task_type="ACTUAL_FACT_REVIEW",
            entity_type="actual_record",
            entity_id=invalid_policy_actual_id,
            assigned_role="AUDITOR",
            payload={
                "created_by": "actual-author",
                "actual_id": invalid_policy_actual_id,
                "actual_key": "invalid-policy-link",
                "actual_submission_hash": "7" * 64,
                "source_observation_id": observation_id,
                "source_leaf_ids": source_leaf_ids,
                "project_outcome_evidence_ids": outcome_evidence_ids,
                "actuals_policy_version_id": f"missing-policy-{suffix}",
                "actuals_policy_content_hash": "6" * 64,
                "review_role": "AUDITOR",
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO actual_records (
                    id, project_id, actual_key, entity_type, entity_id, metric,
                    value, unit, verified, source_observation_id, occurred_on,
                    payload, supersedes_actual_id, is_current, created_at
                ) VALUES (
                    :actual_id, :project_id, 'invalid-policy-link', 'PROJECT',
                    :project_id, 'project_cost_total', 125.00, 'RUB', false,
                    :observation_id, DATE '2026-07-20', CAST(:payload AS json),
                    NULL, true, now()
                )
                """
            ),
            {
                "actual_id": invalid_policy_actual_id,
                "project_id": project_id,
                "observation_id": observation_id,
                "payload": json.dumps(invalid_policy_payload),
            },
        )
    invalid_policy_engine.dispose()
    assert "actuals policy linkage is invalid" in str(error_info.value)
    _assert_rejected(
        database_url,
        "UPDATE actual_records SET value=126.00 WHERE id=:actual_id",
        {"actual_id": actual_id},
        "actual record identity or basis is immutable",
    )
    _assert_rejected(
        database_url,
        "DELETE FROM actual_records WHERE id=:actual_id",
        {"actual_id": actual_id},
        "actual record history cannot be deleted",
    )
    _assert_rejected(
        database_url,
        """
        UPDATE variance_records
        SET absolute_variance=26.00
        WHERE id=:variance_id
        """,
        {"variance_id": variance_id},
        "variance identity or arithmetic is immutable",
    )
    _assert_rejected(
        database_url,
        "DELETE FROM variance_records WHERE id=:variance_id",
        {"variance_id": variance_id},
        "variance history cannot be deleted",
    )
    _assert_rejected(
        database_url,
        """
        UPDATE calibration_examples
        SET target_value=126.00
        WHERE id=:calibration_id
        """,
        {"calibration_id": calibration_id},
        "invalid calibration review mutation",
    )
    _assert_rejected(
        database_url,
        "DELETE FROM calibration_examples WHERE id=:calibration_id",
        {"calibration_id": calibration_id},
        "calibration example history cannot be deleted",
    )
    with pytest.raises(RuntimeError, match="Cannot remove actuals history guards"):
        command.downgrade(Config("alembic.ini"), "e5b8d3f7a642")
    engine.dispose()
