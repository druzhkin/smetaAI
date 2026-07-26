from __future__ import annotations

import base64
from datetime import timedelta
from io import BytesIO
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tenderguard.application.governance import GovernanceService
from tenderguard.application.operational_qualification import build_result_envelope
from tenderguard.application.production_qualification import (
    ProductionGateEvidenceReviewCommand,
    ProductionGateEvidenceRevocationCommand,
    ProductionGateEvidenceService,
)
from tenderguard.config import Settings
from tenderguard.domain.common import canonical_json, utc_now
from tenderguard.domain.enums import ActorRole
from tenderguard.domain.operational_qualification import QualificationFinding
from tenderguard.domain.production_qualification import (
    ProductionEvidenceArtifact,
    ProductionGateEvidenceStatement,
    ProductionGateEvidenceSubmission,
)
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.database import (
    create_database_engine,
    create_schema_for_tests,
    create_session_factory,
)
from tenderguard.infrastructure.object_store import LocalObjectStore

ORGANIZATION_ID = "org-production-evidence"
BUILD_REFERENCE = "git:" + ("a" * 40)
AUDIT_KEY = "production-gate-evidence-audit-key-at-least-32-bytes"


def _actor(actor_id: str, *roles: ActorRole) -> Actor:
    return Actor(
        actor_id=actor_id,
        organization_id=ORGANIZATION_ID,
        roles=frozenset(roles),
    )


def _approve_version(
    *,
    governance: GovernanceService,
    creator: Actor,
    approver: Actor,
    kind: str,
    label: str,
    payload: dict[str, object],
) -> tuple[str, str]:
    version = governance.create_version(
        actor=creator,
        kind=kind,
        version_label=label,
        payload=payload,
        request_id=f"request-create-{label}",
        reason=f"Create controlled {label}",
    )
    approved = governance.approve_version(
        actor=approver,
        version_id=version.version_id,
        request_id=f"request-approve-{label}",
        reason=f"Independently approve {label}",
    )
    return approved.version_id, approved.content_hash


def _base_evidence_profile(
    *,
    gate_name: str,
    mode: str,
) -> dict[str, object]:
    return {
        "schema_version": "tenderguard.production-gate-evidence-profile/v1",
        "gate_name": gate_name,
        "expected_application_build_reference": BUILD_REFERENCE,
        "allowed_environments": ["qualification"],
        "maximum_evidence_age_days": 30,
        "evidence_mode": mode,
        "required_artifact_categories": ["QUALIFICATION_RESULT"]
        if mode == "INTERNAL_QUALIFICATION_RESULT"
        else ["SIGNED_REPORT"],
        "allowed_artifact_categories": ["QUALIFICATION_RESULT", "TELEMETRY"]
        if mode == "INTERNAL_QUALIFICATION_RESULT"
        else ["SIGNED_REPORT", "SUPPORTING_LOG"],
        "maximum_artifact_count": 10,
        "maximum_artifact_bytes": 1_000_000,
        "maximum_total_artifact_bytes": 5_000_000,
        "required_claim_keys": ["SCOPE"],
        "approval_roles": ["AUDITOR"],
    }


def _external_submission(
    *,
    organization_id: str,
    gate_name: str,
    profile_version_id: str,
    profile_content_hash: str,
    artifact: ProductionEvidenceArtifact,
    private_key: Ed25519PrivateKey,
    attester_id: str,
    attester_key_id: str,
) -> ProductionGateEvidenceSubmission:
    completed_at = utc_now() - timedelta(minutes=1)
    started_at = completed_at - timedelta(hours=1)
    statement = ProductionGateEvidenceStatement(
        schema_version="tenderguard.production-gate-evidence/v1",
        organization_id=organization_id,
        gate_name=gate_name,
        profile_version_id=profile_version_id,
        profile_content_hash=profile_content_hash,
        application_build_reference=BUILD_REFERENCE,
        environment="qualification",
        executed_by="external-control-team",
        started_at=started_at,
        completed_at=completed_at,
        artifacts=(artifact,),
        claims={"SCOPE": f"{gate_name}-full-scope"},
        technical_result_hash=None,
    )
    signature = base64.b64encode(private_key.sign(canonical_json(statement))).decode("ascii")
    return ProductionGateEvidenceSubmission(
        profile_version_id=profile_version_id,
        profile_content_hash=profile_content_hash,
        environment="qualification",
        executed_by=statement.executed_by,
        started_at=started_at,
        completed_at=completed_at,
        artifacts=(artifact,),
        claims=statement.claims,
        signed_statement_hash=statement.statement_hash,
        attester_id=attester_id,
        attester_key_id=attester_key_id,
        attestation_signature_b64=signature,
    )


