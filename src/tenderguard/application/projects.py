from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Select, select
from sqlalchemy.orm import Session

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
from tenderguard.domain.audit import AuditEvent, append_event
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
    OutboxEventRow,
    PriceDecisionRow,
    ProjectControlledVersionRow,
    ProjectMembershipRow,
    ProjectPassportFactRow,
    ProjectRow,
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

    def normative_engine_qualified(self) -> bool:
        return self._qualified_normative_adapter() is not None

    def attempt_bid_release(
        self,
        *,
        actor: Actor,
        project_id: str,
        expected_row_version: int,
        request_id: str,
        reason: str,
    ) -> tuple[ProjectView, Any]:
        return self._attempt_release(
            actor=actor,
            project_id=project_id,
            expected_row_version=expected_row_version,
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
        request_id: str,
        reason: str,
    ) -> tuple[ProjectView, Any]:
        return self._attempt_release(
            actor=actor,
            project_id=project_id,
            expected_row_version=expected_row_version,
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
                payload=decision.model_dump(mode="json"),
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
                "allowed": decision.allowed,
                "resulting_state": decision.resulting_state,
                "finding_codes": [item.code for item in decision.findings],
            },
        )
        return self._view(project), decision

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
            price_decision.status = PriceStatus.EXPIRED.value
            price_decision.payload = {**price_decision.payload, **marker}
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
        approval_records = list(
            self.session.scalars(
                select(ApprovalRecordRow)
                .join(ApprovalTaskRow, ApprovalTaskRow.id == ApprovalRecordRow.task_id)
                .where(ApprovalTaskRow.project_id == project.id)
            )
        )
        four_eyes: list[FourEyesRecord] = []
        for change in changes:
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
            and self._production_qualification_evidence_complete(row.payload)
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
        rows = list(
            self.session.scalars(
                select(ProjectMembershipRow)
                .where(ProjectMembershipRow.project_id == project_id)
                .order_by(
                    ProjectMembershipRow.principal_id,
                    ProjectMembershipRow.version.desc(),
                )
            )
        )
        current: dict[str, ProjectMembershipRow] = {}
        for row in rows:
            current.setdefault(row.principal_id, row)
        return tuple(current.values())

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
        row = ProjectMembershipRow(
            id=f"membership-{uuid4()}",
            project_id=project.id,
            principal_id=principal_id,
            roles=sorted(role.value for role in roles),
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
        roles: set[ActorRole] = set()
        for value in row.roles:
            try:
                role = ActorRole(value)
            except ValueError:
                continue
            if role is not ActorRole.SYSTEM:
                roles.add(role)
        return frozenset(roles)

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
        if payload.get("all_gates_complete") is not True or not isinstance(gates, dict):
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
        return True

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
