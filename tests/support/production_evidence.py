from __future__ import annotations

import base64
from datetime import timedelta
from io import BytesIO
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy.orm import Session

from tenderguard.application.governance import GovernanceService
from tenderguard.application.operational_qualification import build_result_envelope
from tenderguard.application.production_qualification import (
    ProductionGateEvidenceReviewCommand,
    ProductionGateEvidenceService,
)
from tenderguard.config import Settings
from tenderguard.domain.common import canonical_json, utc_now
from tenderguard.domain.operational_qualification import QualificationFinding
from tenderguard.domain.production_qualification import (
    ProductionEvidenceArtifact,
    ProductionGateEvidenceStatement,
    ProductionGateEvidenceSubmission,
)
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.object_store import ObjectStore


def register_all_non_business_gate_evidence(
    *,
    session: Session,
    settings: Settings,
    object_store: ObjectStore,
    profile_creator: Actor,
    profile_approver: Actor,
    submitter: Actor,
    reviewer: Actor,
    environment: str,
    label_prefix: str,
) -> dict[str, dict[str, object]]:
    build_reference = settings.application_build_reference
    if build_reference is None:
        raise ValueError("Test evidence requires an immutable build")
    governance = GovernanceService(
        session=session,
        settings=settings,
        object_store=object_store,
    )
    service = ProductionGateEvidenceService(
        session=session,
        settings=settings,
        object_store=object_store,
    )
    private_key = Ed25519PrivateKey.generate()
    public_key = base64.b64encode(private_key.public_key().public_bytes_raw()).decode("ascii")
    attester_id = f"{label_prefix}-assurance-provider"
    attester_key_id = f"{label_prefix}-assurance-key"
    external_content = f"{label_prefix}:signed qualification report".encode()
    external_object = object_store.put(BytesIO(external_content))
    external_artifact = ProductionEvidenceArtifact(
        category="SIGNED_REPORT",
        object_hash=external_object.object_hash,
        size_bytes=external_object.size_bytes,
        media_type="application/pdf",
        source_reference=f"assurance-report:{label_prefix}",
    )

    source_profiles = {
        "LOAD": _approved_version(
            governance=governance,
            creator=profile_creator,
            approver=profile_approver,
            kind="load_test_profile",
            label=f"{label_prefix}-load-source",
            payload=_load_profile(build_reference, environment),
        ),
        "RECOVERY": _approved_version(
            governance=governance,
            creator=profile_creator,
            approver=profile_approver,
            kind="recovery_profile",
            label=f"{label_prefix}-recovery-source",
            payload=_recovery_profile(build_reference, environment),
        ),
    }
    bindings: dict[str, dict[str, object]] = {}
    for gate_name in (
        "rules_and_catalog_calibration",
        "damaged_conflicting_document_resilience",
        "security_review",
        "methodology_approval",
    ):
        raw_profile = _base_profile(
            build_reference=build_reference,
            gate_name=gate_name,
            mode="EXTERNAL_ATTESTED_PACKAGE",
            environment=environment,
        )
        raw_profile.update(
            {
                "trusted_attester_id": attester_id,
                "trusted_attester_key_id": attester_key_id,
                "trusted_attester_public_key_b64": public_key,
            }
        )
        profile_id, profile_hash = _approved_version(
            governance=governance,
            creator=profile_creator,
            approver=profile_approver,
            kind="production_gate_evidence_profile",
            label=f"{label_prefix}-{gate_name}",
            payload=raw_profile,
        )
        completed_at = utc_now() - timedelta(minutes=1)
        started_at = completed_at - timedelta(hours=1)
        statement = ProductionGateEvidenceStatement(
            schema_version="tenderguard.production-gate-evidence/v1",
            organization_id=submitter.organization_id,
            gate_name=gate_name,
            profile_version_id=profile_id,
            profile_content_hash=profile_hash,
            application_build_reference=build_reference,
            environment=environment,
            executed_by=f"{label_prefix}-external-control-team",
            started_at=started_at,
            completed_at=completed_at,
            artifacts=(external_artifact,),
            claims={"SCOPE": "FULL_APPROVED_SCOPE"},
        )
        signature = base64.b64encode(private_key.sign(canonical_json(statement))).decode("ascii")
        package = service.submit_package(
            actor=submitter,
            submission=ProductionGateEvidenceSubmission(
                profile_version_id=profile_id,
                profile_content_hash=profile_hash,
                environment=environment,
                executed_by=statement.executed_by,
                started_at=started_at,
                completed_at=completed_at,
                artifacts=statement.artifacts,
                claims=statement.claims,
                signed_statement_hash=statement.statement_hash,
                attester_id=attester_id,
                attester_key_id=attester_key_id,
                attestation_signature_b64=signature,
            ),
            request_id=f"request-submit-{label_prefix}-{gate_name}",
            reason=f"Register exact {gate_name} qualification evidence",
        )
        bindings[gate_name] = _approve_and_bind(
            service=service,
            package_id=package.package_id,
            gate_name=gate_name,
            owner_id=profile_creator.actor_id,
            reviewer=reviewer,
            environment=environment,
            request_id=f"request-review-{label_prefix}-{gate_name}",
        )

    for gate_name, result_type in (
        ("load_test", "LOAD"),
        ("backup_restore", "RECOVERY"),
    ):
        source_id, source_hash = source_profiles[result_type]
        raw_profile = _base_profile(
            build_reference=build_reference,
            gate_name=gate_name,
            mode="INTERNAL_QUALIFICATION_RESULT",
            environment=environment,
        )
        raw_profile.update(
            {
                "source_profile_version_id": source_id,
                "source_profile_content_hash": source_hash,
                "expected_qualification_type": result_type,
            }
        )
        profile_id, profile_hash = _approved_version(
            governance=governance,
            creator=profile_creator,
            approver=profile_approver,
            kind="production_gate_evidence_profile",
            label=f"{label_prefix}-{gate_name}",
            payload=raw_profile,
        )
        completed_at = utc_now() - timedelta(minutes=1)
        started_at = completed_at - timedelta(minutes=5)
        result = build_result_envelope(
            qualification_type=result_type,
            status="TECHNICAL_VERIFICATION_PASSED",
            profile_version_id=source_id,
            profile_content_hash=source_hash,
            started_at=started_at,
            completed_at=completed_at,
            findings=(
                QualificationFinding(
                    code=f"{result_type}_FULL_CONTROL",
                    passed=True,
                    message=f"{result_type} qualification passed",
                ),
            ),
            evidence=(
                {
                    "target_environment": environment,
                    "expected_application_build_reference": build_reference,
                }
                if result_type == "LOAD"
                else {
                    "source_environment": environment,
                    "restore_environment": f"{environment}-isolated-restore",
                    "application_build_reference": build_reference,
                }
            ),
        )
        result_bytes = result.model_dump_json(indent=2).encode()
        result_object = object_store.put(BytesIO(result_bytes))
        package = service.submit_package(
            actor=submitter,
            submission=ProductionGateEvidenceSubmission(
                profile_version_id=profile_id,
                profile_content_hash=profile_hash,
                environment=environment,
                executed_by=f"{label_prefix}-{result_type.lower()}-runner",
                started_at=started_at,
                completed_at=completed_at,
                artifacts=(
                    ProductionEvidenceArtifact(
                        category="QUALIFICATION_RESULT",
                        object_hash=result_object.object_hash,
                        size_bytes=result_object.size_bytes,
                        media_type="application/json",
                        source_reference=f"runner-output:{result.result_hash}",
                    ),
                ),
                claims={"SCOPE": "FULL_APPROVED_SCOPE"},
                technical_result=result,
            ),
            request_id=f"request-submit-{label_prefix}-{gate_name}",
            reason=f"Register exact {gate_name} technical result",
        )
        bindings[gate_name] = _approve_and_bind(
            service=service,
            package_id=package.package_id,
            gate_name=gate_name,
            owner_id=profile_creator.actor_id,
            reviewer=reviewer,
            environment=environment,
            request_id=f"request-review-{label_prefix}-{gate_name}",
        )
    return bindings