def _internal_result_submission(
    *,
    store: LocalObjectStore,
    profile_version_id: str,
    profile_content_hash: str,
    result_type: str,
) -> ProductionGateEvidenceSubmission:
    completed_at = utc_now() - timedelta(minutes=1)
    started_at = completed_at - timedelta(minutes=5)
    result = build_result_envelope(
        qualification_type=result_type,
        status="TECHNICAL_VERIFICATION_PASSED",
        profile_version_id=profile_version_id,
        profile_content_hash=profile_content_hash,
        started_at=started_at,
        completed_at=completed_at,
        findings=(
            QualificationFinding(
                code=f"{result_type}_CONTROL",
                passed=True,
                message=f"{result_type} qualification passed",
            ),
        ),
        evidence=(
            {
                "target_environment": "qualification",
                "expected_application_build_reference": BUILD_REFERENCE,
            }
            if result_type == "LOAD"
            else {
                "source_environment": "qualification",
                "restore_environment": "isolated-restore",
                "application_build_reference": BUILD_REFERENCE,
            }
        ),
    )
    result_bytes = result.model_dump_json(indent=2).encode("utf-8")
    stored = store.put(BytesIO(result_bytes))
    return ProductionGateEvidenceSubmission(
        profile_version_id="pending",
        profile_content_hash="0" * 64,
        environment="qualification",
        executed_by=f"{result_type.lower()}-runner",
        started_at=started_at,
        completed_at=completed_at,
        artifacts=(
            ProductionEvidenceArtifact(
                category="QUALIFICATION_RESULT",
                object_hash=stored.object_hash,
                size_bytes=stored.size_bytes,
                media_type="application/json",
                source_reference=f"runner-output:{result.result_hash}",
            ),
        ),
        claims={"SCOPE": f"{result_type}-full-scope"},
        technical_result=result,
    )


