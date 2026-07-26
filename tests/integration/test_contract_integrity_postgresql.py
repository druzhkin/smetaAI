from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from tenderguard.config import get_settings


def _require_postgresql() -> str:
    database_url = get_settings().database_url
    if not database_url.startswith("postgresql"):
        pytest.skip("Contract history guards require PostgreSQL")
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


def test_postgresql_contract_history_rejects_tampering_and_deletion() -> None:
    database_url = _require_postgresql()
    suffix = uuid4().hex
    project_id = f"project-contract-guard-{suffix}"
    term_id = f"term-contract-guard-{suffix}"
    engine = create_engine(database_url)
    payload = (
        '{"kind":"PENALTIES","value":"0.1% per day",'
        '"observation_ids":["obs-1"],"created_by":"author"}'
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO projects (
                    id, organization_id, code, name, state, blocked_resume_state,
                    current_document_set_revision_id, row_version,
                    created_at, updated_at
                ) VALUES (
                    :project_id, 'ci-org', :code, 'Contract guard CI', 'DRAFT',
                    NULL, NULL, 1, now(), now()
                )
                """
            ),
            {"project_id": project_id, "code": f"CONTRACT-GUARD-{suffix}"},
        )
        connection.execute(
            text(
                """
                INSERT INTO contract_terms (
                    id, project_id, kind, verified, cost_impact_resolved,
                    supersedes_term_id, is_current, payload, created_at, updated_at
                ) VALUES (
                    :term_id, :project_id, 'PENALTIES', false, false,
                    NULL, true, CAST(:payload AS json), now(), now()
                )
                """
            ),
            {
                "term_id": term_id,
                "project_id": project_id,
                "payload": payload,
            },
        )
    engine.dispose()

    _assert_rejected(
        database_url,
        """
        UPDATE contract_terms
        SET payload=CAST('{"value":"tampered"}' AS json),
            updated_at=updated_at + interval '1 second'
        WHERE id=:term_id
        """,
        {"term_id": term_id},
        "invalid contract term transition",
    )
    _assert_rejected(
        database_url,
        "DELETE FROM contract_terms WHERE id=:term_id",
        {"term_id": term_id},
        "contract term history cannot be deleted",
    )

    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE contract_terms
                SET is_current=false, updated_at=updated_at + interval '1 second'
                WHERE id=:term_id
                """
            ),
            {"term_id": term_id},
        )
    engine.dispose()

    _assert_rejected(
        database_url,
        """
        UPDATE contract_terms
        SET payload=payload, updated_at=updated_at + interval '1 second'
        WHERE id=:term_id
        """,
        {"term_id": term_id},
        "superseded contract term is immutable",
    )
