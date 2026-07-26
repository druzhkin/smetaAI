from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from tenderguard.config import get_settings


def _require_postgresql() -> str:
    database_url = get_settings().database_url
    if not database_url.startswith("postgresql"):
        pytest.skip("Approval evidence guards require PostgreSQL")
    return database_url


def _assert_rejected(
    database_url: str,
    statement: str,
    parameters: dict[str, object],
    expected_message: str,
) -> None:
    engine = create_engine(database_url)
    with pytest.raises(DBAPIError) as error_info, engine.begin() as connection:
        connection.execute(text(statement), parameters)
    engine.dispose()
    assert expected_message in str(error_info.value)


def test_postgresql_approval_task_and_record_must_form_one_atomic_decision() -> None:
    database_url = _require_postgresql()
    suffix = uuid4().hex
    project_id = f"project-approval-guard-{suffix}"
    task_id = f"task-approval-guard-{suffix}"
    approval_id = f"approval-guard-{suffix}"
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO projects (
                    id, organization_id, code, name, state, row_version,
                    created_at, updated_at
                ) VALUES (
                    :id, :organization_id, :code, 'Approval evidence guard',
                    'EXPERT_REVIEW', 1, now(), now()
                )
                """
            ),
            {
                "id": project_id,
                "organization_id": f"org-approval-guard-{suffix}",
                "code": f"APPROVAL-GUARD-{suffix}",
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO approval_tasks (
                    id, project_id, task_type, entity_type, entity_id,
                    assigned_role, status, required, payload,
                    created_at, updated_at
                ) VALUES (
                    :id, :project_id, 'MANUAL_EVIDENCE_REVIEW',
                    'evidence_observation', :entity_id, 'REVIEWER',
                    'PENDING', true, CAST(:payload AS json), now(), now()
                )
                """
            ),
            {
                "id": task_id,
                "project_id": project_id,
                "entity_id": f"observation-{suffix}",
                "payload": '{"created_by":"technical-author"}',
            },
        )
    engine.dispose()

    _assert_rejected(
        database_url,
        """
        UPDATE approval_tasks
        SET payload=CAST(:payload AS json), updated_at=now() + interval '1 second'
        WHERE id=:id
        """,
        {
            "id": task_id,
            "payload": '{"created_by":"replacement-author"}',
        },
        "invalid approval task transition",
    )
    _assert_rejected(
        database_url,
        """
        UPDATE approval_tasks
        SET status='APPROVED', updated_at=now() + interval '1 second'
        WHERE id=:id
        """,
        {"id": task_id},
        "terminal approval task and immutable decision record must agree",
    )
    _assert_rejected(
        database_url,
        """
        INSERT INTO approval_records (
            id, task_id, decision, decided_by, reason, payload, decided_at
        ) VALUES (
            :id, :task_id, 'APPROVED', 'reviewer', 'Forged record',
            CAST('{}' AS json), now()
        )
        """,
        {"id": f"forged-{approval_id}", "task_id": task_id},
        "terminal approval task and immutable decision record must agree",
    )

    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE approval_tasks
                SET status='APPROVED', updated_at=now() + interval '1 second'
                WHERE id=:id
                """
            ),
            {"id": task_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO approval_records (
                    id, task_id, decision, decided_by, reason, payload, decided_at
                ) VALUES (
                    :id, :task_id, 'APPROVED', 'independent-reviewer',
                    'Checked immutable source evidence',
                    CAST(:payload AS json), now() + interval '1 second'
                )
                """
            ),
            {
                "id": approval_id,
                "task_id": task_id,
                "payload": '{"evidence_ids":["observation-source"]}',
            },
        )
    engine.dispose()

    _assert_rejected(
        database_url,
        "UPDATE approval_records SET reason='Rewritten' WHERE id=:id",
        {"id": approval_id},
        "immutable TenderGuard record cannot be changed",
    )
    _assert_rejected(
        database_url,
        "DELETE FROM approval_records WHERE id=:id",
        {"id": approval_id},
        "immutable TenderGuard record cannot be changed",
    )
    _assert_rejected(
        database_url,
        """
        UPDATE approval_tasks
        SET status='PENDING', updated_at=now() + interval '2 seconds'
        WHERE id=:id
        """,
        {"id": task_id},
        "invalid approval task transition",
    )
    _assert_rejected(
        database_url,
        "DELETE FROM approval_tasks WHERE id=:id",
        {"id": task_id},
        "approval task cannot be deleted",
    )


def test_postgresql_approval_task_allows_only_auditable_supersession() -> None:
    database_url = _require_postgresql()
    suffix = uuid4().hex
    project_id = f"project-approval-supersede-{suffix}"
    task_id = f"task-approval-supersede-{suffix}"
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO projects (
                    id, organization_id, code, name, state, row_version,
                    created_at, updated_at
                ) VALUES (
                    :id, :organization_id, :code, 'Approval supersession',
                    'EXPERT_REVIEW', 1, now(), now()
                )
                """
            ),
            {
                "id": project_id,
                "organization_id": f"org-approval-supersede-{suffix}",
                "code": f"APPROVAL-SUPERSEDE-{suffix}",
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO approval_tasks (
                    id, project_id, task_type, entity_type, entity_id,
                    assigned_role, status, required, payload,
                    created_at, updated_at
                ) VALUES (
                    :id, :project_id, 'QUANTITY_REVIEW', 'quantity',
                    :entity_id, 'REVIEWER', 'PENDING', true,
                    CAST(:payload AS json), now(), now()
                )
                """
            ),
            {
                "id": task_id,
                "project_id": project_id,
                "entity_id": f"quantity-{suffix}",
                "payload": '{"created_by":"estimator"}',
            },
        )
        connection.execute(
            text(
                """
                UPDATE approval_tasks
                SET status='SUPERSEDED',
                    payload=CAST(:payload AS json),
                    updated_at=now() + interval '1 second'
                WHERE id=:id
                """
            ),
            {
                "id": task_id,
                "payload": (
                    '{"created_by":"estimator",'
                    '"invalidated_by_document_revision_id":"revision-new",'
                    '"invalidated_document_set_revision_id":"document-set-old",'
                    '"invalidated_at":"2026-07-26T12:00:00+00:00"}'
                ),
            },
        )
    engine.dispose()

    _assert_rejected(
        database_url,
        """
        UPDATE approval_tasks
        SET payload=CAST(:payload AS json), updated_at=now() + interval '2 seconds'
        WHERE id=:id
        """,
        {
            "id": task_id,
            "payload": '{"created_by":"rewritten"}',
        },
        "invalid approval task transition",
    )
