from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from tenderguard.application.operational_qualification import load_approved_profile
from tenderguard.application.projects import ProjectService
from tenderguard.config import Settings
from tenderguard.domain.audit import verify_chain
from tenderguard.domain.common import content_hash, ensure_utc, utc_now
from tenderguard.domain.enums import ActorRole
from tenderguard.domain.models import DomainModel
from tenderguard.domain.operational_qualification import (
    LoadProfile,
    QualificationResultEnvelope,
    RecoveryProfile,
)
from tenderguard.domain.production_qualification import (
    ProductionGateEvidenceProfile,
    ProductionGateEvidenceStatement,
    ProductionGateEvidenceSubmission,
    verify_production_evidence_signature,
)
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.object_store import ObjectStore
from tenderguard.infrastructure.orm import (
    AuditEventRow,
    ProductionGateEvidenceApprovalRow,
    ProductionGateEvidencePackageRow,
    ProductionGateEvidenceRevocationRow,
)

PRODUCTION_GATE_EVIDENCE_PROFILE_KIND = "production_gate_evidence_profile"


class ProductionGateEvidencePackageView(DomainModel):
    package_id: str
    gate_name: str
    profile_version_id: str
    profile_content_hash: str
    application_build_reference: str
    environment: str
    evidence_mode: str
    package_hash: str
    submitted_by: str
    submitted_at: datetime
    decision: str | None
    approval_hash: str | None
    reviewed_by: str | None
    reviewed_at: datetime | None
    revoked: bool


class ProductionGateEvidencePackageDetail(DomainModel):
    package: ProductionGateEvidencePackageView
    statement: ProductionGateEvidenceStatement
    technical_result: QualificationResultEnvelope | None


class ProductionGateEvidenceReviewCommand(DomainModel):
    decision: Literal["APPROVED", "REJECTED"]
    reason: str = Field(min_length=1, max_length=4000)


class ProductionGateEvidenceRevocationCommand(DomainModel):
    reason: str = Field(min_length=1, max_length=4000)


