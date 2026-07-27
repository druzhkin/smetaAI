from __future__ import annotations

import json
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from tenderguard.config import get_settings


def _require_postgresql() -> str:
    database_url = get_settings().database_url
    if not database_url.startswith("postgresql"):
        pytest.skip("Risk history guards require PostgreSQL")
    return database_url


def _assert_rejected(
    database_url: str,
    statement: str,
    parameters: dict[str, str],
    expected_message: str,
) -> None:
    engine = create_engine(database_url)
    with pytest.raises(DBAPIError) as exc_info, engine.begin() as connection:
        connection.execute(text(statement), parameters)
    engine.dispose()
    assert expected_message in str(exc_info.value)


def test_postgresql_risk_history_allows_only_formal_lifecycle_transitions() -> None:
    database_url = _require_postgresql()
    suffix = uuid4().hex
    project_id = f"project-risk-guard-{suffix}"
    model_id = f"risk-model-guard-{suffix}"
    risk_id = f"risk-item-guard-{suffix}"
    calculation_id = f"risk-calculation-guard-{suffix}"
    draft_payload = {
        "risk": {
            "risk_id": risk_id,
            "description": "Guarded risk",
            "probability": "0.1",
            "impact_min": "10",
            "impact_most_likely": "20",
            "impact_max": "30",
            "currency": "RUB",
            "observation_ids": ["observation-1"],
            "status": "IN_REVIEW",
            "correlated": False,
            "correlation_group": None,
            "mitigation_cost_input_id": None,
        },
        "created_by": "risk-author",
    }
    reviewed_payload = {
        **draft_payload,
        "risk": {**draft_payload["risk"], "status": "VERIFIED"},
        "verified_by": "risk-reviewer",
        "verified_at": "2026-07-26T10:00:00+00:00",
        "reviewed_by": "risk-reviewer",
        "reviewed_at": "2026-07-26T10:00:00+00:00",
        "review_decision": "APPROVED",
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
                    :project_id, 'ci-org', :code, 'Risk guard CI', 'DRAFT',
                    NULL, NULL, 1, now(), now()
                )
                """
            ),
            {"project_id": project_id, "code": f"RISK-GUARD-{suffix}"},
        )
        connection.execute(
            text(
                """
                INSERT INTO controlled_versions (
                    id, kind, version_label, content_hash, status, payload,
                    approved_by, approved_at
                ) VALUES (
                    :model_id, 'risk_model', :label, :content_hash, 'DRAFT',
                    CAST('{}' AS json), NULL, NULL
                )
                """
            ),
            {
                "model_id": model_id,
                "label": f"ci-{suffix}",
                "content_hash": "a" * 64,
            },
        )
        connection.execute(
            text(
                """
                UPDATE controlled_versions
                SET status='APPROVED', approved_by='ci-reviewer', approved_at=now()
                WHERE id=:model_id
                """
            ),
            {"model_id": model_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO risk_items (
                    id, project_id, risk_key, status, currency, expected_impact,
                    supersedes_risk_id, is_current, payload, created_at, updated_at
                ) VALUES (
                    :risk_id, :project_id, 'guarded-risk', 'IN_REVIEW', 'RUB',
                    NULL, NULL, true, CAST(:payload AS json), now(), now()
                )
                """
            ),
            {
                "risk_id": risk_id,
                "project_id": project_id,
                "payload": json.dumps(draft_payload),
            },
        )
        connection.execute(
            text(
                """
                UPDATE risk_items
                SET status='VERIFIED',
                    payload=CAST(:payload AS json),
                    updated_at=updated_at + interval '1 second'
                WHERE id=:risk_id
                """
            ),
            {
                "risk_id": risk_id,
                "payload": json.dumps(reviewed_payload),
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO risk_calculations (
                    id, project_id, policy_version_id, status, expected_reserve,
                    currency, unit, supersedes_calculation_id, is_current,
                    payload, created_at
                ) VALUES (
                    :calculation_id, :project_id, :model_id, 'VALIDATED', 2.00,
                    'RUB', 'project', NULL, true, CAST(:payload AS json), now()
                )
                """
            ),
            {
                "calculation_id": calculation_id,
                "project_id": project_id,
                "model_id": model_id,
                "payload": json.dumps(
                    {
                        "basis_type": "RISK_RESERVE",
                        "unit_rate": "2.00",
                        "currency": "RUB",
                        "unit": "project",
                    }
                ),
            },
        )

    _assert_rejected(
        database_url,
        """
        UPDATE risk_items
        SET payload=CAST(:payload AS json),
            updated_at=updated_at + interval '1 second'
        WHERE id=:risk_id
        """,
        {"risk_id": risk_id, "payload": json.dumps({"tampered": True})},
        "invalid risk item transition",
    )
    _assert_rejected(
        database_url,
        "DELETE FROM risk_items WHERE id=:risk_id",
        {"risk_id": risk_id},
        "risk item history cannot be deleted",
    )
    _assert_rejected(
        database_url,
        """
        UPDATE risk_calculations
        SET expected_reserve=3.00
        WHERE id=:calculation_id
        """,
        {"calculation_id": calculation_id},
        "invalid risk calculation transition",
    )
    _assert_rejected(
        database_url,
        "DELETE FROM risk_calculations WHERE id=:calculation_id",
        {"calculation_id": calculation_id},
        "risk calculation history cannot be deleted",
    )

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE risk_calculations
                SET is_current=false
                WHERE id=:calculation_id
                """
            ),
            {"calculation_id": calculation_id},
        )
    engine.dispose()

    _assert_rejected(
        database_url,
        """
        UPDATE risk_calculations
        SET payload=payload
        WHERE id=:calculation_id
        """,
        {"calculation_id": calculation_id},
        "superseded risk calculation is immutable",
    )