def _approved_version(
    *,
    governance: GovernanceService,
    creator: Actor,
    approver: Actor,
    kind: str,
    label: str,
    payload: dict[str, Any],
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


def _base_profile(
    *,
    build_reference: str,
    gate_name: str,
    mode: str,
    environment: str,
) -> dict[str, Any]:
    return {
        "schema_version": "tenderguard.production-gate-evidence-profile/v1",
        "gate_name": gate_name,
        "expected_application_build_reference": build_reference,
        "allowed_environments": [environment],
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


def _load_profile(build_reference: str, environment: str) -> dict[str, Any]:
    slo = {
        "minimum_success_ratio": "0.99",
        "maximum_p95_ms": "1000",
        "maximum_p99_ms": "2000",
        "minimum_requests_per_second": "1",
        "minimum_completed_requests": 1,
    }
    return {
        "schema_version": "tenderguard.load-profile/v1",
        "target_environment": environment,
        "expected_application_build_reference": build_reference,
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
                "slo": slo,
            }
        ],
        "overall_slo": slo,
    }


def _recovery_profile(build_reference: str, environment: str) -> dict[str, Any]:
    return {
        "schema_version": "tenderguard.recovery-profile/v1",
        "source_environment": environment,
        "restore_environment": f"{environment}-isolated-restore",
        "expected_application_build_reference": build_reference,
        "maximum_rpo_seconds": 3600,
        "maximum_rto_seconds": 7200,
        "require_worm": False,
        "require_external_audit_anchor": False,
        "require_oidc_configuration": False,
        "require_export_signing_configuration": False,
        "require_integration_signing_configuration": False,
        "required_adapter_qualification_ids": ["adapter-qualification-1"],
        "required_golden_snapshot_ids": ["snapshot-golden-1"],
    }


def _approve_and_bind(
    *,
    service: ProductionGateEvidenceService,
    package_id: str,
    gate_name: str,
    owner_id: str,
    reviewer: Actor,
    environment: str,
    request_id: str,
) -> dict[str, object]:
    approved = service.review_package(
        actor=reviewer,
        package_id=package_id,
        command=ProductionGateEvidenceReviewCommand(
            decision="APPROVED",
            reason=f"Independently approve {gate_name} evidence",
        ),
        request_id=request_id,
    )
    if approved.approval_hash is None or approved.reviewed_at is None:
        raise AssertionError("Test production evidence did not produce an approval")
    return {
        "status": "PASSED",
        "evidence_hash": approved.approval_hash,
        "evidence_package_id": approved.package_id,
        "source_reference": f"production_gate_evidence_package:{approved.package_id}",
        "owner_id": owner_id,
        "approved_by": reviewer.actor_id,
        "approved_at": approved.reviewed_at.isoformat(),
        "environment": environment,
    }
