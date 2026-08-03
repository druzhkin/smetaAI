from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from tenderguard.config import get_settings


def _require_postgresql() -> str:
    database_url = get_settings().database_url
    if not database_url.startswith("postgresql"):
        pytest.skip("FGIS CS acquisition immutability requires PostgreSQL")
    return database_url


def test_postgresql_fgiscs_acquisition_is_append_only() -> None:
    database_url = _require_postgresql()
    suffix = uuid4().hex
    project_id = f"project-fgiscs-{suffix}"
    catalog_id = f"catalog-fgiscs-{suffix}"
    policy_id = f"policy-fgiscs-{suffix}"
    document_set_id = f"document-set-fgiscs-{suffix}"
    match_id = f"match-fgiscs-{suffix}"
    qualification_id = f"qualification-fgiscs-{suffix}"
    acquisition_id = f"acquisition-fgiscs-{suffix}"
    engine = create_engine(database_url)

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO projects (
                    id, organization_id, code, name, state, blocked_resume_state,
                    current_document_set_revision_id, row_version, created_at, updated_at
                ) VALUES (
                    :project_id, 'ci-org', :code, 'FGIS CS guard CI',
                    'PRICING_IN_PROGRESS', NULL, :document_set_id, 1, now(), now()
                )
                """
            ),
            {
                "project_id": project_id,
                "code": f"FGIS-GUARD-{suffix}",
                "document_set_id": document_set_id,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO document_set_revisions (
                    id, project_id, manifest_hash, revision_ids, status,
                    created_by, created_at, confirmed_by, confirmed_at
                ) VALUES (
                    :document_set_id, :project_id, :manifest_hash, CAST('[]' AS json),
                    'CONFIRMED', 'ci-author', now(), 'ci-reviewer', now()
                )
                """
            ),
            {
                "document_set_id": document_set_id,
                "project_id": project_id,
                "manifest_hash": "a" * 64,
            },
        )
        for version_id, kind, marker in (
            (catalog_id, "catalog", "b"),
            (policy_id, "fgis_cs_acquisition_policy", "c"),
        ):
            connection.execute(
                text(
                    """
                    INSERT INTO controlled_versions (
                        id, kind, version_label, content_hash, status, payload,
                        approved_by, approved_at
                    ) VALUES (
                        :version_id, :kind, :version_label, :content_hash,
                        'DRAFT', CAST('{}' AS json), NULL, NULL
                    )
                    """
                ),
                {
                    "version_id": version_id,
                    "kind": kind,
                    "version_label": f"ci-{suffix}-{kind}",
                    "content_hash": marker * 64,
                },
            )
            connection.execute(
                text(
                    """
                    UPDATE controlled_versions
                    SET status='APPROVED', approved_by='ci-reviewer', approved_at=now()
                    WHERE id=:version_id
                    """
                ),
                {"version_id": version_id},
            )
        connection.execute(
            text(
                """
                INSERT INTO nomenclature_matches (
                    id, project_id, source_item_id, canonical_item_id, match_class,
                    status, catalog_version_id, supersedes_match_id, is_current,
                    payload, created_at, updated_at
                ) VALUES (
                    :match_id, :project_id, 'ci-item', 'ci-canonical', 'EXACT',
                    'VERIFIED', :catalog_id, NULL, true, CAST('{}' AS json), now(), now()
                )
                """
            ),
            {
                "match_id": match_id,
                "project_id": project_id,
                "catalog_id": catalog_id,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO adapter_qualifications (
                    id, adapter_name, adapter_version, status, valid_until,
                    test_evidence_hash, payload, approved_by, approved_at
                ) VALUES (
                    :qualification_id, 'ci-fgiscs', '1', 'APPROVED', NULL,
                    :test_hash, CAST('{}' AS json), 'ci-reviewer', now()
                )
                """
            ),
            {"qualification_id": qualification_id, "test_hash": "d" * 64},
        )
        connection.execute(
            text(
                """
                INSERT INTO fgiscs_acquisitions (
                    id, project_id, item_id, nomenclature_match_id,
                    document_set_revision_id, policy_version_id,
                    adapter_qualification_id, status, artifact_object_hash,
                    artifact_object_key, artifact_size_bytes, acquired_at,
                    payload, created_at
                ) VALUES (
                    :acquisition_id, :project_id, 'ci-item', :match_id,
                    :document_set_id, :policy_id, :qualification_id, 'UNVERIFIED',
                    :artifact_hash, 'objects/ci', 10, now(), CAST('{}' AS json), now()
                )
                """
            ),
            {
                "acquisition_id": acquisition_id,
                "project_id": project_id,
                "match_id": match_id,
                "document_set_id": document_set_id,
                "policy_id": policy_id,
                "qualification_id": qualification_id,
                "artifact_hash": "e" * 64,
            },
        )
    engine.dispose()

    for statement in (
        "UPDATE fgiscs_acquisitions SET status='VERIFIED' WHERE id=:acquisition_id",
        "DELETE FROM fgiscs_acquisitions WHERE id=:acquisition_id",
    ):
        engine = create_engine(database_url)
        with pytest.raises(DBAPIError) as exc_info, engine.begin() as connection:
            connection.execute(text(statement), {"acquisition_id": acquisition_id})
        engine.dispose()
        assert "immutable TenderGuard record cannot be changed" in str(exc_info.value)
