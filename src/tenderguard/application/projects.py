from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Select, and_, func, select
from sqlalchemy.orm import Session

from tenderguard.application.operational_qualification import load_approved_profile
from tenderguard.application.snapshot_integrity import read_verified_snapshot
from tenderguard.application.stage_gates import (
    boq_stage_blockers,
    contract_stage_blockers,
    passport_stage_blockers,
    pricing_stage_blockers,
    risk_stage_blockers,
    scope_stage_blockers,
)
from tenderguard.config import Settings
from tenderguard.domain.access import project_role_mask, validate_project_role_evidence
from tenderguard.domain.audit import AuditEvent, append_event, verify_chain
from tenderguard.domain.business_qualification import (
    BusinessQualificationDataset,
    BusinessQualificationEvaluation,
    BusinessQualificationProfile,
    QualificationReferencePayload,
)
from tenderguard.domain.common import canonical_data, content_hash, ensure_utc, utc_now
from tenderguard.domain.enums import (
    ActorRole,
    ApprovalState,
    MatchClass,
    PriceStatus,
    ProjectAccessLevel,
    ProjectMembershipStatus,
    Severity,
    VerificationStatus,
    VersionStatus,
)
from tenderguard.domain.intake import IntakeManifest
from tenderguard.domain.models import (
    CalculationSnapshot,
    ControlledVersion,
    GateDecision,
    IndependentValidationResult,
    WorkflowTransition,
)
from tenderguard.domain.release import (
    FourEyesRecord,
    ReleaseContext,
    evaluate_bid_release,
    evaluate_internal_release,
)
from tenderguard.domain.workflow import validate_transition
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.object_store import ObjectStore, StoredObject
from tenderguard.infrastructure.orm import (
    AdapterQualificationRow,
    ApprovalRecordRow,
    ApprovalTaskRow,
    AuditEventRow,
    BoqLineRow,
    BusinessQualificationApprovalRow,
    BusinessQualificationCampaignRow,
    BusinessQualificationCaseRow,
    BusinessQualificationDiscrepancyReviewRow,
    BusinessQualificationDiscrepancyRow,
    BusinessQualificationEvaluationRow,
    BusinessQualificationReferenceRow,
    CalculationRunRow,
    CalculationSnapshotRow,
    ConflictRow,
    ContractTermRow,
    ControlledVersionRow,
    CostInputRow,
    DocumentRevisionRow,
    DocumentRow,
    DocumentSetRevisionRow,
    FileManifestRow,
    ManualChangeRow,
    NomenclatureMatchRow,
    NormativeCalculationRow,
    ObservationRow,
    OutboxEventRow,
    PriceDecisionRow,
    ProjectControlledVersionRow,
    ProjectMembershipRow,
    ProjectPassportFactRow,
    ProjectRow,
    QuantityManualChangeApplicationRow,
    QuantityRow,
    QuarantinedUploadRow,
    ReleaseDecisionRow,
    RiskCalculationRow,
    RiskItemRow,
    ScopeEvaluationRow,
    ScopeFindingRow,
    VerificationFindingRow,
    WorkflowTransitionRow,
)


class ApplicationModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ProjectView(ApplicationModel):
    id: str
    organization_id: str
    code: str
    name: str
    state: ApprovalState
    row_version: int
    current_document_set_revision_id: str | None


class ProjectMembershipView(ApplicationModel):
    membership_revision_id: str
    project_id: str
    principal_id: str
    roles: tuple[ActorRole, ...]
    access_level: ProjectAccessLevel
    status: ProjectMembershipStatus
    version: int
    supersedes_membership_id: str | None
    changed_by: str
    reason: str
    created_at: datetime


class DocumentSetView(ApplicationModel):
    id: str
    project_id: str
    manifest_hash: str
    revision_ids: tuple[str, ...]
    status: str
    created_by: str
    created_at: datetime
    confirmed_by: str | None
    confirmed_at: datetime | None


@dataclass(frozen=True)
class SystemProjectAccess:
    qualification_id: str
    capability: str


class DocumentUploadResult(ApplicationModel):
    document_id: str
    document_revision_id: str
    candidate_document_set_revision_id: str
    manifest: IntakeManifest
    project_state: ApprovalState


class ProjectNotFoundError(LookupError):
    pass


class OptimisticLockError(RuntimeError):
    pass


