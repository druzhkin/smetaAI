from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from tenderguard.config import get_settings


def _require_postgresql() -> str:
    database_url = get_settings().database_url
    if not database_url.startswith("postgresql"):
        pytest.skip("Final expert rework immutability requires PostgreSQL")
    return database_url


def test_postgresql_final_expert_rework_request_is_append_only() -> None:
    database_url = _require_postgresql()
    suffix = uuid4().hex
    project_id = f"project-final-review-{suffix}"
    run_id = f"run-final-review-{suffix}"
    snapshot_id = f"snapshot-final-review-{suffix}"
    rework_id = f"expert-rework-{suffix}"
    engine = create_engine(database_url)

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO projects (
                    id, organization_id, code, name, state, blocked_resume_state,
                    current_document_set_revision_id, row_version, created_at, updated_at
                ) VALUES (
                    :project_id, 'ci-org', :code, 'Final review guard CI',
                    'EXPERT_REVIEW', NULL, 'documents-ci', 1, now(), now()
                )
                """
            ),
            {"project_id": project_id, "code": f"FINAL-REVIEW-{suffix}"},
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
                    'objects/final-review-ci', 'ci-system', now()
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
                    :rework_id, :project_id, :snapshot_id, 'APPROVED_FOR_BID',
                    :gate_hash, 'PRICING_IN_PROGRESS', CAST('{}' AS json),
                    'ci-expert', now()
                )
                """
            ),
            {
                "rework_id": rework_id,
                "project_id": project_id,
                "snapshot_id": snapshot_id,
                "gate_hash": "d" * 64,
            },
        )
    engine.dispose()

    for statement in (
        "UPDATE expert_rework_requests SET target_stage='BLOCKED' WHERE id=:id",
        "DELETE FROM expert_rework_requests WHERE id=:id",
    ):
        engine = create_engine(database_url)
        with pytest.raises(DBAPIError) as exc_info, engine.begin() as connection:
            connection.execute(text(statement), {"id": rework_id})
        engine.dispose()
        assert "immutable TenderGuard record cannot be changed" in str(exc_info.value)
