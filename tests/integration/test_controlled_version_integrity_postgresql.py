from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from tenderguard.config import get_settings


def _require_postgresql() -> str:
    database_url = get_settings().database_url
    if not database_url.startswith("postgresql"):
        pytest.skip("Controlled version lifecycle guards require PostgreSQL")
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


def test_postgresql_controlled_versions_allow_only_exact_draft_approval_transition() -> None:
    database_url = _require_postgresql()
    suffix = uuid4().hex
    version_id = f"controlled-lifecycle-{suffix}"
    invalid_insert_id = f"controlled-invalid-insert-{suffix}"
    insert_sql = """
        INSERT INTO controlled_versions (
            id, kind, version_label, content_hash, status, payload,
            approved_by, approved_at
        ) VALUES (
            :version_id, 'calculation_model', :version_label, :content_hash,
            :status, CAST(:payload AS json), CAST(:approved_by AS varchar),
            CASE WHEN CAST(:approved_by AS varchar) IS NULL THEN NULL ELSE now() END
        )
    """
    _assert_rejected(
        database_url,
        insert_sql,
        {
            "version_id": invalid_insert_id,
            "version_label": f"invalid-{suffix}",
            "content_hash": "a" * 64,
            "status": "APPROVED",
            "payload": "{}",
            "approved_by": "bypass-reviewer",
        },
        "controlled version must be inserted as an unapproved draft",
    )
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(insert_sql),
            {
                "version_id": version_id,
                "version_label": f"valid-{suffix}",
                "content_hash": "b" * 64,
                "status": "DRAFT",
                "payload": "{}",
                "approved_by": None,
            },
        )
    engine.dispose()

    _assert_rejected(
        database_url,
        "UPDATE controlled_versions SET payload=CAST(:payload AS json) WHERE id=:id",
        {"id": version_id, "payload": '{"changed":true}'},
        "controlled version basis is immutable",
    )
    _assert_rejected(
        database_url,
        "UPDATE controlled_versions SET status='SUPERSEDED' WHERE id=:id",
        {"id": version_id},
        "invalid controlled version lifecycle transition",
    )

    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE controlled_versions
                SET status='APPROVED', approved_by='independent-reviewer',
                    approved_at=now()
                WHERE id=:id
                """
            ),
            {"id": version_id},
        )
    engine.dispose()

    _assert_rejected(
        database_url,
        "UPDATE controlled_versions SET approved_by=:actor WHERE id=:id",
        {"id": version_id, "actor": "replacement-reviewer"},
        "invalid controlled version lifecycle transition",
    )
    _assert_rejected(
        database_url,
        "DELETE FROM controlled_versions WHERE id=:id",
        {"id": version_id},
        "controlled version cannot be deleted",
    )