class ProjectService:
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

    def create_project(
        self,
        *,
        actor: Actor,
        code: str,
        name: str,
        request_id: str,
        reason: str,
    ) -> ProjectView:
        actor.require_any(ActorRole.ESTIMATOR, ActorRole.ADMIN)
        if ActorRole.SYSTEM in actor.roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="SYSTEM identities cannot create projects",
            )
        code = self._required_text(code, "code", 128)
        name = self._required_text(name, "name", 500)
        reason = self._required_text(reason, "reason", 2000)
        if any(character.isspace() for character in code):
            raise ValueError("Project code must not contain whitespace")
        now = utc_now()
        project = ProjectRow(
            id=f"project-{uuid4()}",
            organization_id=actor.organization_id,
            code=code,
            name=name,
            state=ApprovalState.DRAFT.value,
            row_version=1,
            created_at=now,
            updated_at=now,
        )
        self.session.add(project)
        self.session.flush()
        owner_membership = self._append_membership(
            project=project,
            principal_id=actor.actor_id,
            roles=tuple(
                sorted(
                    (role for role in actor.roles if role is not ActorRole.SYSTEM),
                    key=lambda role: role.value,
                )
            ),
            access_level=ProjectAccessLevel.OWNER,
            status_value=ProjectMembershipStatus.ACTIVE,
            changed_by=actor.actor_id,
            reason=reason,
            previous=None,
            now=now,
        )
        self._audit(
            aggregate_type="project",
            aggregate_id=project.id,
            event_type="project_created",
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload={
                "code": code,
                "name": name,
                "state": ApprovalState.DRAFT,
                "owner_membership_revision_id": owner_membership.id,
            },
        )
        self._audit(
            aggregate_type="project",
            aggregate_id=project.id,
            event_type="project_membership_bootstrapped",
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload={
                "membership_revision_id": owner_membership.id,
                "principal_id": actor.actor_id,
                "roles": owner_membership.roles,
                "access_level": owner_membership.access_level,
                "version": owner_membership.version,
            },
        )
        self._outbox(
            topic="project.created",
            aggregate_id=project.id,
            payload={"project_id": project.id},
        )
        return self._view(project)

    def record_event(
        self,
        *,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        actor: Actor,
        request_id: str,
        reason: str,
        payload: dict[str, Any],
    ) -> AuditEvent:
        return self._audit(
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_type=event_type,
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload=payload,
        )

    def enqueue_event(
        self,
        *,
        topic: str,
        aggregate_id: str,
        payload: dict[str, Any],
    ) -> None:
        self._outbox(topic=topic, aggregate_id=aggregate_id, payload=payload)

    def get_project(
        self,
        *,
        actor: Actor,
        project_id: str,
        lock: bool = False,
        required_roles: tuple[ActorRole, ...] | None = None,
        require_owner: bool = False,
        system_access: SystemProjectAccess | None = None,
    ) -> ProjectRow:
        is_system = ActorRole.SYSTEM in actor.roles
        if is_system:
            if actor.roles != frozenset({ActorRole.SYSTEM}) or system_access is None:
                raise ProjectNotFoundError(project_id)
        else:
            if system_access is not None:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Service access can only be used by a SYSTEM actor",
                )
            if required_roles:
                actor.require_any(*required_roles)
        query: Select[tuple[ProjectRow]] = select(ProjectRow).where(
            ProjectRow.id == project_id,
            ProjectRow.organization_id == actor.organization_id,
        )
        if lock:
            query = query.with_for_update()
        project = self.session.scalar(query)
        if project is None:
            raise ProjectNotFoundError(project_id)
        if system_access is not None:
            self._require_system_access(actor=actor, access=system_access)
            return project
        membership = self._current_membership(
            project_id=project.id,
            principal_id=actor.actor_id,
        )
        if membership is None or membership.status != ProjectMembershipStatus.ACTIVE.value:
            raise ProjectNotFoundError(project_id)
        scoped_roles = self._membership_roles(membership)
        if required_roles:
            effective_roles = actor.roles.intersection(scoped_roles, required_roles)
        else:
            effective_roles = actor.roles.intersection(scoped_roles)
        if not effective_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Required project role is missing",
            )
        if require_owner and membership.access_level != ProjectAccessLevel.OWNER.value:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Project owner access is required",
            )
        return project

    def project_view(self, *, actor: Actor, project_id: str) -> ProjectView:
        return self._view(self.get_project(actor=actor, project_id=project_id))

    def grant_project_membership(
        self,
        *,
        actor: Actor,
        project_id: str,
        principal_id: str,
        roles: tuple[ActorRole, ...],
        access_level: ProjectAccessLevel,
        request_id: str,
        reason: str,
    ) -> ProjectMembershipView:
        principal_id = self._required_text(principal_id, "principal_id", 128)
        reason = self._required_text(reason, "reason", 2000)
        normalized_roles = tuple(sorted(set(roles), key=lambda role: role.value))
        if not normalized_roles:
            raise ValueError("At least one project role is required")
        if ActorRole.SYSTEM in normalized_roles:
            raise ValueError(
                "SYSTEM identities use qualified service access, not project membership"
            )
        if (
            principal_id == actor.actor_id
            and access_level is ProjectAccessLevel.OWNER
            and not actor.roles.intersection(normalized_roles)
        ):
            raise ValueError("An owner cannot remove every currently usable project role")
        project = self.get_project(
            actor=actor,
            project_id=project_id,
            lock=True,
            require_owner=True,
        )
        previous = self._current_membership(
            project_id=project.id,
            principal_id=principal_id,
        )
        if (
            previous is not None
            and previous.status == ProjectMembershipStatus.ACTIVE.value
            and previous.access_level == access_level.value
            and previous.roles == [role.value for role in normalized_roles]
        ):
            return self._membership_view(previous)
        if (
            previous is not None
            and previous.status == ProjectMembershipStatus.ACTIVE.value
            and previous.access_level == ProjectAccessLevel.OWNER.value
            and access_level is not ProjectAccessLevel.OWNER
        ):
            self._require_another_active_owner(project.id, excluding=principal_id)
        row = self._append_membership(
            project=project,
            principal_id=principal_id,
            roles=normalized_roles,
            access_level=access_level,
            status_value=ProjectMembershipStatus.ACTIVE,
            changed_by=actor.actor_id,
            reason=reason,
            previous=previous,
            now=utc_now(),
        )
        self._record_membership_change(
            actor=actor,
            project_id=project.id,
            row=row,
            request_id=request_id,
            reason=reason,
            event_type="project_membership_granted",
        )
        return self._membership_view(row)

    def revoke_project_membership(
        self,
        *,
        actor: Actor,
        project_id: str,
        principal_id: str,
        request_id: str,
        reason: str,
    ) -> ProjectMembershipView:
        principal_id = self._required_text(principal_id, "principal_id", 128)
        reason = self._required_text(reason, "reason", 2000)
        project = self.get_project(
            actor=actor,
            project_id=project_id,
            lock=True,
            require_owner=True,
        )
        previous = self._current_membership(
            project_id=project.id,
            principal_id=principal_id,
        )
        if previous is None or previous.status != ProjectMembershipStatus.ACTIVE.value:
            raise LookupError(principal_id)
        if previous.access_level == ProjectAccessLevel.OWNER.value:
            self._require_another_active_owner(project.id, excluding=principal_id)
        row = self._append_membership(
            project=project,
            principal_id=principal_id,
            roles=self._membership_roles(previous),
            access_level=ProjectAccessLevel(previous.access_level),
            status_value=ProjectMembershipStatus.REVOKED,
            changed_by=actor.actor_id,
            reason=reason,
            previous=previous,
            now=utc_now(),
        )
        self._record_membership_change(
            actor=actor,
            project_id=project.id,
            row=row,
            request_id=request_id,
            reason=reason,
            event_type="project_membership_revoked",
        )
        return self._membership_view(row)

    def list_project_memberships(
        self,
        *,
        actor: Actor,
        project_id: str,
    ) -> tuple[ProjectMembershipView, ...]:
        project = self.get_project(
            actor=actor,
            project_id=project_id,
            require_owner=True,
        )
        current = self._current_memberships(project.id)
        return tuple(
            self._membership_view(row)
            for row in sorted(current, key=lambda item: item.principal_id)
            if row.status == ProjectMembershipStatus.ACTIVE.value
        )

    def register_scanned_document_revision(
        self,
        *,
        actor: Actor,
        project_id: str,
        logical_key: str,
        title: str,
        document_type: str,
        critical: bool,
        revision_label: str,
        filename: str,
        media_type: str,
        stored: StoredObject,
        manifest: IntakeManifest,
        member_objects: dict[str, str],
        quarantine_upload_id: str,
        submitted_by: str,
        request_id: str,
        reason: str,
        make_candidate_current: bool = True,
        invalidated_document_set_revision_id: str | None = None,
    ) -> DocumentUploadResult:
        actor.require_any(ActorRole.SYSTEM)
        project = self.get_project(
            actor=actor,
            project_id=project_id,
            lock=True,
            system_access=SystemProjectAccess(
                qualification_id=self.settings.document_processor_qualification_id or "",
                capability="DOCUMENT_INTAKE",
            ),
        )
        if ApprovalState(project.state) in {
            ApprovalState.APPROVED_FOR_BID,
            ApprovalState.SUPERSEDED,
            ApprovalState.ARCHIVED,
        }:
            raise ValueError("Released/superseded/archived projects are immutable")
        quarantine = self.session.scalar(
            select(QuarantinedUploadRow).where(
                QuarantinedUploadRow.id == quarantine_upload_id,
                QuarantinedUploadRow.project_id == project.id,
            )
        )
        if quarantine is None:
            raise LookupError(quarantine_upload_id)
        if quarantine.object_hash != stored.object_hash:
            raise RuntimeError("Promoted object differs from quarantined object")
        if manifest.root_sha256 != stored.object_hash:
            raise RuntimeError("Object-store hash differs from intake hash")
        invalidated_document_set_id = (
            invalidated_document_set_revision_id if make_candidate_current else None
        )

        now = utc_now()
        document = self.session.scalar(
            select(DocumentRow).where(
                DocumentRow.project_id == project.id,
                DocumentRow.logical_key == logical_key,
            )
        )
        if document is None:
            document = DocumentRow(
                id=f"document-{uuid4()}",
                project_id=project.id,
                logical_key=logical_key,
                title=title,
                document_type=document_type,
                critical=critical,
                cancelled=False,
                created_at=now,
                updated_at=now,
            )
            self.session.add(document)
            self.session.flush()
        else:
            document.title = title
            document.document_type = document_type
            document.critical = critical
            document.updated_at = now

        previous_current = self.session.scalar(
            select(DocumentRevisionRow).where(
                DocumentRevisionRow.document_id == document.id,
                DocumentRevisionRow.is_current.is_(True),
            )
        )
        revision = DocumentRevisionRow(
            id=f"document-revision-{uuid4()}",
            document_id=document.id,
            revision_label=revision_label,
            object_hash=stored.object_hash,
            object_key=stored.object_key,
            original_filename=filename,
            media_type=media_type,
            size_bytes=stored.size_bytes,
            supersedes_revision_id=previous_current.id if previous_current else None,
            is_current=make_candidate_current,
            corrupt=not manifest.all_files_processed
            and any(item.corrupt for item in manifest.entries),
            protected=any(item.protected for item in manifest.entries),
            inspection_payload=manifest.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
        )
        if previous_current and make_candidate_current:
            previous_current.is_current = False
            previous_current.updated_at = now
        self.session.add(revision)
        self.session.flush()

        for finding in manifest.findings:
            finding_identity = {
                "document_revision_id": revision.id,
                "code": finding.code,
                "archive_path": finding.archive_path,
                "message": finding.message,
            }
            self.session.add(
                VerificationFindingRow(
                    id=f"finding-{content_hash(finding_identity)[:24]}",
                    project_id=project.id,
                    contour="INPUT_INTEGRITY",
                    code=finding.code,
                    severity=finding.severity.value,
                    resolved=False,
                    payload=finding.model_dump(mode="json"),
                    created_at=now,
                    updated_at=now,
                )
            )

        for entry in manifest.entries:
            expected_hash = member_objects.get(entry.archive_path, entry.sha256)
            if expected_hash != entry.sha256 and entry.sha256 != "0" * 64:
                raise RuntimeError("Stored archive member hash differs from manifest")
            self.session.add(
                FileManifestRow(
                    id=entry.entry_id,
                    document_revision_id=revision.id,
                    archive_path=entry.archive_path,
                    object_hash=entry.sha256,
                    size_bytes=entry.size_bytes,
                    media_type=entry.media_type,
                    corrupt=entry.corrupt,
                    protected=entry.protected,
                    nested_archive=entry.nested_archive,
                    inspection_payload=entry.model_dump(mode="json"),
                )
            )

        invalidated_counts: dict[str, int] = {}
        if invalidated_document_set_id is not None:
            invalidated_counts = self._invalidate_derived_records(
                project_id=project.id,
                document_set_revision_id=invalidated_document_set_id,
                replacement_document_revision_id=revision.id,
                invalidated_at=now,
            )
        candidate_id = self._create_document_set_candidate(project, submitted_by, now)
        if make_candidate_current:
            project.current_document_set_revision_id = None
        if (
            invalidated_document_set_id is not None
            and ApprovalState(project.state) is not ApprovalState.BLOCKED
        ):
            self._change_state(
                project=project,
                to_state=ApprovalState.BLOCKED,
                actor=actor,
                request_id=request_id,
                reason="A new current document revision invalidated derived project data",
            )
        elif not manifest.all_files_processed and ApprovalState(project.state) in {
            ApprovalState.DRAFT,
            ApprovalState.EXTRACTION_IN_PROGRESS,
        }:
            self._change_state(
                project=project,
                to_state=ApprovalState.DOCUMENTS_INCOMPLETE,
                actor=actor,
                request_id=request_id,
                reason="Input integrity checks produced blocking findings",
            )
        elif not (
            not manifest.all_files_processed
            and ApprovalState(project.state) is ApprovalState.DOCUMENTS_INCOMPLETE
        ):
            project.row_version += 1
            project.updated_at = now
        self._audit(
            aggregate_type="project",
            aggregate_id=project.id,
            event_type="document_revision_registered_after_scan",
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload={
                "document_id": document.id,
                "document_revision_id": revision.id,
                "revision_label": revision_label,
                "object_hash": stored.object_hash,
                "manifest_hash": content_hash(manifest),
                "all_files_processed": manifest.all_files_processed,
                "candidate_document_set_revision_id": candidate_id,
                "invalidated_document_set_revision_id": invalidated_document_set_id,
                "invalidated_derived_record_counts": invalidated_counts,
                "quarantine_upload_id": quarantine_upload_id,
                "submitted_by": submitted_by,
            },
        )
        self._outbox(
            topic="document.revision.received",
            aggregate_id=revision.id,
            payload={
                "project_id": project.id,
                "document_revision_id": revision.id,
                "object_hash": stored.object_hash,
            },
        )
        return DocumentUploadResult(
            document_id=document.id,
            document_revision_id=revision.id,
            candidate_document_set_revision_id=candidate_id,
            manifest=manifest,
            project_state=ApprovalState(project.state),
        )

    def register_quarantined_upload(
        self,
        *,
        actor: Actor,
        project_id: str,
        upload_id: str,
        object_hash: str,
        make_candidate_current: bool,
        inherited_invalidated_document_set_id: str | None,
        request_id: str,
        reason: str,
    ) -> str | None:
        project = self.get_project(
            actor=actor,
            project_id=project_id,
            lock=True,
            required_roles=(ActorRole.ESTIMATOR, ActorRole.TECHNICAL_EXPERT),
        )
        state = ApprovalState(project.state)
        if state in {
            ApprovalState.APPROVED_FOR_BID,
            ApprovalState.SUPERSEDED,
            ApprovalState.ARCHIVED,
        }:
            raise ValueError("Released/superseded/archived projects are immutable")
        invalidated_document_set_id = (
            (project.current_document_set_revision_id or inherited_invalidated_document_set_id)
            if make_candidate_current
            else None
        )
        now = utc_now()
        if make_candidate_current:
            project.current_document_set_revision_id = None
            if state is ApprovalState.DRAFT:
                self._change_state(
                    project=project,
                    to_state=ApprovalState.DOCUMENTS_INCOMPLETE,
                    actor=actor,
                    request_id=request_id,
                    reason="Current-candidate upload is quarantined pending qualified scanning",
                )
            elif state not in {
                ApprovalState.DOCUMENTS_INCOMPLETE,
                ApprovalState.BLOCKED,
            }:
                self._change_state(
                    project=project,
                    to_state=ApprovalState.BLOCKED,
                    actor=actor,
                    request_id=request_id,
                    reason="Potential new document revision is quarantined",
                )
            else:
                project.row_version += 1
                project.updated_at = now
        else:
            project.row_version += 1
            project.updated_at = now
        finding_payload = {
            "upload_id": upload_id,
            "object_hash": object_hash,
            "make_candidate_current": make_candidate_current,
        }
        self.session.add(
            VerificationFindingRow(
                id=self.quarantine_finding_id(upload_id),
                project_id=project.id,
                contour="INPUT_INTEGRITY",
                code="QUARANTINE_SCAN_PENDING",
                severity=(
                    Severity.BLOCKER.value if make_candidate_current else Severity.WARNING.value
                ),
                resolved=False,
                payload=finding_payload,
                created_at=now,
                updated_at=now,
            )
        )
        self._audit(
            aggregate_type="project",
            aggregate_id=project.id,
            event_type="document_upload_quarantined",
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload={
                **finding_payload,
                "invalidated_document_set_revision_id": invalidated_document_set_id,
            },
        )
        self._outbox(
            topic="document.upload.quarantined",
            aggregate_id=upload_id,
            payload={
                "project_id": project.id,
                "upload_id": upload_id,
                "object_hash": object_hash,
            },
        )
        return invalidated_document_set_id

    @staticmethod
    def quarantine_finding_id(upload_id: str) -> str:
        return f"finding-quarantine-{content_hash(upload_id)[:24]}"

    def confirm_document_set(
        self,
        *,
        actor: Actor,
        project_id: str,
        candidate_id: str,
        request_id: str,
        reason: str,
    ) -> ProjectView:
        candidate_id = self._required_text(candidate_id, "candidate_id", 64)
        reason = self._required_text(reason, "reason", 2000)
        project = self.get_project(
            actor=actor,
            project_id=project_id,
            lock=True,
            required_roles=(ActorRole.REVIEWER, ActorRole.APPROVER),
        )
        candidate = self.session.scalar(
            select(DocumentSetRevisionRow)
            .where(
                DocumentSetRevisionRow.id == candidate_id,
                DocumentSetRevisionRow.project_id == project.id,
            )
            .with_for_update()
        )
        if candidate is None:
            raise LookupError(candidate_id)
        if candidate.status != "DRAFT":
            raise ValueError("Only a DRAFT document-set candidate can be confirmed")
        if candidate.created_by == actor.actor_id:
            raise ValueError(
                "Document-set confirmation requires an actor different from the submitter"
            )
        latest_revision_ids = self._current_revision_ids(project.id)
        if candidate.revision_ids != latest_revision_ids:
            raise ValueError("Document-set candidate is stale")
        now = utc_now()
        self.session.query(DocumentSetRevisionRow).filter(
            DocumentSetRevisionRow.project_id == project.id,
            DocumentSetRevisionRow.status == "CONFIRMED",
        ).update({"status": "SUPERSEDED"})
        candidate.status = "CONFIRMED"
        candidate.confirmed_by = actor.actor_id
        candidate.confirmed_at = now
        project.current_document_set_revision_id = candidate.id
        project.row_version += 1
        project.updated_at = now
        self._audit(
            aggregate_type="project",
            aggregate_id=project.id,
            event_type="document_set_confirmed",
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload={
                "document_set_revision_id": candidate.id,
                "manifest_hash": candidate.manifest_hash,
                "revision_ids": candidate.revision_ids,
            },
        )
        return self._view(project)

    def document_set_view(
        self,
        *,
        actor: Actor,
        project_id: str,
        document_set_id: str,
    ) -> DocumentSetView:
        document_set_id = self._required_text(document_set_id, "document_set_id", 64)
        project = self.get_project(
            actor=actor,
            project_id=project_id,
            required_roles=(
                ActorRole.ESTIMATOR,
                ActorRole.TECHNICAL_EXPERT,
                ActorRole.REVIEWER,
                ActorRole.APPROVER,
                ActorRole.AUDITOR,
            ),
        )
        row = self.session.scalar(
            select(DocumentSetRevisionRow).where(
                DocumentSetRevisionRow.id == document_set_id,
                DocumentSetRevisionRow.project_id == project.id,
            )
        )
        if row is None:
            raise LookupError(document_set_id)
        return self._document_set_view(row)

    def transition(
        self,
        *,
        actor: Actor,
        project_id: str,
        to_state: ApprovalState,
        expected_row_version: int,
        request_id: str,
        reason: str,
    ) -> ProjectView:
        if to_state in {
            ApprovalState.APPROVED_FOR_BID,
            ApprovalState.APPROVED_FOR_INTERNAL_USE,
        }:
            raise ValueError("Approval states require a dedicated release decision")
        project = self.get_project(
            actor=actor,
            project_id=project_id,
            lock=True,
            required_roles=(
                ActorRole.ESTIMATOR,
                ActorRole.PROCUREMENT,
                ActorRole.TECHNICAL_EXPERT,
                ActorRole.REVIEWER,
            ),
        )
        if project.row_version != expected_row_version:
            raise OptimisticLockError(
                f"Expected row version {expected_row_version}, found {project.row_version}"
            )
        self._validate_stage_gate(project, to_state)
        self._change_state(
            project=project,
            to_state=to_state,
            actor=actor,
            request_id=request_id,
            reason=reason,
        )
        return self._view(project)

    def _validate_stage_gate(
        self,
        project: ProjectRow,
        to_state: ApprovalState,
    ) -> None:
        from_state = ApprovalState(project.state)
        blockers: tuple[str, ...] = ()
        gate_name: str | None = None
        if (
            from_state is ApprovalState.EXTRACTION_REVIEW
            and to_state is ApprovalState.BOQ_IN_PROGRESS
        ):
            gate_name = "project passport"
            blockers = passport_stage_blockers(self.session, project.id)
        elif from_state is ApprovalState.BOQ_IN_PROGRESS and to_state is ApprovalState.BOQ_REVIEW:
            gate_name = "BoQ verification"
            blockers = boq_stage_blockers(self.session, project.id)
        elif from_state is ApprovalState.BOQ_REVIEW and to_state in {
            ApprovalState.PRICING_IN_PROGRESS,
            ApprovalState.RFQ_REQUIRED,
        }:
            gate_name = "scope completeness"
            blockers = scope_stage_blockers(self.session, project.id)
        elif (
            from_state is ApprovalState.PRICING_IN_PROGRESS
            and to_state is ApprovalState.CALCULATION_IN_PROGRESS
        ):
            gate_name = "pricing verification"
            blockers = (
                pricing_stage_blockers(self.session, project.id)
                + contract_stage_blockers(self.session, project.id)
                + risk_stage_blockers(self.session, project.id)
            )
        if blockers:
            raise ValueError(
                f"{gate_name} stage gate blocked the transition: {', '.join(blockers)}"
            )

    def evaluate_release(self, *, actor: Actor, project_id: str) -> ReleaseContext:
        project = self.get_project(
            actor=actor,
            project_id=project_id,
            required_roles=(
                ActorRole.ESTIMATOR,
                ActorRole.REVIEWER,
                ActorRole.APPROVER,
                ActorRole.AUDITOR,
            ),
        )
        return self._build_release_context(project)

    def evaluate_release_gates(
        self,
        *,
        actor: Actor,
        project_id: str,
    ) -> tuple[ProjectView, GateDecision, str, GateDecision, str]:
        project = self.get_project(
            actor=actor,
            project_id=project_id,
            required_roles=(
                ActorRole.ESTIMATOR,
                ActorRole.REVIEWER,
                ActorRole.APPROVER,
                ActorRole.AUDITOR,
            ),
        )
        context = self._build_release_context(project)
        bid_decision = evaluate_bid_release(context)
        internal_decision = evaluate_internal_release(context)
        return (
            self._view(project),
            bid_decision,
            self._release_gate_hash(project, context, bid_decision),
            internal_decision,
            self._release_gate_hash(project, context, internal_decision),
        )

    def normative_engine_qualified(self) -> bool:
        return self._qualified_normative_adapter() is not None

    def attempt_bid_release(
        self,
        *,
        actor: Actor,
        project_id: str,
        expected_row_version: int,
        expected_gate_hash: str,
        request_id: str,
        reason: str,
    ) -> tuple[ProjectView, Any]:
        return self._attempt_release(
            actor=actor,
            project_id=project_id,
            expected_row_version=expected_row_version,
            expected_gate_hash=expected_gate_hash,
            request_id=request_id,
            reason=reason,
            requested_state=ApprovalState.APPROVED_FOR_BID,
        )

    def attempt_internal_release(
        self,
        *,
        actor: Actor,
        project_id: str,
        expected_row_version: int,
        expected_gate_hash: str,
        request_id: str,
        reason: str,
    ) -> tuple[ProjectView, Any]:
        return self._attempt_release(
            actor=actor,
            project_id=project_id,
            expected_row_version=expected_row_version,
            expected_gate_hash=expected_gate_hash,
            request_id=request_id,
            reason=reason,
            requested_state=ApprovalState.APPROVED_FOR_INTERNAL_USE,
        )

    def _attempt_release(
        self,
        *,
        actor: Actor,
        project_id: str,
        expected_row_version: int,
        expected_gate_hash: str,
        request_id: str,
        reason: str,
        requested_state: ApprovalState,
    ) -> tuple[ProjectView, Any]:
        project = self.get_project(
            actor=actor,
            project_id=project_id,
            lock=True,
            required_roles=(ActorRole.APPROVER,),
        )
        if project.row_version != expected_row_version:
            raise OptimisticLockError(
                f"Expected row version {expected_row_version}, found {project.row_version}"
            )
        requested_transition = WorkflowTransition(
            project_id=project.id,
            from_state=ApprovalState(project.state),
            to_state=requested_state,
            actor_id=actor.actor_id,
            reason=reason,
            occurred_at=utc_now(),
        )
        validate_transition(requested_transition)
        context = self._build_release_context(project)
        decision = (
            evaluate_bid_release(context)
            if requested_state is ApprovalState.APPROVED_FOR_BID
            else evaluate_internal_release(context)
        )
        gate_hash = self._release_gate_hash(project, context, decision)
        if gate_hash != expected_gate_hash:
            raise ValueError("Release gate changed; reload the complete server evaluation")
        release_context_hash = content_hash(context)
        now = utc_now()
        decision_id = f"release-decision-{uuid4()}"
        self.session.add(
            ReleaseDecisionRow(
                id=decision_id,
                project_id=project.id,
                snapshot_id=context.snapshot.snapshot_id if context.snapshot else None,
                requested_state=decision.requested_state.value,
                resulting_state=decision.resulting_state.value,
                allowed=decision.allowed,
                payload={
                    **decision.model_dump(mode="json"),
                    "gate_hash": gate_hash,
                    "release_context_hash": release_context_hash,
                    "project_row_version": expected_row_version,
                },
                decided_by=actor.actor_id,
                decided_at=now,
            )
        )
        target = decision.resulting_state
        self._change_state(
            project=project,
            to_state=target,
            actor=actor,
            request_id=request_id,
            reason=reason if decision.allowed else "Release policy blocked the calculation",
        )
        self._audit(
            aggregate_type="project",
            aggregate_id=project.id,
            event_type=(
                "bid_release_decided"
                if requested_state is ApprovalState.APPROVED_FOR_BID
                else "internal_release_decided"
            ),
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload={
                "decision_id": decision_id,
                "gate_hash": gate_hash,
                "release_context_hash": release_context_hash,
                "project_row_version": expected_row_version,
                "snapshot_id": context.snapshot.snapshot_id if context.snapshot else None,
                "allowed": decision.allowed,
                "resulting_state": decision.resulting_state,
                "finding_codes": [item.code for item in decision.findings],
            },
        )
        return self._view(project), decision

    @staticmethod
    def _release_gate_hash(
        project: ProjectRow,
        context: ReleaseContext,
        decision: GateDecision,
    ) -> str:
        return content_hash(
            {
                "project_id": project.id,
                "project_state": project.state,
                "project_row_version": project.row_version,
                "current_document_set_revision_id": (project.current_document_set_revision_id),
                "release_context": context,
                "decision": decision,
            }
        )

    def _change_state(
        self,
        *,
        project: ProjectRow,
        to_state: ApprovalState,
        actor: Actor,
        request_id: str,
        reason: str,
    ) -> None:
        from_state = ApprovalState(project.state)
        transition = WorkflowTransition(
            project_id=project.id,
            from_state=from_state,
            to_state=to_state,
            actor_id=actor.actor_id,
            reason=reason,
            occurred_at=utc_now(),
        )
        validate_transition(transition)
        if to_state is ApprovalState.BLOCKED:
            project.blocked_resume_state = from_state.value
        elif from_state is ApprovalState.BLOCKED:
            project.blocked_resume_state = None
        project.state = to_state.value
        project.row_version += 1
        project.updated_at = transition.occurred_at
        self.session.add(
            WorkflowTransitionRow(
                id=f"workflow-transition-{uuid4()}",
                project_id=project.id,
                from_state=from_state.value,
                to_state=to_state.value,
                actor_id=actor.actor_id,
                reason=reason,
                occurred_at=transition.occurred_at,
            )
        )
        self._audit(
            aggregate_type="project",
            aggregate_id=project.id,
            event_type="workflow_transition",
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload={"from_state": from_state, "to_state": to_state},
        )

    def _create_document_set_candidate(
        self, project: ProjectRow, actor_id: str, created_at: datetime
    ) -> str:
        revision_ids = self._current_revision_ids(project.id)
        manifest_hash = content_hash(revision_ids)
        existing = self.session.scalar(
            select(DocumentSetRevisionRow).where(
                DocumentSetRevisionRow.project_id == project.id,
                DocumentSetRevisionRow.manifest_hash == manifest_hash,
            )
        )
        if existing is not None:
            return existing.id
        candidate_id = f"document-set-{manifest_hash[:24]}"
        self.session.add(
            DocumentSetRevisionRow(
                id=candidate_id,
                project_id=project.id,
                manifest_hash=manifest_hash,
                revision_ids=revision_ids,
                status="DRAFT",
                created_by=actor_id,
                created_at=created_at,
            )
        )
        return candidate_id

    def _invalidate_derived_records(
        self,
        *,
        project_id: str,
        document_set_revision_id: str,
        replacement_document_revision_id: str,
        invalidated_at: datetime,
    ) -> dict[str, int]:
        marker = {
            "invalidated_by_document_revision_id": replacement_document_revision_id,
            "invalidated_document_set_revision_id": document_set_revision_id,
            "invalidated_at": invalidated_at.isoformat(),
        }
        counts: dict[str, int] = {}

        boq_lines = list(
            self.session.scalars(
                select(BoqLineRow).where(
                    BoqLineRow.project_id == project_id,
                    BoqLineRow.is_current.is_(True),
                )
            )
        )
        for boq_line in boq_lines:
            boq_line.status = VerificationStatus.IN_REVIEW.value
            boq_line.payload = {**boq_line.payload, **marker}
            boq_line.updated_at = invalidated_at
        counts["boq_lines"] = len(boq_lines)

        quantities = list(
            self.session.scalars(
                select(QuantityRow)
                .join(BoqLineRow, BoqLineRow.id == QuantityRow.boq_line_id)
                .where(
                    BoqLineRow.project_id == project_id,
                    BoqLineRow.is_current.is_(True),
                    QuantityRow.is_current.is_(True),
                )
            )
        )
        for quantity in quantities:
            quantity.status = VerificationStatus.IN_REVIEW.value
            quantity.payload = {**quantity.payload, **marker}
            quantity.updated_at = invalidated_at
        counts["quantities"] = len(quantities)

        passport_facts = list(
            self.session.scalars(
                select(ProjectPassportFactRow).where(
                    ProjectPassportFactRow.project_id == project_id,
                    ProjectPassportFactRow.is_current.is_(True),
                )
            )
        )
        for passport_fact in passport_facts:
            passport_fact.status = VerificationStatus.IN_REVIEW.value
            passport_fact.payload = {**passport_fact.payload, **marker}
            passport_fact.updated_at = invalidated_at
        counts["passport_facts"] = len(passport_facts)

        scope_evaluations = list(
            self.session.scalars(
                select(ScopeEvaluationRow).where(
                    ScopeEvaluationRow.project_id == project_id,
                    ScopeEvaluationRow.is_current.is_(True),
                )
            )
        )
        for scope_evaluation in scope_evaluations:
            scope_evaluation.status = "STALE"
            scope_evaluation.payload = {**scope_evaluation.payload, **marker}
        counts["scope_evaluations"] = len(scope_evaluations)

        nomenclature_matches = list(
            self.session.scalars(
                select(NomenclatureMatchRow).where(
                    NomenclatureMatchRow.project_id == project_id,
                    NomenclatureMatchRow.is_current.is_(True),
                )
            )
        )
        for nomenclature_match in nomenclature_matches:
            nomenclature_match.status = VerificationStatus.IN_REVIEW.value
            nomenclature_match.payload = {**nomenclature_match.payload, **marker}
            nomenclature_match.updated_at = invalidated_at
        counts["nomenclature_matches"] = len(nomenclature_matches)

        price_decisions = list(
            self.session.scalars(
                select(PriceDecisionRow).where(
                    PriceDecisionRow.project_id == project_id,
                    PriceDecisionRow.is_current.is_(True),
                )
            )
        )
        for price_decision in price_decisions:
            price_decision.is_current = False
            self.session.add(
                PriceDecisionRow(
                    id=f"price-decision-{uuid4()}",
                    project_id=price_decision.project_id,
                    item_id=price_decision.item_id,
                    status=PriceStatus.EXPIRED.value,
                    amount_per_unit=price_decision.amount_per_unit,
                    currency=price_decision.currency,
                    unit=price_decision.unit,
                    policy_version_id=price_decision.policy_version_id,
                    derived_observation_id=None,
                    supersedes_decision_id=price_decision.id,
                    is_current=True,
                    payload={
                        **price_decision.payload,
                        **marker,
                        "expired_from_decision_id": price_decision.id,
                    },
                    created_at=invalidated_at,
                )
            )
        counts["price_decisions"] = len(price_decisions)

        normative_calculations = list(
            self.session.scalars(
                select(NormativeCalculationRow).where(
                    NormativeCalculationRow.project_id == project_id,
                    NormativeCalculationRow.status == "VALIDATED",
                )
            )
        )
        for normative_calculation in normative_calculations:
            normative_calculation.status = "STALE"
            normative_calculation.payload = {**normative_calculation.payload, **marker}
        counts["normative_calculations"] = len(normative_calculations)

        contract_terms = list(
            self.session.scalars(
                select(ContractTermRow).where(
                    ContractTermRow.project_id == project_id,
                    ContractTermRow.is_current.is_(True),
                )
            )
        )
        for contract_term in contract_terms:
            contract_term.verified = False
            contract_term.cost_impact_resolved = False
            contract_term.payload = {**contract_term.payload, **marker}
            contract_term.updated_at = invalidated_at
        counts["contract_terms"] = len(contract_terms)

        risk_items = list(
            self.session.scalars(
                select(RiskItemRow).where(
                    RiskItemRow.project_id == project_id,
                    RiskItemRow.is_current.is_(True),
                )
            )
        )
        for risk_item in risk_items:
            risk_item.status = VerificationStatus.IN_REVIEW.value
            risk_item.payload = {**risk_item.payload, **marker}
            risk_item.updated_at = invalidated_at
        counts["risk_items"] = len(risk_items)

        risk_calculations = list(
            self.session.scalars(
                select(RiskCalculationRow).where(
                    RiskCalculationRow.project_id == project_id,
                    RiskCalculationRow.is_current.is_(True),
                )
            )
        )
        for risk_calculation in risk_calculations:
            risk_calculation.status = "STALE"
            risk_calculation.payload = {**risk_calculation.payload, **marker}
        counts["risk_calculations"] = len(risk_calculations)

        approval_tasks = list(
            self.session.scalars(
                select(ApprovalTaskRow).where(
                    ApprovalTaskRow.project_id == project_id,
                    ApprovalTaskRow.status != "SUPERSEDED",
                )
            )
        )
        for approval_task in approval_tasks:
            approval_task.status = "SUPERSEDED"
            approval_task.payload = {**approval_task.payload, **marker}
            approval_task.updated_at = invalidated_at
        counts["approval_tasks"] = len(approval_tasks)
        return counts

    def _current_revision_ids(self, project_id: str) -> list[str]:
        return list(
            self.session.scalars(
                select(DocumentRevisionRow.id)
                .join(DocumentRow, DocumentRow.id == DocumentRevisionRow.document_id)
                .where(
                    DocumentRow.project_id == project_id,
                    DocumentRow.cancelled.is_(False),
                    DocumentRevisionRow.is_current.is_(True),
                )
                .order_by(DocumentRevisionRow.id)
            )
        )

    def _build_release_context(self, project: ProjectRow) -> ReleaseContext:
        critical_documents = list(
            self.session.scalars(
                select(DocumentRow).where(
                    DocumentRow.project_id == project.id,
                    DocumentRow.critical.is_(True),
                    DocumentRow.cancelled.is_(False),
                )
            )
        )
        current_revisions = {
            revision.document_id: revision
            for revision in self.session.scalars(
                select(DocumentRevisionRow)
                .join(DocumentRow, DocumentRow.id == DocumentRevisionRow.document_id)
                .where(
                    DocumentRow.project_id == project.id,
                    DocumentRevisionRow.is_current.is_(True),
                )
            )
        }
        missing_critical = tuple(
            document.id
            for document in critical_documents
            if document.id not in current_revisions
            or current_revisions[document.id].corrupt
            or current_revisions[document.id].protected
        )
        quantity_ids = tuple(
            self.session.scalars(
                select(QuantityRow.id)
                .join(BoqLineRow, BoqLineRow.id == QuantityRow.boq_line_id)
                .where(
                    BoqLineRow.project_id == project.id,
                    BoqLineRow.is_current.is_(True),
                    QuantityRow.is_current.is_(True),
                    QuantityRow.status != VerificationStatus.VERIFIED.value,
                )
            )
        )
        conflict_ids = tuple(
            self.session.scalars(
                select(ConflictRow.id).where(
                    ConflictRow.project_id == project.id,
                    ConflictRow.status != VerificationStatus.VERIFIED.value,
                )
            )
        )
        cost_without_basis = tuple(
            self.session.scalars(
                select(CostInputRow.id).where(
                    CostInputRow.project_id == project.id,
                    CostInputRow.amount_basis_id.is_(None),
                )
            )
        )
        analogue_rows = list(
            self.session.scalars(
                select(NomenclatureMatchRow).where(NomenclatureMatchRow.project_id == project.id)
            )
        )
        unverified_analogues = tuple(
            row.id
            for row in analogue_rows
            if row.is_current
            and (
                row.status != VerificationStatus.VERIFIED.value
                or row.match_class
                in {
                    MatchClass.TECHNICALLY_UNACCEPTABLE.value,
                    MatchClass.INSUFFICIENT_DATA.value,
                }
            )
        )
        price_violations = tuple(
            self.session.scalars(
                select(PriceDecisionRow.id).where(
                    PriceDecisionRow.project_id == project.id,
                    PriceDecisionRow.is_current.is_(True),
                    PriceDecisionRow.status != PriceStatus.VERIFIED.value,
                )
            )
        )
        outstanding_approvals = tuple(
            self.session.scalars(
                select(ApprovalTaskRow.id).where(
                    ApprovalTaskRow.project_id == project.id,
                    ApprovalTaskRow.required.is_(True),
                    ApprovalTaskRow.status != "APPROVED",
                    ApprovalTaskRow.status != "SUPERSEDED",
                )
            )
        )
        blocking_findings = list(
            self.session.scalars(
                select(VerificationFindingRow).where(
                    VerificationFindingRow.project_id == project.id,
                    VerificationFindingRow.severity == Severity.BLOCKER.value,
                    VerificationFindingRow.resolved.is_(False),
                )
            )
        )
        contract_risks = tuple(
            row.id for row in blocking_findings if row.contour == "CONTRACT"
        ) + contract_stage_blockers(self.session, project.id)
        boq_blockers = tuple(
            self.session.scalars(
                select(BoqLineRow.id).where(
                    BoqLineRow.project_id == project.id,
                    BoqLineRow.is_current.is_(True),
                    BoqLineRow.status != VerificationStatus.VERIFIED.value,
                )
            )
        )
        scope_blockers = tuple(
            self.session.scalars(
                select(ScopeFindingRow.id).where(
                    ScopeFindingRow.project_id == project.id,
                    ScopeFindingRow.severity == Severity.BLOCKER.value,
                    ScopeFindingRow.resolved.is_(False),
                )
            )
        )
        other_blockers = (
            tuple(row.id for row in blocking_findings if row.contour != "CONTRACT")
            + boq_blockers
            + scope_blockers
            + passport_stage_blockers(self.session, project.id)
            + scope_stage_blockers(self.session, project.id)
            + risk_stage_blockers(self.session, project.id)
        )
        bound_versions = list(
            self.session.execute(
                select(ControlledVersionRow)
                .join(
                    ProjectControlledVersionRow,
                    ProjectControlledVersionRow.controlled_version_id == ControlledVersionRow.id,
                )
                .where(ProjectControlledVersionRow.project_id == project.id)
            ).scalars()
        )
        controlled_versions = tuple(
            ControlledVersion(
                kind=row.kind,
                version_id=row.id,
                content_hash=row.content_hash,
                status=VersionStatus(row.status),
                approved_by=row.approved_by,
                approved_at=ensure_utc(row.approved_at),
            )
            for row in bound_versions
        )
        approval_policy = next(
            (row for row in bound_versions if row.kind == "approval_policy"),
            None,
        )
        threshold_raw = (
            approval_policy.payload.get("max_unverified_cost_share") if approval_policy else None
        )
        threshold = None if threshold_raw is None else str(threshold_raw)

        snapshot_row = self.session.scalar(
            select(CalculationSnapshotRow)
            .where(CalculationSnapshotRow.project_id == project.id)
            .order_by(CalculationSnapshotRow.created_at.desc())
            .limit(1)
        )
        latest_calculation = (
            self.session.scalar(
                select(CalculationRunRow).where(
                    CalculationRunRow.id == snapshot_row.calculation_run_id,
                    CalculationRunRow.project_id == project.id,
                )
            )
            if snapshot_row is not None
            else None
        )
        independent = None
        project_total = "0"
        if latest_calculation is not None:
            project_total = str(latest_calculation.grand_total)
            raw_validation = latest_calculation.payload.get("independent_validation")
            if raw_validation:
                independent = IndependentValidationResult.model_validate(raw_validation)
        snapshot = (
            CalculationSnapshot(
                snapshot_id=snapshot_row.id,
                project_id=snapshot_row.project_id,
                document_set_revision_id=snapshot_row.document_set_revision_id,
                input_hash=snapshot_row.input_hash,
                output_hash=snapshot_row.output_hash,
                snapshot_hash=snapshot_row.snapshot_hash,
                created_by=snapshot_row.created_by,
                created_at=ensure_utc(snapshot_row.created_at),
                fixed=snapshot_row.fixed,
            )
            if snapshot_row
            else None
        )
        snapshot_integrity_valid = False
        snapshot_controlled_versions_match = False
        if snapshot_row is not None:
            try:
                verified_snapshot_payload = read_verified_snapshot(
                    object_store=self.object_store,
                    snapshot=snapshot_row,
                )
                snapshot_integrity_valid = True
                snapshot_versions = verified_snapshot_payload.get("controlled_versions")
                if isinstance(snapshot_versions, list):
                    snapshot_version_set = {
                        (
                            item.get("kind"),
                            item.get("version_id"),
                            item.get("content_hash"),
                        )
                        for item in snapshot_versions
                        if isinstance(item, dict)
                    }
                    bound_version_set = {
                        (row.kind, row.id, row.content_hash) for row in bound_versions
                    }
                    snapshot_controlled_versions_match = (
                        len(snapshot_version_set) == len(snapshot_versions)
                        and snapshot_version_set == bound_version_set
                    )
            except Exception:
                snapshot_integrity_valid = False
                snapshot_controlled_versions_match = False
        changes = list(
            self.session.scalars(
                select(ManualChangeRow).where(
                    ManualChangeRow.project_id == project.id,
                    ManualChangeRow.critical.is_(True),
                )
            )
        )
        manual_change_applications = {
            row.manual_change_id: row
            for row in self.session.scalars(
                select(QuantityManualChangeApplicationRow).where(
                    QuantityManualChangeApplicationRow.project_id == project.id
                )
            )
        }
        changes = [
            change
            for change in changes
            if (
                change.payload.get("lifecycle_version") != "quantity-manual-change-v1"
                or change.id in manual_change_applications
            )
        ]
        approval_records = list(
            self.session.scalars(
                select(ApprovalRecordRow)
                .join(ApprovalTaskRow, ApprovalTaskRow.id == ApprovalRecordRow.task_id)
                .where(ApprovalTaskRow.project_id == project.id)
            )
        )
        approval_tasks = {
            row.id: row
            for row in self.session.scalars(
                select(ApprovalTaskRow).where(ApprovalTaskRow.project_id == project.id)
            )
        }
        four_eyes: list[FourEyesRecord] = []
        for change in changes:
            if change.payload.get("lifecycle_version") == "quantity-manual-change-v1":
                application = manual_change_applications.get(change.id)
                approval_id = (
                    application.payload.get("approval_id") if application is not None else None
                )
                approval = next(
                    (
                        record
                        for record in approval_records
                        if isinstance(approval_id, str) and record.id == approval_id
                    ),
                    None,
                )
                task = approval_tasks.get(approval.task_id) if approval is not None else None
                expected_evidence_ids = change.payload.get("source_observation_ids")
                approval_evidence_ids = (
                    approval.payload.get("evidence_ids") if approval is not None else None
                )
                applied_quantity = (
                    self.session.get(QuantityRow, application.quantity_id)
                    if application is not None
                    else None
                )
                applied_record = (
                    applied_quantity.payload.get("record")
                    if applied_quantity is not None
                    and applied_quantity.boq_line_id == change.entity_id
                    and applied_quantity.supersedes_quantity_id
                    == change.payload.get("previous_quantity_id")
                    else None
                )
                if (
                    application is None
                    or application.applied_by != change.changed_by
                    or application.payload.get("manual_change_hash") != content_hash(change.payload)
                    or application.payload.get("before_hash") != change.payload.get("before_hash")
                    or application.payload.get("after_hash") != change.payload.get("after_hash")
                    or application.payload.get("policy_version_id")
                    != change.payload.get("policy_version_id")
                    or not isinstance(applied_record, dict)
                    or applied_record.get("manual_change_id") != change.id
                    or approval is None
                    or approval.decision != "APPROVED"
                    or approval.decided_by == change.changed_by
                    or approval.payload.get("related_change_ids") != [change.id]
                    or not isinstance(expected_evidence_ids, list)
                    or not all(isinstance(item, str) for item in expected_evidence_ids)
                    or not isinstance(approval_evidence_ids, list)
                    or not all(isinstance(item, str) for item in approval_evidence_ids)
                    or len(approval_evidence_ids) != len(set(approval_evidence_ids))
                    or sorted(approval_evidence_ids) != sorted(expected_evidence_ids)
                    or task is None
                    or task.id != change.payload.get("approval_task_id")
                    or task.task_type != "MANUAL_CHANGE"
                    or task.entity_type != "manual_change"
                    or task.entity_id != change.id
                    or not task.required
                    or task.status != "APPROVED"
                ):
                    approval = None
            else:
                approval = next(
                    (
                        record
                        for record in approval_records
                        if change.id in record.payload.get("related_change_ids", [])
                        and record.decision == "APPROVED"
                    ),
                    None,
                )
            four_eyes.append(
                FourEyesRecord(
                    change_id=change.id,
                    changed_by=change.changed_by,
                    approved_by=approval.decided_by if approval else None,
                    approval_id=approval.id if approval else None,
                )
            )
        # Until cost inputs carry a verified monetary subtotal, every baseless
        # input is conservatively represented by the project total.
        if cost_without_basis and latest_calculation is not None:
            unverified_cost_total = latest_calculation.grand_total
        else:
            unverified_cost_total = Decimal("0")
        production_qualified = any(
            row.kind == "production_qualification"
            and row.status == VersionStatus.APPROVED.value
            and self._production_qualification_evidence_valid(
                row.payload,
                organization_id=project.organization_id,
            )
            for row in bound_versions
        )
        qualification = self._qualified_normative_adapter()
        normative_calculation_valid = False
        if qualification is not None:
            normative_calculation_valid = (
                self.session.scalar(
                    select(NormativeCalculationRow.id)
                    .where(
                        NormativeCalculationRow.project_id == project.id,
                        NormativeCalculationRow.adapter_qualification_id == qualification.id,
                        NormativeCalculationRow.status == "VALIDATED",
                        NormativeCalculationRow.total.is_not(None),
                        NormativeCalculationRow.currency.is_not(None),
                        NormativeCalculationRow.artifact_hash.is_not(None),
                    )
                    .order_by(NormativeCalculationRow.created_at.desc())
                    .limit(1)
                )
                is not None
            )
        operational_integrity_valid = self._operational_integrity_valid()
        return ReleaseContext(
            current_document_set_confirmed=bool(project.current_document_set_revision_id),
            current_document_set_revision_id=project.current_document_set_revision_id,
            missing_critical_document_ids=missing_critical,
            unverified_key_quantity_ids=quantity_ids,
            unresolved_conflict_ids=conflict_ids,
            cost_item_ids_without_basis=cost_without_basis,
            unverified_analogue_ids=unverified_analogues,
            price_normalization_violation_ids=price_violations,
            independent_validation=independent,
            unverified_cost_total=str(unverified_cost_total),
            project_cost_total=project_total,
            max_unverified_cost_share=threshold,
            outstanding_approval_ids=outstanding_approvals,
            controlled_versions=controlled_versions,
            snapshot=snapshot,
            snapshot_integrity_valid=snapshot_integrity_valid,
            snapshot_controlled_versions_match=snapshot_controlled_versions_match,
            critical_manual_changes=tuple(four_eyes),
            unresolved_contract_risk_ids=contract_risks,
            blocking_contour_finding_ids=other_blockers,
            normative_engine_qualified=qualification is not None,
            normative_calculation_valid=normative_calculation_valid,
            production_qualification_complete=production_qualified,
            operational_integrity_valid=operational_integrity_valid,
        )

    def _operational_integrity_valid(self) -> bool:
        if self.settings.app_env not in {"staging", "production"}:
            return True
        if not self.settings.worm_policy_configured:
            return False
        assert self.settings.s3_required_object_lock_mode is not None
        assert self.settings.s3_minimum_retention_days is not None
        try:
            retention_valid = self.object_store.retention_status().satisfies(
                required_mode=self.settings.s3_required_object_lock_mode,
                minimum_days=self.settings.s3_minimum_retention_days,
            )
            if not retention_valid:
                return False
            from tenderguard.application.audit_integrity import AuditIntegrityService

            return (
                AuditIntegrityService(
                    session=self.session,
                    settings=self.settings,
                    object_store=self.object_store,
                )
                .anchor_status()
                .valid
            )
        except Exception:
            return False

    def _current_membership(
        self,
        *,
        project_id: str,
        principal_id: str,
    ) -> ProjectMembershipRow | None:
        return self.session.scalar(
            select(ProjectMembershipRow)
            .where(
                ProjectMembershipRow.project_id == project_id,
                ProjectMembershipRow.principal_id == principal_id,
            )
            .order_by(ProjectMembershipRow.version.desc())
            .limit(1)
        )

    def _current_memberships(self, project_id: str) -> tuple[ProjectMembershipRow, ...]:
        latest_versions = (
            select(
                ProjectMembershipRow.principal_id.label("principal_id"),
                func.max(ProjectMembershipRow.version).label("version"),
            )
            .where(ProjectMembershipRow.project_id == project_id)
            .group_by(ProjectMembershipRow.principal_id)
            .subquery()
        )
        return tuple(
            self.session.scalars(
                select(ProjectMembershipRow)
                .join(
                    latest_versions,
                    and_(
                        latest_versions.c.principal_id == ProjectMembershipRow.principal_id,
                        latest_versions.c.version == ProjectMembershipRow.version,
                    ),
                )
                .where(ProjectMembershipRow.project_id == project_id)
                .order_by(ProjectMembershipRow.principal_id)
            )
        )

    def _append_membership(
        self,
        *,
        project: ProjectRow,
        principal_id: str,
        roles: Iterable[ActorRole],
        access_level: ProjectAccessLevel,
        status_value: ProjectMembershipStatus,
        changed_by: str,
        reason: str,
        previous: ProjectMembershipRow | None,
        now: datetime,
    ) -> ProjectMembershipRow:
        normalized_roles = tuple(roles)
        row = ProjectMembershipRow(
            id=f"membership-{uuid4()}",
            project_id=project.id,
            principal_id=principal_id,
            roles=sorted(role.value for role in normalized_roles),
            role_mask=project_role_mask(normalized_roles),
            access_level=access_level.value,
            status=status_value.value,
            version=1 if previous is None else previous.version + 1,
            supersedes_membership_id=None if previous is None else previous.id,
            changed_by=changed_by,
            reason=reason,
            created_at=now,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def _record_membership_change(
        self,
        *,
        actor: Actor,
        project_id: str,
        row: ProjectMembershipRow,
        request_id: str,
        reason: str,
        event_type: str,
    ) -> None:
        payload = {
            "membership_revision_id": row.id,
            "principal_id": row.principal_id,
            "roles": row.roles,
            "access_level": row.access_level,
            "status": row.status,
            "version": row.version,
            "supersedes_membership_id": row.supersedes_membership_id,
        }
        self._audit(
            aggregate_type="project",
            aggregate_id=project_id,
            event_type=event_type,
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload=payload,
        )
        self._outbox(
            topic="project.membership.changed",
            aggregate_id=project_id,
            payload={"project_id": project_id, **payload},
        )

    def _require_another_active_owner(self, project_id: str, *, excluding: str) -> None:
        owners = [
            row
            for row in self._current_memberships(project_id)
            if row.principal_id != excluding
            and row.status == ProjectMembershipStatus.ACTIVE.value
            and row.access_level == ProjectAccessLevel.OWNER.value
        ]
        if not owners:
            raise ValueError("The last active project owner cannot be removed or downgraded")

    def _require_system_access(
        self,
        *,
        actor: Actor,
        access: SystemProjectAccess,
    ) -> None:
        qualification = self.session.scalar(
            select(AdapterQualificationRow).where(
                AdapterQualificationRow.id == access.qualification_id,
                AdapterQualificationRow.status == "APPROVED",
            )
        )
        if qualification is None:
            self._deny_system_access()
        assert qualification is not None
        if qualification.valid_until and qualification.valid_until < utc_now().date():
            self._deny_system_access()
        payload = qualification.payload
        if (
            payload.get("organization_id") != actor.organization_id
            or payload.get("service_actor_id") != actor.actor_id
            or access.capability not in payload.get("supported_methods", [])
        ):
            self._deny_system_access()
        configured: tuple[str | None, str | None] | None = None
        if access.capability == "MALWARE_SCAN":
            configured = (
                self.settings.malware_scanner_qualification_id,
                self.settings.malware_scanner_adapter,
            )
        elif access.capability == "DOCUMENT_INTAKE":
            configured = (
                self.settings.document_processor_qualification_id,
                self.settings.document_processor_adapter,
            )
        if configured is not None and (
            qualification.id != configured[0] or qualification.adapter_name != configured[1]
        ):
            self._deny_system_access()

    @staticmethod
    def _deny_system_access() -> None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Qualified service access is denied",
        )

    @staticmethod
    def _membership_roles(row: ProjectMembershipRow) -> frozenset[ActorRole]:
        try:
            return validate_project_role_evidence(row.roles, row.role_mask)
        except ValueError as error:
            raise RuntimeError("Project membership role evidence is invalid") from error

    @staticmethod
    def _membership_view(row: ProjectMembershipRow) -> ProjectMembershipView:
        created_at = ensure_utc(row.created_at)
        assert created_at is not None
        return ProjectMembershipView(
            membership_revision_id=row.id,
            project_id=row.project_id,
            principal_id=row.principal_id,
            roles=tuple(sorted(ProjectService._membership_roles(row), key=lambda role: role.value)),
            access_level=ProjectAccessLevel(row.access_level),
            status=ProjectMembershipStatus(row.status),
            version=row.version,
            supersedes_membership_id=row.supersedes_membership_id,
            changed_by=row.changed_by,
            reason=row.reason,
            created_at=created_at,
        )

    @staticmethod
    def _required_text(value: str, field: str, max_length: int) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{field} is required")
        if len(normalized) > max_length:
            raise ValueError(f"{field} exceeds {max_length} characters")
        return normalized

    def _qualified_normative_adapter(self) -> AdapterQualificationRow | None:
        qualification_id = self.settings.normative_adapter_qualification_id
        adapter_name = self.settings.normative_adapter
        if not qualification_id or not adapter_name:
            return None
        qualification = self.session.scalar(
            select(AdapterQualificationRow).where(
                AdapterQualificationRow.id == qualification_id,
                AdapterQualificationRow.adapter_name == adapter_name,
                AdapterQualificationRow.status == "APPROVED",
            )
        )
        if qualification is None:
            return None
        if qualification.valid_until is not None and qualification.valid_until < utc_now().date():
            return None
        return qualification

    @staticmethod
    def _production_qualification_evidence_complete(payload: dict[str, Any]) -> bool:
        required_gates = {
            "historical_projects",
            "blind_estimator_comparison",
            "parallel_operation",
            "variance_resolution",
            "rules_and_catalog_calibration",
            "damaged_conflicting_document_resilience",
            "load_test",
            "security_review",
            "backup_restore",
            "methodology_approval",
        }
        gates = payload.get("gates")
        business = payload.get("business_qualification")
        if (
            payload.get("all_gates_complete") is not True
            or not isinstance(gates, dict)
            or not isinstance(business, dict)
        ):
            return False
        campaign_id = business.get("campaign_id")
        package_hash = business.get("package_hash")
        if (
            not isinstance(campaign_id, str)
            or not campaign_id
            or len(campaign_id) > 64
            or not isinstance(package_hash, str)
            or len(package_hash) != 64
            or any(character not in "0123456789abcdef" for character in package_hash)
            or not all(
                isinstance(business.get(field), str) and business[field].strip()
                for field in ("approved_by", "approved_at", "environment")
            )
        ):
            return False
        for gate_name in required_gates:
            gate = gates.get(gate_name)
            if not isinstance(gate, dict):
                return False
            evidence_hash = gate.get("evidence_hash")
            if (
                gate.get("status") != "PASSED"
                or not isinstance(evidence_hash, str)
                or len(evidence_hash) != 64
                or any(character not in "0123456789abcdef" for character in evidence_hash)
                or not all(
                    isinstance(gate.get(field), str) and gate[field].strip()
                    for field in (
                        "owner_id",
                        "approved_by",
                        "approved_at",
                        "environment",
                    )
                )
            ):
                return False
        for gate_name in (
            "historical_projects",
            "blind_estimator_comparison",
            "parallel_operation",
            "variance_resolution",
        ):
            gate = gates[gate_name]
            if (
                gate.get("evidence_hash") != package_hash
                or gate.get("source_reference") != f"business_qualification_campaign:{campaign_id}"
            ):
                return False
        return True

    def _production_qualification_evidence_valid(
        self,
        payload: dict[str, Any],
        *,
        organization_id: str,
    ) -> bool:
        if not self._production_qualification_evidence_complete(payload):
            return False
        business = payload["business_qualification"]
        campaign = self.session.scalar(
            select(BusinessQualificationCampaignRow).where(
                BusinessQualificationCampaignRow.id == business["campaign_id"],
                BusinessQualificationCampaignRow.organization_id == organization_id,
                BusinessQualificationCampaignRow.status == "PASSED",
            )
        )
        if (
            campaign is None
            or campaign.created_by == campaign.evaluated_by
            or campaign.created_by == campaign.finalized_by
            or campaign.evaluated_by == campaign.finalized_by
        ):
            return False
        approval = self.session.scalar(
            select(BusinessQualificationApprovalRow).where(
                BusinessQualificationApprovalRow.campaign_id == campaign.id,
                BusinessQualificationApprovalRow.package_hash == business["package_hash"],
            )
        )
        evaluation_row = self.session.scalar(
            select(BusinessQualificationEvaluationRow).where(
                BusinessQualificationEvaluationRow.campaign_id == campaign.id
            )
        )
        if (
            approval is None
            or evaluation_row is None
            or not evaluation_row.metrics_passed
            or approval.evaluation_id != evaluation_row.id
            or campaign.result_hash != evaluation_row.result_hash
            or campaign.finalized_by != approval.approved_by
            or business["approved_by"] != approval.approved_by
        ):
            return False
        try:
            claimed_approved_at = ensure_utc(
                datetime.fromisoformat(business["approved_at"].replace("Z", "+00:00"))
            )
            evaluation = BusinessQualificationEvaluation.model_validate(evaluation_row.payload)
            profile, profile_row = load_approved_profile(
                session=self.session,
                settings=self.settings,
                version_id=campaign.profile_version_id,
                expected_content_hash=campaign.profile_hash,
                expected_kind="business_qualification_profile",
                profile_type=BusinessQualificationProfile,
            )
            dataset, dataset_row = load_approved_profile(
                session=self.session,
                settings=self.settings,
                version_id=campaign.dataset_version_id,
                expected_content_hash=campaign.dataset_hash,
                expected_kind="business_qualification_dataset",
                profile_type=BusinessQualificationDataset,
            )
        except (LookupError, TypeError, ValueError):
            return False
        profile_governance = profile_row.payload.get("_governance")
        dataset_governance = dataset_row.payload.get("_governance")
        if (
            claimed_approved_at != ensure_utc(approval.approved_at)
            or claimed_approved_at != ensure_utc(campaign.finalized_at)
            or evaluation.result_hash != evaluation_row.result_hash
            or not evaluation.metrics_passed
            or evaluation.campaign_id != campaign.id
            or evaluation.profile_version_id != campaign.profile_version_id
            or evaluation.dataset_version_id != campaign.dataset_version_id
            or evaluation.application_build_reference != campaign.application_build_reference
            or evaluation.currency != profile.currency
            or profile.expected_application_build_reference != campaign.application_build_reference
            or self.settings.application_build_reference != campaign.application_build_reference
            or not isinstance(profile_governance, dict)
            or profile_governance.get("organization_id") != organization_id
            or not isinstance(dataset_governance, dict)
            or dataset_governance.get("organization_id") != organization_id
            or {metric.mode for metric in evaluation.modes} != {"HISTORICAL", "BLIND", "PARALLEL"}
            or any(not metric.passed for metric in evaluation.modes)
        ):
            return False
        cases = list(
            self.session.scalars(
                select(BusinessQualificationCaseRow).where(
                    BusinessQualificationCaseRow.campaign_id == campaign.id
                )
            )
        )
        references = list(
            self.session.scalars(
                select(BusinessQualificationReferenceRow).where(
                    BusinessQualificationReferenceRow.campaign_id == campaign.id
                )
            )
        )
        if (
            not cases
            or {case.mode for case in cases} != {"HISTORICAL", "BLIND", "PARALLEL"}
            or len(references) != len(cases)
            or {reference.case_id for reference in references} != {case.id for case in cases}
            or {metric.case_id for metric in evaluation.cases} != {case.id for case in cases}
        ):
            return False
        ordered_cases = sorted(cases, key=lambda item: item.case_key)
        dataset_cases = {item.case_key: item for item in dataset.cases}
        references_by_case = {item.case_id: item for item in references}
        reference_actors = {
            actor_id
            for reference in references
            for actor_id in (
                reference.registered_by,
                reference.payload.get("prepared_by"),
            )
            if isinstance(actor_id, str) and actor_id
        }
        if (
            set(dataset_cases) != {case.case_key for case in cases}
            or campaign.evaluated_by in reference_actors
            or campaign.finalized_by in reference_actors
        ):
            return False
        try:
            for case in ordered_cases:
                planned = dataset_cases[case.case_key]
                reference = references_by_case[case.id]
                prediction_identity = {
                    "case_key": case.case_key,
                    "mode": case.mode,
                    "project_id": case.project_id,
                    "snapshot_id": case.snapshot_id,
                    "snapshot_hash": case.snapshot_hash,
                    "prediction_total": self._qualification_decimal_identity(case.prediction_total),
                    "currency": case.currency,
                }
                if (
                    planned.mode != case.mode
                    or planned.project_id != case.project_id
                    or planned.snapshot_id != case.snapshot_id
                    or planned.stratum != case.stratum
                    or content_hash(prediction_identity) != case.prediction_hash
                    or reference.campaign_id != campaign.id
                    or reference.currency != case.currency
                    or reference.reference_total <= 0
                ):
                    return False
                if case.mode == "HISTORICAL":
                    if (
                        planned.historical_actual_id != reference.source_entity_id
                        or reference.reference_kind != "VERIFIED_ACTUAL"
                        or reference.source_entity_type != "ACTUAL_RECORD"
                        or reference.payload.get("comparison_basis_hash")
                        != profile.comparison_basis_hash
                        or content_hash(reference.payload) != reference.evidence_hash
                    ):
                        return False
                else:
                    reference_payload = QualificationReferencePayload.model_validate(
                        reference.payload
                    )
                    observation = self.session.scalar(
                        select(ObservationRow).where(
                            ObservationRow.id == reference.source_entity_id,
                            ObservationRow.project_id == case.project_id,
                            ObservationRow.status == VerificationStatus.VERIFIED.value,
                        )
                    )
                    if (
                        planned.historical_actual_id is not None
                        or reference.source_entity_type != "OBSERVATION"
                        or observation is None
                        or reference_payload.case_key != case.case_key
                        or reference_payload.mode != case.mode
                        or reference_payload.amount != reference.reference_total
                        or reference_payload.currency != reference.currency
                        or reference_payload.comparison_basis_hash != profile.comparison_basis_hash
                        or reference_payload.evidence_hash != reference.evidence_hash
                        or reference_payload.reviewed_by != reference.registered_by
                        or observation.payload.get("qualification_reference") != reference.payload
                    ):
                        return False
            recomputed_input_hash = content_hash(
                {
                    "profile_version_id": campaign.profile_version_id,
                    "profile_hash": campaign.profile_hash,
                    "dataset_version_id": campaign.dataset_version_id,
                    "dataset_hash": campaign.dataset_hash,
                    "application_build_reference": campaign.application_build_reference,
                    "population_size": dataset.population_size,
                    "cases": [
                        {
                            "case_key": case.case_key,
                            "mode": case.mode,
                            "project_id": case.project_id,
                            "snapshot_id": case.snapshot_id,
                            "snapshot_hash": case.snapshot_hash,
                            "prediction_total": self._qualification_decimal_identity(
                                case.prediction_total
                            ),
                            "currency": case.currency,
                            "prediction_hash": case.prediction_hash,
                            "stratum": case.stratum,
                        }
                        for case in ordered_cases
                    ],
                    "exclusions": dataset.exclusions,
                }
            )
        except (ArithmeticError, KeyError, TypeError, ValueError):
            return False
        if (
            recomputed_input_hash != campaign.input_hash
            or campaign.payload.get("population_size") != dataset.population_size
            or campaign.payload.get("exclusion_count") != len(dataset.exclusions)
            or campaign.payload.get("population_evidence_hash") != dataset.population_evidence_hash
            or campaign.payload.get("selection_query_hash") != dataset.selection_query_hash
            or campaign.payload.get("selection_cutoff_at")
            != dataset.selection_cutoff_at.isoformat()
            or campaign.payload.get("case_prediction_hashes")
            != [case.prediction_hash for case in ordered_cases]
        ):
            return False
        discrepancies = list(
            self.session.scalars(
                select(BusinessQualificationDiscrepancyRow).where(
                    BusinessQualificationDiscrepancyRow.campaign_id == campaign.id
                )
            )
        )
        reviews = (
            list(
                self.session.scalars(
                    select(BusinessQualificationDiscrepancyReviewRow).where(
                        BusinessQualificationDiscrepancyReviewRow.discrepancy_id.in_(
                            [row.id for row in discrepancies]
                        )
                    )
                )
            )
            if discrepancies
            else []
        )
        if (
            {row.case_id for row in discrepancies}
            != {metric.case_id for metric in evaluation.cases if metric.material}
            or len(reviews) != len(discrepancies)
            or any(review.decision != "ACCEPTED" for review in reviews)
            or any(review.reviewed_by == approval.approved_by for review in reviews)
        ):
            return False
        reviews_by_discrepancy = {review.discrepancy_id: review for review in reviews}
        recomputed_package_hash = content_hash(
            {
                "campaign_id": campaign.id,
                "input_hash": campaign.input_hash,
                "profile_version_id": campaign.profile_version_id,
                "profile_hash": campaign.profile_hash,
                "dataset_version_id": campaign.dataset_version_id,
                "dataset_hash": campaign.dataset_hash,
                "application_build_reference": campaign.application_build_reference,
                "evaluation_id": evaluation_row.id,
                "evaluation_result_hash": evaluation.result_hash,
                "population_size": dataset.population_size,
                "profile_schema_version": profile.schema_version,
                "accepted_discrepancy_reviews": [
                    {
                        "discrepancy_id": discrepancy.id,
                        "review_id": reviews_by_discrepancy[discrepancy.id].id,
                        "evidence_hash": reviews_by_discrepancy[discrepancy.id].evidence_hash,
                    }
                    for discrepancy in sorted(discrepancies, key=lambda item: item.id)
                ],
            }
        )
        if (
            recomputed_package_hash != approval.package_hash
            or recomputed_package_hash != business["package_hash"]
        ):
            return False
        events = [
            self._audit_domain(row)
            for row in self.session.scalars(
                select(AuditEventRow)
                .where(
                    AuditEventRow.aggregate_type == "business_qualification_campaign",
                    AuditEventRow.aggregate_id == campaign.id,
                )
                .order_by(AuditEventRow.sequence)
            )
        ]
        locked = [
            event for event in events if event.event_type == "business_qualification_inputs_locked"
        ]
        evaluated = [
            event for event in events if event.event_type == "business_qualification_evaluated"
        ]
        approved = [
            event for event in events if event.event_type == "business_qualification_approved"
        ]
        return bool(
            events
            and verify_chain(events, self.settings.audit_verification_keyring)
            and len(locked) == len(evaluated) == len(approved) == 1
            and locked[0].actor_id == campaign.created_by
            and locked[0].payload.get("input_hash") == campaign.input_hash
            and evaluated[0].actor_id == campaign.evaluated_by
            and evaluated[0].payload.get("result_hash") == evaluation_row.result_hash
            and approved[0].actor_id == approval.approved_by
            and approved[0].payload.get("package_hash") == approval.package_hash
        )

    @staticmethod
    def _qualification_decimal_identity(value: Decimal) -> str:
        if not value.is_finite():
            raise ValueError("Qualification amount must be finite")
        return format(value.normalize(), "f")

    def _audit(
        self,
        *,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        actor: Actor,
        request_id: str,
        reason: str,
        payload: dict[str, Any],
    ) -> AuditEvent:
        previous_row = self.session.scalar(
            select(AuditEventRow)
            .where(
                AuditEventRow.aggregate_type == aggregate_type,
                AuditEventRow.aggregate_id == aggregate_id,
            )
            .order_by(AuditEventRow.sequence.desc())
            .limit(1)
            .with_for_update()
        )
        previous = self._audit_domain(previous_row) if previous_row else None
        event = append_event(
            previous=previous,
            event_id=f"audit-{uuid4()}",
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_type=event_type,
            actor_id=actor.actor_id,
            actor_roles=tuple(sorted(role.value for role in actor.roles)),
            request_id=request_id,
            reason=reason,
            occurred_at=utc_now(),
            payload=payload,
            signing_key=self.settings.audit_signing_key.get_secret_value().encode("utf-8"),
            signing_key_id=self.settings.audit_signing_key_id,
        )
        self.session.add(
            AuditEventRow(
                id=event.event_id,
                aggregate_type=event.aggregate_type,
                aggregate_id=event.aggregate_id,
                sequence=event.sequence,
                event_type=event.event_type,
                actor_id=event.actor_id,
                actor_roles=list(event.actor_roles),
                request_id=event.request_id,
                reason=event.reason,
                payload=canonical_data(event.payload),
                previous_hash=event.previous_hash,
                signing_key_id=event.signing_key_id,
                signature_version=event.signature_version,
                event_hash=event.event_hash,
                signature=event.signature,
                occurred_at=event.occurred_at,
            )
        )
        # Multiple audit events may be emitted in one transaction. Flush so
        # the next sequence lookup observes the pending event.
        self.session.flush()
        self._outbox(
            topic="audit.event.recorded",
            aggregate_id=event.aggregate_id,
            deduplication_key=f"audit-event:{event.event_id}",
            payload={
                "organization_id": actor.organization_id,
                "audit_event_id": event.event_id,
                "aggregate_type": event.aggregate_type,
                "aggregate_id": event.aggregate_id,
                "sequence": event.sequence,
                "event_type": event.event_type,
                "event_hash": event.event_hash,
                "occurred_at": event.occurred_at,
            },
        )
        return event

    @staticmethod
    def _audit_domain(row: AuditEventRow) -> AuditEvent:
        return AuditEvent(
            sequence=row.sequence,
            event_id=row.id,
            aggregate_type=row.aggregate_type,
            aggregate_id=row.aggregate_id,
            event_type=row.event_type,
            actor_id=row.actor_id,
            actor_roles=tuple(row.actor_roles),
            request_id=row.request_id,
            reason=row.reason,
            occurred_at=ensure_utc(row.occurred_at),
            payload=row.payload,
            previous_hash=row.previous_hash,
            signing_key_id=row.signing_key_id,
            signature_version=row.signature_version,
            event_hash=row.event_hash,
            signature=row.signature,
        )

    def _outbox(
        self,
        *,
        topic: str,
        aggregate_id: str,
        payload: dict[str, Any],
        deduplication_key: str | None = None,
    ) -> None:
        now = utc_now()
        event_id = f"outbox-{uuid4()}"
        event_deduplication_key = deduplication_key or f"outbox-event:{event_id}"
        self.session.add(
            OutboxEventRow(
                id=event_id,
                deduplication_key=event_deduplication_key,
                delivery_deduplication_key=event_deduplication_key,
                topic=topic,
                aggregate_id=aggregate_id,
                payload=canonical_data(payload),
                attempts=0,
                available_at=now,
                created_at=now,
            )
        )

    @staticmethod
    def _view(project: ProjectRow) -> ProjectView:
        return ProjectView(
            id=project.id,
            organization_id=project.organization_id,
            code=project.code,
            name=project.name,
            state=ApprovalState(project.state),
            row_version=project.row_version,
            current_document_set_revision_id=project.current_document_set_revision_id,
        )

    @staticmethod
    def _document_set_view(row: DocumentSetRevisionRow) -> DocumentSetView:
        created_at = ensure_utc(row.created_at)
        if created_at is None:
            raise RuntimeError("Document-set creation timestamp is missing")
        return DocumentSetView(
            id=row.id,
            project_id=row.project_id,
            manifest_hash=row.manifest_hash,
            revision_ids=tuple(row.revision_ids),
            status=row.status,
            created_by=row.created_by,
            created_at=created_at,
            confirmed_by=row.confirmed_by,
            confirmed_at=ensure_utc(row.confirmed_at),
        )
