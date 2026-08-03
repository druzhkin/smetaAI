from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from tenderguard.config import get_settings


def _require_postgresql() -> str:
    database_url = get_settings().database_url
    if not database_url.startswith("postgresql"):
        pytest.skip("Automatic rework immutability requires PostgreSQL")
    return database_url


def test_postgresql_automation_rework_dispatch_is_append_only() -> None:
    database_url = _require_postgresql()
    suffix = uuid4().hex
    project_id = f"project-automation-{suffix}"
    source_event_id = f"source-automation-{suffix}"
    command_event_id = f"command-automation-{suffix}"
    run_id = f"run-automation-{suffix}"
    snapshot_id = f"snapshot-automation-{suffix}"
    request_id = f"expert-rework-{suffix}"
    dispatch_id = f"automation-dispatch-{suffix}"
    engine = create_engine(database_url)

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO adapter_qualifications (
                    id, adapter_name, adapter_version, status, valid_until,
                    test_evidence_hash, payload, approved_by, approved_at
                ) VALUES (
                    :qualification_id, :adapter_name, :adapter_version,
                    'APPROVED', NULL, :evidence_hash, CAST('{}' AS json),
                    'ci-qualification-reviewer', now()
                )
                """
            ),
            {
                "qualification_id": f"qualification-automation-{suffix}",
                "adapter_name": f"automation-dispatcher-{suffix}",
                "adapter_version": suffix,
                "evidence_hash": "9" * 64,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO projects (
                    id, organization_id, code, name, state, blocked_resume_state,
                    current_document_set_revision_id, row_version, created_at, updated_at
                ) VALUES (
                    :project_id, 'ci-org', :code, 'Automation rework guard CI',
                    'PRICING_IN_PROGRESS', NULL, 'documents-ci', 1, now(), now()
                )
                """
            ),
            {"project_id": project_id, "code": f"AUTO-REWORK-{suffix}"},
        )
        connection.execute(
            text(
                """
                INSERT INTO calculation_runs (
                    id, project_id, engine_version, status, currency,
                    grand_total, payload, created_at
                ) VALUES (
                    :run_id, :project_id, 'ci-engine', 'VALIDATED', 'RUB',
                    100, CAST('{}' AS json), now()
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
                    :snapshot_id, :project_id, :run_id, 'documents-ci',
                    :input_hash, :output_hash, :snapshot_hash, true,
                    'objects/automation-rework-ci', 'ci-system', now()
                )
                """
            ),
            {
                "snapshot_id": snapshot_id,
                "project_id": project_id,
                "run_id": run_id,
                "input_hash": "a" * 64,
                "output_hash": "b" * 64,
                "snapshot_hash": suffix + ("c" * (64 - len(suffix))),
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO expert_rework_requests (
                    id, project_id, snapshot_id, requested_state, gate_hash,
                    target_stage, payload, requested_by, requested_at
                ) VALUES (
                    :request_id, :project_id, :snapshot_id, 'APPROVED_FOR_BID',
                    :gate_hash, 'PRICING_IN_PROGRESS', CAST('{}' AS json),
                    'ci-expert', now()
                )
                """
            ),
            {
                "request_id": request_id,
                "project_id": project_id,
                "snapshot_id": snapshot_id,
                "gate_hash": "d" * 64,
            },
        )
        for event_id, topic, aggregate_id in (
            (source_event_id, "project.final-review.rework-requested", request_id),
            (command_event_id, "project.automation.pricing.requested", dispatch_id),
        ):
            connection.execute(
                text(
                    """
                    INSERT INTO outbox_events (
                        id, deduplication_key, delivery_deduplication_key, topic,
                        aggregate_id, payload, attempts, available_at, created_at
                    ) VALUES (
                        :event_id, :deduplication_key, :deduplication_key, :topic,
                        :aggregate_id, CAST('{}' AS json), 0, now(), now()
                    )
                    """
                ),
                {
                    "event_id": event_id,
                    "deduplication_key": f"automation-{event_id}",
                    "topic": topic,
                    "aggregate_id": aggregate_id,
                },
            )
        connection.execute(
            text(
                """
                INSERT INTO automation_rework_dispatches (
                    id, rework_request_id, project_id, source_outbox_event_id,
                    command_outbox_event_id, target_stage, command_topic, status,
                    request_hash, dispatch_hash, worker_qualification_id,
                    worker_actor_id, payload, dispatched_at
                ) VALUES (
                    :dispatch_id, :request_id, :project_id, :source_event_id,
                    :command_event_id, 'PRICING_IN_PROGRESS',
                    'project.automation.pricing.requested', 'STAGE_COMMAND_QUEUED',
                    :request_hash, :dispatch_hash, :qualification_id,
                    'automation-worker-ci', CAST('{}' AS json), now()
                )
                """
            ),
            {
                "dispatch_id": dispatch_id,
                "request_id": request_id,
                "project_id": project_id,
                "source_event_id": source_event_id,
                "command_event_id": command_event_id,
                "request_hash": "e" * 64,
                "dispatch_hash": suffix + ("f" * (64 - len(suffix))),
                "qualification_id": f"qualification-automation-{suffix}",
            },
        )
    engine.dispose()

    for statement in (
        "UPDATE automation_rework_dispatches SET status='BLOCKED' WHERE id=:id",
        "DELETE FROM automation_rework_dispatches WHERE id=:id",
    ):
        engine = create_engine(database_url)
        with pytest.raises(DBAPIError) as exc_info, engine.begin() as connection:
            connection.execute(text(statement), {"id": dispatch_id})
        engine.dispose()
        assert "immutable TenderGuard record cannot be changed" in str(exc_info.value)
