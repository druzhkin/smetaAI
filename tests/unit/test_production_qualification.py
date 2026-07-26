from __future__ import annotations

import base64
from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from tenderguard.domain.common import canonical_json
from tenderguard.domain.production_qualification import (
    ProductionGateEvidenceProfile,
    ProductionGateEvidenceStatement,
    verify_production_evidence_signature,
)

BUILD_REFERENCE = "git:" + ("a" * 40)


def _external_profile(
    *,
    gate_name: str = "security_review",
) -> dict[str, object]:
    public_key = Ed25519PrivateKey.generate().public_key().public_bytes_raw()
    return {
        "schema_version": "tenderguard.production-gate-evidence-profile/v1",
        "gate_name": gate_name,
        "expected_application_build_reference": BUILD_REFERENCE,
        "allowed_environments": ["qualification"],
        "maximum_evidence_age_days": 30,
        "evidence_mode": "EXTERNAL_ATTESTED_PACKAGE",
        "required_artifact_categories": ["SIGNED_REPORT"],
        "allowed_artifact_categories": ["SIGNED_REPORT"],
        "maximum_artifact_count": 2,
        "maximum_artifact_bytes": 1000,
        "maximum_total_artifact_bytes": 2000,
        "required_claim_keys": ["SCOPE"],
        "approval_roles": ["AUDITOR"],
        "trusted_attester_id": "independent-provider",
        "trusted_attester_key_id": "key-1",
        "trusted_attester_public_key_b64": base64.b64encode(public_key).decode(),
    }


def test_load_and_recovery_cannot_substitute_external_attestations() -> None:
    with pytest.raises(
        ValidationError,
        match="require their internal qualification results",
    ):
        ProductionGateEvidenceProfile.model_validate(_external_profile(gate_name="load_test"))


def test_production_evidence_rejects_float_limits_and_operational_approvers() -> None:
    raw = _external_profile()
    raw["maximum_evidence_age_days"] = 30.0
    with pytest.raises(ValidationError, match="Floating-point"):
        ProductionGateEvidenceProfile.model_validate(raw)

    raw = _external_profile()
    raw["approval_roles"] = ["SYSTEM"]
    with pytest.raises(ValidationError, match="independent control roles"):
        ProductionGateEvidenceProfile.model_validate(raw)


def test_external_evidence_signature_covers_the_exact_statement() -> None:
    private_key = Ed25519PrivateKey.generate()
    public_key_b64 = base64.b64encode(private_key.public_key().public_bytes_raw()).decode()
    statement = ProductionGateEvidenceStatement(
        schema_version="tenderguard.production-gate-evidence/v1",
        organization_id="org-1",
        gate_name="security_review",
        profile_version_id="profile-1",
        profile_content_hash="b" * 64,
        application_build_reference=BUILD_REFERENCE,
        environment="qualification",
        executed_by="security-provider",
        started_at=datetime(2026, 7, 24, 10, tzinfo=UTC),
        completed_at=datetime(2026, 7, 24, 11, tzinfo=UTC),
        artifacts=(
            {
                "category": "SIGNED_REPORT",
                "object_hash": "c" * 64,
                "size_bytes": 100,
                "media_type": "application/pdf",
                "source_reference": "report:1",
            },
        ),
        claims={"SCOPE": "FULL"},
    )
    signature = base64.b64encode(private_key.sign(canonical_json(statement))).decode()
    verify_production_evidence_signature(
        statement=statement,
        signature_b64=signature,
        trusted_public_key_b64=public_key_b64,
    )

    tampered = statement.model_copy(update={"environment": "different"})
    with pytest.raises(ValueError, match="signature verification failed"):
        verify_production_evidence_signature(
            statement=tampered,
            signature_b64=signature,
            trusted_public_key_b64=public_key_b64,
        )