class ProductionGateEvidenceService:
    def __init__(
        self,
        *,
        session: Session,
        settings: Settings,
        object_store: ObjectStore,
    ) -> None:
        self.session = session
        self.settings = settings
        self.object_store = object_store
        self.projects = ProjectService(
            session=session,
            settings=settings,
            object_store=object_store,
        )

    def submit_package(
        self,
        *,
        actor: Actor,
        submission: ProductionGateEvidenceSubmission,
        request_id: str,
        reason: str,
    ) -> ProductionGateEvidencePackageView:
        actor.require_any(
            ActorRole.AUDITOR,
            ActorRole.METHODOLOGY_OWNER,
            ActorRole.ADMIN,
        )
        reason = self._required_text(reason, "reason", 4000)
        profile, profile_row = self._profile(
            organization_id=actor.organization_id,
            version_id=submission.profile_version_id,
            content_hash_value=submission.profile_content_hash,
        )
        build_reference = self.settings.application_build_reference
        if (
            build_reference is None
            or build_reference != profile.expected_application_build_reference
        ):
            raise ValueError("Evidence profile does not match the running application build")
        now = utc_now()
        started_at = self._required_utc(submission.started_at, "started_at")
        completed_at = self._required_utc(submission.completed_at, "completed_at")
        if started_at > completed_at or completed_at > now:
            raise ValueError("Evidence timestamps are inconsistent or in the future")
        if completed_at + timedelta(days=profile.maximum_evidence_age_days) < now:
            raise ValueError("Evidence is already stale under its approved profile")
        if submission.environment not in profile.allowed_environments:
            raise ValueError("Evidence environment is not allowed by the profile")
        self._validate_submission_shape(profile, submission)
        technical_result_hash = (
            submission.technical_result.result_hash
            if submission.technical_result is not None
            else None
        )
        statement = ProductionGateEvidenceStatement(
            schema_version="tenderguard.production-gate-evidence/v1",
            organization_id=actor.organization_id,
            gate_name=profile.gate_name,
            profile_version_id=profile_row.id,
            profile_content_hash=profile_row.content_hash,
            application_build_reference=build_reference,
            environment=submission.environment,
            executed_by=submission.executed_by,
            started_at=started_at,
            completed_at=completed_at,
            artifacts=tuple(
                sorted(
                    submission.artifacts,
                    key=lambda item: (item.category, item.object_hash),
                )
            ),
            claims=dict(sorted(submission.claims.items())),
            technical_result_hash=technical_result_hash,
        )
        self._validate_package_basis(
            profile=profile,
            statement=statement,
            technical_result=submission.technical_result,
            attester_id=submission.attester_id,
            attester_key_id=submission.attester_key_id,
            signature_b64=submission.attestation_signature_b64,
            signed_statement_hash=submission.signed_statement_hash,
            now=now,
        )
        package_hash = statement.statement_hash
        if self.session.scalar(
            select(ProductionGateEvidencePackageRow.id).where(
                ProductionGateEvidencePackageRow.package_hash == package_hash
            )
        ):
            raise ValueError("This exact production gate evidence package is already registered")
        package_id = f"qualification-evidence-{package_hash[:40]}"
        row = ProductionGateEvidencePackageRow(
            id=package_id,
            organization_id=actor.organization_id,
            gate_name=profile.gate_name,
            profile_version_id=profile_row.id,
            profile_content_hash=profile_row.content_hash,
            application_build_reference=build_reference,
            environment=submission.environment,
            evidence_mode=profile.evidence_mode,
            package_hash=package_hash,
            statement_payload=statement.model_dump(mode="json"),
            technical_result_payload=(
                submission.technical_result.model_dump(mode="json")
                if submission.technical_result is not None
                else None
            ),
            attester_id=submission.attester_id,
            attester_key_id=submission.attester_key_id,
            attestation_signature_b64=submission.attestation_signature_b64,
            submitted_by=actor.actor_id,
            submitted_at=now,
        )
        self.session.add(row)
        self.session.flush()
        self._audit(
            row=row,
            actor=actor,
            request_id=request_id,
            reason=reason,
            event_type="production_gate_evidence_submitted",
            payload={
                "gate_name": row.gate_name,
                "profile_version_id": row.profile_version_id,
                "profile_content_hash": row.profile_content_hash,
                "package_hash": row.package_hash,
                "environment": row.environment,
                "artifact_hashes": [artifact.object_hash for artifact in statement.artifacts],
            },
        )
        return self._view(row)

    def review_package(
        self,
        *,
        actor: Actor,
        package_id: str,
        command: ProductionGateEvidenceReviewCommand,
        request_id: str,
    ) -> ProductionGateEvidencePackageView:
        row = self._package(
            organization_id=actor.organization_id,
            package_id=package_id,
        )
        profile, _ = self._profile(
            organization_id=actor.organization_id,
            version_id=row.profile_version_id,
            content_hash_value=row.profile_content_hash,
        )
        actor.require_any(*profile.approval_roles)
        reason = self._required_text(command.reason, "reason", 4000)
        if actor.actor_id in {row.submitted_by}:
            raise ValueError("Production evidence requires four-eyes review")
        statement = ProductionGateEvidenceStatement.model_validate(row.statement_payload)
        if actor.actor_id == statement.executed_by:
            raise ValueError("Evidence executor cannot approve their own package")
        if self.session.scalar(
            select(ProductionGateEvidenceApprovalRow.id).where(
                ProductionGateEvidenceApprovalRow.package_id == row.id
            )
        ):
            raise ValueError("Production evidence package already has an immutable review")
        now = utc_now()
        if command.decision == "APPROVED":
            self._revalidate_row_basis(row, profile=profile, now=now)
        approval_hash = content_hash(
            {
                "package_id": row.id,
                "package_hash": row.package_hash,
                "decision": command.decision,
                "reason": reason,
                "reviewed_by": actor.actor_id,
                "reviewed_at": now,
            }
        )
        approval = ProductionGateEvidenceApprovalRow(
            id=f"qualification-evidence-approval-{uuid4()}",
            package_id=row.id,
            decision=command.decision,
            approval_hash=approval_hash,
            reason=reason,
            reviewed_by=actor.actor_id,
            reviewed_at=now,
        )
        self.session.add(approval)
        self.session.flush()
        self._audit(
            row=row,
            actor=actor,
            request_id=request_id,
            reason=reason,
            event_type="production_gate_evidence_reviewed",
            payload={
                "decision": approval.decision,
                "package_hash": row.package_hash,
                "approval_hash": approval.approval_hash,
            },
        )
        return self._view(row, approval=approval)

    def revoke_package(
        self,
        *,
        actor: Actor,
        package_id: str,
        command: ProductionGateEvidenceRevocationCommand,
        request_id: str,
    ) -> ProductionGateEvidencePackageView:
        actor.require_any(
            ActorRole.AUDITOR,
            ActorRole.METHODOLOGY_OWNER,
            ActorRole.ADMIN,
        )
        reason = self._required_text(command.reason, "reason", 4000)
        row = self._package(
            organization_id=actor.organization_id,
            package_id=package_id,
        )
        approval = self.session.scalar(
            select(ProductionGateEvidenceApprovalRow).where(
                ProductionGateEvidenceApprovalRow.package_id == row.id,
                ProductionGateEvidenceApprovalRow.decision == "APPROVED",
            )
        )
        if approval is None:
            raise ValueError("Only approved production evidence can be revoked")
        if self.session.scalar(
            select(ProductionGateEvidenceRevocationRow.id).where(
                ProductionGateEvidenceRevocationRow.package_id == row.id
            )
        ):
            raise ValueError("Production evidence package is already revoked")
        revocation = ProductionGateEvidenceRevocationRow(
            id=f"qualification-evidence-revocation-{uuid4()}",
            package_id=row.id,
            reason=reason,
            revoked_by=actor.actor_id,
            revoked_at=utc_now(),
        )
        self.session.add(revocation)
        self.session.flush()
        self._audit(
            row=row,
            actor=actor,
            request_id=request_id,
            reason=reason,
            event_type="production_gate_evidence_revoked",
            payload={
                "package_hash": row.package_hash,
                "approval_hash": approval.approval_hash,
                "revocation_id": revocation.id,
            },
        )
        return self._view(row, approval=approval, revoked=True)

    def get_package(
        self,
        *,
        actor: Actor,
        package_id: str,
    ) -> ProductionGateEvidencePackageDetail:
        actor.require_any(
            ActorRole.AUDITOR,
            ActorRole.METHODOLOGY_OWNER,
            ActorRole.ADMIN,
        )
        row = self._package(
            organization_id=actor.organization_id,
            package_id=package_id,
        )
        approval = self.session.scalar(
            select(ProductionGateEvidenceApprovalRow).where(
                ProductionGateEvidenceApprovalRow.package_id == row.id
            )
        )
        revoked = (
            self.session.scalar(
                select(ProductionGateEvidenceRevocationRow.id).where(
                    ProductionGateEvidenceRevocationRow.package_id == row.id
                )
            )
            is not None
        )
        return ProductionGateEvidencePackageDetail(
            package=self._view(row, approval=approval, revoked=revoked),
            statement=ProductionGateEvidenceStatement.model_validate(row.statement_payload),
            technical_result=(
                QualificationResultEnvelope.model_validate(row.technical_result_payload)
                if row.technical_result_payload is not None
                else None
            ),
        )

    def gate_binding_valid(
        self,
        gate: dict[str, Any],
        *,
        organization_id: str,
        expected_gate_name: str,
    ) -> bool:
        package_id = gate.get("evidence_package_id")
        if not isinstance(package_id, str) or not package_id:
            return False
        try:
            row = self.session.scalar(
                select(ProductionGateEvidencePackageRow).where(
                    ProductionGateEvidencePackageRow.id == package_id,
                    ProductionGateEvidencePackageRow.organization_id == organization_id,
                    ProductionGateEvidencePackageRow.gate_name == expected_gate_name,
                )
            )
            if row is None:
                return False
            profile, profile_row = self._profile(
                organization_id=organization_id,
                version_id=row.profile_version_id,
                content_hash_value=row.profile_content_hash,
            )
            approval = self._approved_review(row.id)
            if approval is None:
                return False
            self._revalidate_row_basis(row, profile=profile, now=utc_now())
            statement = ProductionGateEvidenceStatement.model_validate(row.statement_payload)
            if approval.reviewed_by in {
                row.submitted_by,
                statement.executed_by,
            } or not self._row_audit_valid(row, approval, profile):
                return False
            reviewed_at = self._required_utc(approval.reviewed_at, "reviewed_at")
            governance = profile_row.payload.get("_governance")
            if not isinstance(governance, dict):
                return False
            return bool(
                gate.get("status") == "PASSED"
                and gate.get("evidence_hash") == approval.approval_hash
                and gate.get("source_reference") == f"production_gate_evidence_package:{row.id}"
                and gate.get("owner_id") == governance.get("created_by")
                and gate.get("approved_by") == approval.reviewed_by
                and self._parse_timestamp(gate.get("approved_at")) == reviewed_at
                and gate.get("environment") == row.environment
            )
        except (
            ArithmeticError,
            json.JSONDecodeError,
            KeyError,
            LookupError,
            OSError,
            RuntimeError,
            TypeError,
            UnicodeError,
            ValueError,
        ):
            return False

    def _revalidate_row_basis(
        self,
        row: ProductionGateEvidencePackageRow,
        *,
        profile: ProductionGateEvidenceProfile,
        now: datetime,
    ) -> None:
        statement = ProductionGateEvidenceStatement.model_validate(row.statement_payload)
        technical_result = (
            QualificationResultEnvelope.model_validate(row.technical_result_payload)
            if row.technical_result_payload is not None
            else None
        )
        if (
            statement.statement_hash != row.package_hash
            or statement.organization_id != row.organization_id
            or statement.gate_name != row.gate_name
            or statement.profile_version_id != row.profile_version_id
            or statement.profile_content_hash != row.profile_content_hash
            or statement.application_build_reference != row.application_build_reference
            or statement.environment != row.environment
            or profile.gate_name != row.gate_name
            or profile.evidence_mode != row.evidence_mode
            or self.settings.application_build_reference != row.application_build_reference
        ):
            raise ValueError("Production evidence row no longer reproduces")
        self._validate_package_basis(
            profile=profile,
            statement=statement,
            technical_result=technical_result,
            attester_id=row.attester_id,
            attester_key_id=row.attester_key_id,
            signature_b64=row.attestation_signature_b64,
            signed_statement_hash=row.package_hash,
            now=now,
        )

    def _validate_package_basis(
        self,
        *,
        profile: ProductionGateEvidenceProfile,
        statement: ProductionGateEvidenceStatement,
        technical_result: QualificationResultEnvelope | None,
        attester_id: str | None,
        attester_key_id: str | None,
        signature_b64: str | None,
        signed_statement_hash: str | None,
        now: datetime,
    ) -> None:
        started_at = self._required_utc(statement.started_at, "started_at")
        completed_at = self._required_utc(statement.completed_at, "completed_at")
        current = self._required_utc(now, "validation timestamp")
        if (
            started_at > completed_at
            or completed_at > current
            or completed_at + timedelta(days=profile.maximum_evidence_age_days) < current
            or statement.environment not in profile.allowed_environments
            or statement.application_build_reference != profile.expected_application_build_reference
        ):
            raise ValueError("Production evidence time/environment/build binding is invalid")
        self._validate_artifacts(profile, statement, technical_result)
        if profile.evidence_mode == "INTERNAL_QUALIFICATION_RESULT":
            if (
                technical_result is None
                or any(
                    value is not None
                    for value in (
                        attester_id,
                        attester_key_id,
                        signature_b64,
                    )
                )
                or statement.technical_result_hash != technical_result.result_hash
            ):
                raise ValueError("Internal qualification result evidence is incomplete")
            self._validate_technical_result(
                organization_id=statement.organization_id,
                profile=profile,
                statement=statement,
                result=technical_result,
            )
            return
        if (
            technical_result is not None
            or statement.technical_result_hash is not None
            or signed_statement_hash != statement.statement_hash
            or attester_id != profile.trusted_attester_id
            or attester_key_id != profile.trusted_attester_key_id
            or signature_b64 is None
            or profile.trusted_attester_public_key_b64 is None
        ):
            raise ValueError("Externally attested production evidence is incomplete")
        verify_production_evidence_signature(
            statement=statement,
            signature_b64=signature_b64,
            trusted_public_key_b64=profile.trusted_attester_public_key_b64,
        )

    def _validate_submission_shape(
        self,
        profile: ProductionGateEvidenceProfile,
        submission: ProductionGateEvidenceSubmission,
    ) -> None:
        categories = {artifact.category for artifact in submission.artifacts}
        if (
            len(submission.artifacts) > profile.maximum_artifact_count
            or not set(profile.required_artifact_categories) <= categories
            or not categories <= set(profile.allowed_artifact_categories)
            or not set(profile.required_claim_keys) <= set(submission.claims)
            or sum(artifact.size_bytes for artifact in submission.artifacts)
            > profile.maximum_total_artifact_bytes
            or any(
                artifact.size_bytes > profile.maximum_artifact_bytes
                for artifact in submission.artifacts
            )
        ):
            raise ValueError("Evidence artifacts or claims violate the approved profile")
        if (
            profile.evidence_mode == "INTERNAL_QUALIFICATION_RESULT"
            and submission.technical_result is None
        ):
            raise ValueError("Internal evidence requires a technical qualification result")
        if (
            profile.evidence_mode == "EXTERNAL_ATTESTED_PACKAGE"
            and submission.technical_result is not None
        ):
            raise ValueError("External evidence cannot carry an internal technical result")

    def _validate_artifacts(
        self,
        profile: ProductionGateEvidenceProfile,
        statement: ProductionGateEvidenceStatement,
        technical_result: QualificationResultEnvelope | None,
    ) -> None:
        categories = {artifact.category for artifact in statement.artifacts}
        if (
            len(statement.artifacts) > profile.maximum_artifact_count
            or not set(profile.required_artifact_categories) <= categories
            or not categories <= set(profile.allowed_artifact_categories)
            or not set(profile.required_claim_keys) <= set(statement.claims)
            or sum(artifact.size_bytes for artifact in statement.artifacts)
            > profile.maximum_total_artifact_bytes
        ):
            raise ValueError("Persisted evidence artifacts violate the approved profile")
        result_payloads: list[bytes] = []
        for artifact in statement.artifacts:
            if artifact.size_bytes > profile.maximum_artifact_bytes:
                raise ValueError("Evidence artifact exceeds its approved size limit")
            capture = artifact.category == "QUALIFICATION_RESULT"
            if capture and artifact.size_bytes > self.settings.max_api_request_bytes:
                raise ValueError("Qualification result artifact exceeds the safe parsing limit")
            payload = bytearray()
            measured_size = 0
            with self.object_store.open(artifact.object_hash) as stream:
                while chunk := stream.read(1024 * 1024):
                    measured_size += len(chunk)
                    if measured_size > artifact.size_bytes:
                        raise ValueError("Evidence artifact is larger than declared")
                    if capture:
                        payload.extend(chunk)
            if measured_size != artifact.size_bytes:
                raise ValueError("Evidence artifact size does not reproduce")
            if capture:
                result_payloads.append(bytes(payload))
        if technical_result is not None:
            if len(result_payloads) != 1:
                raise ValueError("Internal evidence requires one qualification result artifact")
            artifact_result = QualificationResultEnvelope.model_validate_json(result_payloads[0])
            if artifact_result != technical_result:
                raise ValueError("Qualification result artifact differs from the registered result")

    def _validate_technical_result(
        self,
        *,
        organization_id: str,
        profile: ProductionGateEvidenceProfile,
        statement: ProductionGateEvidenceStatement,
        result: QualificationResultEnvelope,
    ) -> None:
        if (
            result.status != "TECHNICAL_VERIFICATION_PASSED"
            or not result.findings
            or any(not finding.passed for finding in result.findings)
            or result.qualification_type != profile.expected_qualification_type
            or result.profile_version_id != profile.source_profile_version_id
            or result.profile_content_hash != profile.source_profile_content_hash
            or self._required_utc(result.started_at, "result started_at")
            != self._required_utc(statement.started_at, "statement started_at")
            or self._required_utc(result.completed_at, "result completed_at")
            != self._required_utc(statement.completed_at, "statement completed_at")
        ):
            raise ValueError("Technical qualification result is not a passing exact result")
        assert profile.source_profile_version_id is not None
        assert profile.source_profile_content_hash is not None
        if result.qualification_type == "LOAD":
            load_profile, source_row = load_approved_profile(
                session=self.session,
                settings=self.settings,
                version_id=profile.source_profile_version_id,
                expected_content_hash=profile.source_profile_content_hash,
                expected_kind="load_test_profile",
                profile_type=LoadProfile,
            )
            target_environment = load_profile.target_environment
            expected_build = load_profile.expected_application_build_reference
            evidence_build = result.evidence.get("expected_application_build_reference")
            evidence_environment = result.evidence.get("target_environment")
        else:
            recovery_profile, source_row = load_approved_profile(
                session=self.session,
                settings=self.settings,
                version_id=profile.source_profile_version_id,
                expected_content_hash=profile.source_profile_content_hash,
                expected_kind="recovery_profile",
                profile_type=RecoveryProfile,
            )
            target_environment = recovery_profile.source_environment
            expected_build = recovery_profile.expected_application_build_reference
            evidence_build = result.evidence.get("application_build_reference")
            evidence_environment = result.evidence.get("source_environment")
        governance = source_row.payload.get("_governance")
        if (
            not isinstance(governance, dict)
            or governance.get("organization_id") != organization_id
            or expected_build != statement.application_build_reference
            or target_environment != statement.environment
            or evidence_build != statement.application_build_reference
            or evidence_environment != statement.environment
        ):
            raise ValueError("Technical result source profile binding does not verify")

    def _row_audit_valid(
        self,
        row: ProductionGateEvidencePackageRow,
        approval: ProductionGateEvidenceApprovalRow,
        profile: ProductionGateEvidenceProfile,
    ) -> bool:
        events = [
            ProjectService._audit_domain(event)
            for event in self.session.scalars(
                select(AuditEventRow)
                .where(
                    AuditEventRow.aggregate_type == "production_gate_evidence_package",
                    AuditEventRow.aggregate_id == row.id,
                )
                .order_by(AuditEventRow.sequence)
            )
        ]
        submitted = [
            event for event in events if event.event_type == "production_gate_evidence_submitted"
        ]
        reviewed = [
            event for event in events if event.event_type == "production_gate_evidence_reviewed"
        ]
        revoked_events = [
            event for event in events if event.event_type == "production_gate_evidence_revoked"
        ]
        revocation = self.session.scalar(
            select(ProductionGateEvidenceRevocationRow).where(
                ProductionGateEvidenceRevocationRow.package_id == row.id
            )
        )
        return bool(
            events
            and verify_chain(events, self.settings.audit_verification_keyring)
            and len(submitted) == 1
            and len(reviewed) == 1
            and not revoked_events
            and revocation is None
            and submitted[0].actor_id == row.submitted_by
            and set(submitted[0].actor_roles).intersection(
                {
                    ActorRole.AUDITOR.value,
                    ActorRole.METHODOLOGY_OWNER.value,
                    ActorRole.ADMIN.value,
                }
            )
            and submitted[0].payload.get("package_hash") == row.package_hash
            and reviewed[0].actor_id == approval.reviewed_by
            and reviewed[0].reason == approval.reason
            and reviewed[0].payload.get("decision") == approval.decision
            and reviewed[0].payload.get("approval_hash") == approval.approval_hash
            and reviewed[0].payload.get("package_hash") == row.package_hash
            and set(reviewed[0].actor_roles).intersection(
                role.value for role in profile.approval_roles
            )
        )

    def _approved_review(
        self,
        package_id: str,
    ) -> ProductionGateEvidenceApprovalRow | None:
        approval = self.session.scalar(
            select(ProductionGateEvidenceApprovalRow).where(
                ProductionGateEvidenceApprovalRow.package_id == package_id,
                ProductionGateEvidenceApprovalRow.decision == "APPROVED",
            )
        )
        if approval is None:
            return None
        reviewed_at = self._required_utc(approval.reviewed_at, "reviewed_at")
        expected_hash = content_hash(
            {
                "package_id": approval.package_id,
                "package_hash": self.session.scalar(
                    select(ProductionGateEvidencePackageRow.package_hash).where(
                        ProductionGateEvidencePackageRow.id == approval.package_id
                    )
                ),
                "decision": approval.decision,
                "reason": approval.reason,
                "reviewed_by": approval.reviewed_by,
                "reviewed_at": reviewed_at,
            }
        )
        return approval if expected_hash == approval.approval_hash else None

    def _profile(
        self,
        *,
        organization_id: str,
        version_id: str,
        content_hash_value: str,
    ) -> tuple[ProductionGateEvidenceProfile, Any]:
        profile, row = load_approved_profile(
            session=self.session,
            settings=self.settings,
            version_id=version_id,
            expected_content_hash=content_hash_value,
            expected_kind=PRODUCTION_GATE_EVIDENCE_PROFILE_KIND,
            profile_type=ProductionGateEvidenceProfile,
        )
        governance = row.payload.get("_governance")
        if not isinstance(governance, dict) or governance.get("organization_id") != organization_id:
            raise LookupError(version_id)
        return profile, row

    def _package(
        self,
        *,
        organization_id: str,
        package_id: str,
    ) -> ProductionGateEvidencePackageRow:
        row = self.session.scalar(
            select(ProductionGateEvidencePackageRow).where(
                ProductionGateEvidencePackageRow.id == package_id,
                ProductionGateEvidencePackageRow.organization_id == organization_id,
            )
        )
        if row is None:
            raise LookupError(package_id)
        return row

    def _audit(
        self,
        *,
        row: ProductionGateEvidencePackageRow,
        actor: Actor,
        request_id: str,
        reason: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        self.projects.record_event(
            aggregate_type="production_gate_evidence_package",
            aggregate_id=row.id,
            event_type=event_type,
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload=payload,
        )

    def _view(
        self,
        row: ProductionGateEvidencePackageRow,
        *,
        approval: ProductionGateEvidenceApprovalRow | None = None,
        revoked: bool | None = None,
    ) -> ProductionGateEvidencePackageView:
        if approval is None:
            approval = self.session.scalar(
                select(ProductionGateEvidenceApprovalRow).where(
                    ProductionGateEvidenceApprovalRow.package_id == row.id
                )
            )
        if revoked is None:
            revoked = (
                self.session.scalar(
                    select(ProductionGateEvidenceRevocationRow.id).where(
                        ProductionGateEvidenceRevocationRow.package_id == row.id
                    )
                )
                is not None
            )
        return ProductionGateEvidencePackageView(
            package_id=row.id,
            gate_name=row.gate_name,
            profile_version_id=row.profile_version_id,
            profile_content_hash=row.profile_content_hash,
            application_build_reference=row.application_build_reference,
            environment=row.environment,
            evidence_mode=row.evidence_mode,
            package_hash=row.package_hash,
            submitted_by=row.submitted_by,
            submitted_at=self._required_utc(row.submitted_at, "submitted_at"),
            decision=approval.decision if approval else None,
            approval_hash=approval.approval_hash if approval else None,
            reviewed_by=approval.reviewed_by if approval else None,
            reviewed_at=(
                self._required_utc(approval.reviewed_at, "reviewed_at") if approval else None
            ),
            revoked=bool(revoked),
        )

    @staticmethod
    def _required_text(value: str, field: str, maximum: int) -> str:
        if not value or value != value.strip() or len(value) > maximum:
            raise ValueError(f"{field} is required, normalized, and at most {maximum} characters")
        return value

    @staticmethod
    def _required_utc(value: datetime | None, field: str) -> datetime:
        normalized = ensure_utc(value)
        if normalized is None:
            raise ValueError(f"{field} is missing")
        return normalized

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            return ensure_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