def test_all_non_business_production_gates_require_live_approved_evidence(
    tmp_path: Path,
) -> None:
    settings = Settings(
        app_env="test",
        application_build_reference=BUILD_REFERENCE,
        database_url="sqlite+pysqlite://",
        local_object_store_path=tmp_path / "objects",
        local_quarantine_store_path=tmp_path / "quarantine",
        audit_signing_key=AUDIT_KEY,
        audit_signing_key_id="production-evidence-key-1",
    )
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    sessions = create_session_factory(engine)
    store = LocalObjectStore(tmp_path / "objects")
    profile_creator = _actor("profile-owner", ActorRole.METHODOLOGY_OWNER)
    profile_approver = _actor("profile-approver", ActorRole.METHODOLOGY_OWNER)
    submitter = _actor("evidence-registrar", ActorRole.AUDITOR)
    reviewer = _actor("evidence-reviewer", ActorRole.AUDITOR)
    revoker = _actor("evidence-revoker", ActorRole.ADMIN)
    private_key = Ed25519PrivateKey.generate()
    public_key = base64.b64encode(private_key.public_key().public_bytes_raw()).decode("ascii")
    attester_id = "independent-assurance-provider"
    attester_key_id = "assurance-key-2026"
    external_bytes = b"independently signed production qualification report"
    stored_external = store.put(BytesIO(external_bytes))
    external_artifact = ProductionEvidenceArtifact(
        category="SIGNED_REPORT",
        object_hash=stored_external.object_hash,
        size_bytes=stored_external.size_bytes,
        media_type="application/pdf",
        source_reference="assurance-report:2026-Q3",
    )
    bindings: dict[str, dict[str, object]] = {}

    with sessions.begin() as session:
        governance = GovernanceService(
            session=session,
            settings=settings,
            object_store=store,
        )
        source_profiles: dict[str, tuple[str, str]] = {}
        source_profiles["LOAD"] = _approve_version(
            governance=governance,
            creator=profile_creator,
            approver=profile_approver,
            kind="load_test_profile",
            label="load-source-profile",
            payload={
                "schema_version": "tenderguard.load-profile/v1",
                "target_environment": "qualification",
                "expected_application_build_reference": BUILD_REFERENCE,
                "base_url": "https://qualification.example.test",
                "duration_seconds": 60,
                "concurrency": 2,
                "maximum_requests": 1000,
                "request_timeout_seconds": 10,
                "maximum_response_bytes": 100000,
                "auth_mode": "NONE",
                "allow_production_target": False,
                "endpoints": [
                    {
                        "name": "runtime",
                        "method": "GET",
                        "path": "/v1/runtime-config",
                        "weight": 1,
                        "expected_statuses": [200],
                        "slo": {
                            "minimum_success_ratio": "0.99",
                            "maximum_p95_ms": "1000",
                            "maximum_p99_ms": "2000",
                            "minimum_requests_per_second": "1",
                            "minimum_completed_requests": 1,
                        },
                    }
                ],
                "overall_slo": {
                    "minimum_success_ratio": "0.99",
                    "maximum_p95_ms": "1000",
                    "maximum_p99_ms": "2000",
                    "minimum_requests_per_second": "1",
                    "minimum_completed_requests": 1,
                },
            },
        )
        source_profiles["RECOVERY"] = _approve_version(
            governance=governance,
            creator=profile_creator,
            approver=profile_approver,
            kind="recovery_profile",
            label="recovery-source-profile",
            payload={
                "schema_version": "tenderguard.recovery-profile/v1",
                "source_environment": "qualification",
                "restore_environment": "isolated-restore",
                "expected_application_build_reference": BUILD_REFERENCE,
                "maximum_rpo_seconds": 3600,
                "maximum_rto_seconds": 7200,
                "require_worm": False,
                "require_external_audit_anchor": False,
                "require_oidc_configuration": False,
                "require_export_signing_configuration": False,
                "require_integration_signing_configuration": False,
                "required_adapter_qualification_ids": ["adapter-qualification-1"],
                "required_golden_snapshot_ids": ["snapshot-golden-1"],
            },
        )
        evidence_profiles: dict[str, tuple[str, str]] = {}
        for gate_name in (
            "rules_and_catalog_calibration",
            "damaged_conflicting_document_resilience",
            "security_review",
            "methodology_approval",
        ):
            payload = _base_evidence_profile(
                gate_name=gate_name,
                mode="EXTERNAL_ATTESTED_PACKAGE",
            )
            payload.update(
                {
                    "trusted_attester_id": attester_id,
                    "trusted_attester_key_id": attester_key_id,
                    "trusted_attester_public_key_b64": public_key,
                }
            )
            evidence_profiles[gate_name] = _approve_version(
                governance=governance,
                creator=profile_creator,
                approver=profile_approver,
                kind="production_gate_evidence_profile",
                label=f"evidence-profile-{gate_name}",
                payload=payload,
            )
        for gate_name, result_type in (
            ("load_test", "LOAD"),
            ("backup_restore", "RECOVERY"),
        ):
            payload = _base_evidence_profile(
                gate_name=gate_name,
                mode="INTERNAL_QUALIFICATION_RESULT",
            )
            payload.update(
                {
                    "source_profile_version_id": source_profiles[result_type][0],
                    "source_profile_content_hash": source_profiles[result_type][1],
                    "expected_qualification_type": result_type,
                }
            )
            evidence_profiles[gate_name] = _approve_version(
                governance=governance,
                creator=profile_creator,
                approver=profile_approver,
                kind="production_gate_evidence_profile",
                label=f"evidence-profile-{gate_name}",
                payload=payload,
            )

    for gate_name in (
        "rules_and_catalog_calibration",
        "damaged_conflicting_document_resilience",
        "security_review",
        "methodology_approval",
    ):
        profile_id, profile_hash = evidence_profiles[gate_name]
        submission = _external_submission(
            organization_id=ORGANIZATION_ID,
            gate_name=gate_name,
            profile_version_id=profile_id,
            profile_content_hash=profile_hash,
            artifact=external_artifact,
            private_key=private_key,
            attester_id=attester_id,
            attester_key_id=attester_key_id,
        )
        with sessions.begin() as session:
            service = ProductionGateEvidenceService(
                session=session,
                settings=settings,
                object_store=store,
            )
            package = service.submit_package(
                actor=submitter,
                submission=submission,
                request_id=f"request-submit-{gate_name}",
                reason=f"Register signed {gate_name} evidence",
            )
            with pytest.raises(ValueError, match="four-eyes"):
                service.review_package(
                    actor=submitter,
                    package_id=package.package_id,
                    command=ProductionGateEvidenceReviewCommand(
                        decision="APPROVED",
                        reason="Self approval must fail",
                    ),
                    request_id=f"request-self-review-{gate_name}",
                )
        with sessions.begin() as session:
            service = ProductionGateEvidenceService(
                session=session,
                settings=settings,
                object_store=store,
            )
            approved = service.review_package(
                actor=reviewer,
                package_id=package.package_id,
                command=ProductionGateEvidenceReviewCommand(
                    decision="APPROVED",
                    reason=f"Independently approve {gate_name} evidence",
                ),
                request_id=f"request-review-{gate_name}",
            )
            assert approved.approval_hash is not None
            assert approved.reviewed_at is not None
            bindings[gate_name] = {
                "status": "PASSED",
                "evidence_hash": approved.approval_hash,
                "evidence_package_id": approved.package_id,
                "source_reference": (f"production_gate_evidence_package:{approved.package_id}"),
                "owner_id": profile_creator.actor_id,
                "approved_by": reviewer.actor_id,
                "approved_at": approved.reviewed_at.isoformat(),
                "environment": "qualification",
            }
            assert service.gate_binding_valid(
                bindings[gate_name],
                organization_id=ORGANIZATION_ID,
                expected_gate_name=gate_name,
            )

    for gate_name, result_type in (
        ("load_test", "LOAD"),
        ("backup_restore", "RECOVERY"),
    ):
        source_id, source_hash = source_profiles[result_type]
        profile_id, profile_hash = evidence_profiles[gate_name]
        submission = _internal_result_submission(
            store=store,
            profile_version_id=source_id,
            profile_content_hash=source_hash,
            result_type=result_type,
        ).model_copy(
            update={
                "profile_version_id": profile_id,
                "profile_content_hash": profile_hash,
            }
        )
        with sessions.begin() as session:
            service = ProductionGateEvidenceService(
                session=session,
                settings=settings,
                object_store=store,
            )
            package = service.submit_package(
                actor=submitter,
                submission=submission,
                request_id=f"request-submit-{gate_name}",
                reason=f"Register technical {gate_name} result",
            )
        with sessions.begin() as session:
            service = ProductionGateEvidenceService(
                session=session,
                settings=settings,
                object_store=store,
            )
            approved = service.review_package(
                actor=reviewer,
                package_id=package.package_id,
                command=ProductionGateEvidenceReviewCommand(
                    decision="APPROVED",
                    reason=f"Independently approve {gate_name} result",
                ),
                request_id=f"request-review-{gate_name}",
            )
            assert approved.approval_hash is not None
            assert approved.reviewed_at is not None
            bindings[gate_name] = {
                "status": "PASSED",
                "evidence_hash": approved.approval_hash,
                "evidence_package_id": approved.package_id,
                "source_reference": (f"production_gate_evidence_package:{approved.package_id}"),
                "owner_id": profile_creator.actor_id,
                "approved_by": reviewer.actor_id,
                "approved_at": approved.reviewed_at.isoformat(),
                "environment": "qualification",
            }
            assert service.gate_binding_valid(
                bindings[gate_name],
                organization_id=ORGANIZATION_ID,
                expected_gate_name=gate_name,
            )

    tampered = dict(bindings["security_review"])
    tampered["evidence_hash"] = "0" * 64
    with sessions.begin() as session:
        service = ProductionGateEvidenceService(
            session=session,
            settings=settings,
            object_store=store,
        )
        assert not service.gate_binding_valid(
            tampered,
            organization_id=ORGANIZATION_ID,
            expected_gate_name="security_review",
        )
        revoked = service.revoke_package(
            actor=revoker,
            package_id=str(bindings["security_review"]["evidence_package_id"]),
            command=ProductionGateEvidenceRevocationCommand(
                reason="A material post-review defect invalidated the report",
            ),
            request_id="request-revoke-security-evidence",
        )
        assert revoked.revoked
        assert not service.gate_binding_valid(
            bindings["security_review"],
            organization_id=ORGANIZATION_ID,
            expected_gate_name="security_review",
        )

    engine.dispose()
