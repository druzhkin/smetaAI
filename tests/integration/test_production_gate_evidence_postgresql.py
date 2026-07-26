from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from tenderguard.config import get_settings


def _require_postgresql() -> str:
    database_url = get_settings().database_url
    if not database_url.startswith("postgresql"):
        pytest.skip("Production gate evidence guards require PostgreSQL")
    return database_url


def _assert_rejected(
    database_url: str,
    statement: str,
    parameters: dict[str, str],
) -> None:
    engine = create_engine(database_url)
    with pytest.raises(DBAPIError) as error_info, engine.begin() as connection:
        connection.execute(text(statement), parameters)
    engine.dispose()
    assert "immutable TenderGuard record cannot be changed" in str(error_info.value)


def test_postgresql_production_gate_evidence_is_append_only() -> None:
    database_url = _require_postgresql()
    suffix = uuid4().hex
    profile_id = f"production-evidence-profile-{suffix}"
    package_id = f"production-evidence-package-{suffix}"
    approval_id = f"production-evidence-approval-{suffix}"
    revocation_id = f"production-evidence-revocation-{suffix}"
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO controlled_versions (
                    id, kind, version_label, content_hash, status, payload,
                    approved_by, approved_at
                ) VALUES (
                    :profile_id, 'production_gate_evidence_profile',
                    :profile_label, :profile_hash, 'DRAFT',
                    CAST('{}' AS json), NULL, NULL
                )
                """
            ),
            {
                "profile_id": profile_id,
                "profile_label": f"profile-{suffix}",
                "profile_hash": "a" * 64,
            },
        )
        connection.execute(
            text(
                """
                UPDATE controlled_versions
                SET status = 'APPROVED',
                    approved_by = 'profile-approver',
                    approved_at = now()
                WHERE id = :profile_id
                """
            ),
            {"profile_id": profile_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO production_gate_evidence_packages (
                    id, organization_id, gate_name, profile_version_id,
                    profile_content_hash, application_build_reference,
                    environment, evidence_mode, package_hash, statement_payload,
                    technical_result_payload, attester_id, attester_key_id,
                    attestation_signature_b64, submitted_by, submitted_at
                ) VALUES (
                    :package_id, 'qualification-ci', 'security_review',
                    :profile_id, :profile_hash, :build_reference,
                    'qualification', 'EXTERNAL_ATTESTED_PACKAGE', :package_hash,
                    CAST('{}' AS json), NULL, 'attester', 'key-1', 'signature',
                    'registrar', now()
                )
                """
            ),
            {
                "package_id": package_id,
                "profile_id": profile_id,
                "profile_hash": "a" * 64,
                "build_reference": "git:" + ("b" * 40),
                "package_hash": suffix + ("c" * 32),
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO production_gate_evidence_approvals (
                    id, package_id, decision, approval_hash, reason,
                    reviewed_by, reviewed_at
                ) VALUES (
                    :approval_id, :package_id, 'APPROVED', :approval_hash,
                    'Independent approval', 'reviewer', now()
                )
                """
            ),
            {
                "approval_id": approval_id,
                "package_id": package_id,
                "approval_hash": suffix + ("d" * 32),
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO production_gate_evidence_revocations (
                    id, package_id, reason, revoked_by, revoked_at
                ) VALUES (
                    :revocation_id, :package_id, 'Material defect',
                    'auditor', now()
                )
                """
            ),
            {
                "revocation_id": revocation_id,
                "package_id": package_id,
            },
        )
    engine.dispose()

    for table_name, row_id in (
        ("production_gate_evidence_packages", package_id),
        ("production_gate_evidence_approvals", approval_id),
        ("production_gate_evidence_revocations", revocation_id),
    ):
        _assert_rejected(
            database_url,
            f"DELETE FROM {table_name} WHERE id=:id",
            {"id": row_id},
        )
    _assert_rejected(
        database_url,
        "UPDATE production_gate_evidence_packages SET package_hash=:value WHERE id=:id",
        {"id": package_id, "value": "9" * 64},
    )
