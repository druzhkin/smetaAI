from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from tenderguard.config import get_settings


def _require_postgresql() -> str:
    database_url = get_settings().database_url
    if not database_url.startswith("postgresql"):
        pytest.skip("Calculation evidence guards require a PostgreSQL test database")
    return database_url


def _assert_rejected(
    engine_url: str,
    statement: str,
    parameters: dict[str, str],
    expected_message: str = "immutable TenderGuard record cannot be changed",
) -> None:
    engine = create_engine(engine_url)
    with pytest.raises(DBAPIError) as exc_info, engine.begin() as connection:
        connection.execute(text(statement), parameters)
    engine.dispose()
    assert expected_message in str(exc_info.value)


def test_postgresql_calculation_and_release_evidence_is_immutable() -> None:
    database_url = _require_postgresql()
    suffix = uuid4().hex
    project_id = f"project-{suffix}"
    run_id = f"run-{suffix}"
    snapshot_id = f"snapshot-{suffix}"
    cost_input_id = f"cost-{suffix}"
    release_id = f"release-{suffix}"
    scenario_id = f"scenario-{suffix}"
    engine = create_engine(database_url)

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO projects (
                    id, organization_id, code, name, state, blocked_resume_state,
                    current_document_set_revision_id, row_version, created_at, updated_at
                ) VALUES (
                    :project_id, 'ci-org', :code, 'Calculation guard CI', 'DRAFT',
                    NULL, NULL, 1, now(), now()
                )
                """
            ),
            {"project_id": project_id, "code": f"CALC-GUARD-{suffix}"},
        )
        connection.execute(
            text(
                """
                INSERT INTO calculation_runs (
                    id, project_id, engine_version, status, currency,
                    grand_total, payload, created_at
                ) VALUES (
                    :run_id, :project_id, 'ci-engine', 'VALIDATED', 'RUB',
                    1250.00, CAST('{}' AS json), now()
                )
                """
            ),
            {"run_id": run_id, "project_id": project_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO atomic_cost_inputs (
                    id, project_id, calculation_run_id, semantic_key, category,
                    amount_basis_id, payload, created_at
                ) VALUES (
                    :cost_input_id, :project_id, :run_id, 'pipe', 'MATERIAL',
                    'basis-ci', CAST('{}' AS json), now()
                )
                """
            ),
            {
                "cost_input_id": cost_input_id,
                "project_id": project_id,
                "run_id": run_id,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO calculation_snapshots (
                    id, project_id, calculation_run_id, document_set_revision_id,
                    input_hash, output_hash, snapshot_hash, fixed, object_key,
                    created_by, created_at
                ) VALUES (
                    :snapshot_id, :project_id, :run_id, 'document-set-ci',
                    :input_hash, :output_hash, :snapshot_hash, true,
                    :object_key, 'ci-estimator', now()
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
                "object_key": f"objects/{suffix}",
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO release_decisions (
                    id, project_id, snapshot_id, requested_state, resulting_state,
                    allowed, payload, decided_by, decided_at
                ) VALUES (
                    :release_id, :project_id, :snapshot_id, 'APPROVED_FOR_BID',
                    'BLOCKED', false, CAST('{}' AS json), 'ci-approver', now()
                )
                """
            ),
            {
                "release_id": release_id,
                "project_id": project_id,
                "snapshot_id": snapshot_id,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO scenario_runs (
                    id, project_id, base_calculation_run_id, scenario_version,
                    status, grand_total, payload, created_at
                ) VALUES (
                    :scenario_id, :project_id, :run_id, 'ci-scenario',
                    'VALIDATED', 1300.00, CAST('{}' AS json), now()
                )
                """
            ),
            {
                "scenario_id": scenario_id,
                "project_id": project_id,
                "run_id": run_id,
            },
        )
    engine.dispose()

    _assert_rejected(
        database_url,
        "UPDATE calculation_runs SET grand_total=1 WHERE id=:id",
        {"id": run_id},
    )
    _assert_rejected(
        database_url,
        "UPDATE atomic_cost_inputs SET amount_basis_id='other' WHERE id=:id",
        {"id": cost_input_id},
    )
    _assert_rejected(
        database_url,
        "DELETE FROM calculation_snapshots WHERE id=:id",
        {"id": snapshot_id},
        "fixed calculation snapshot cannot be changed",
    )
    _assert_rejected(
        database_url,
        "UPDATE release_decisions SET allowed=true WHERE id=:id",
        {"id": release_id},
    )
    _assert_rejected(
        database_url,
        "UPDATE scenario_runs SET grand_total=1 WHERE id=:id",
        {"id": scenario_id},
    )
