from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import uuid4

from pydantic import Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from tenderguard.application.controlled_version_integrity import (
    require_bound_controlled_version,
)
from tenderguard.application.document_set_integrity import (
    require_confirmed_document_set_integrity,
)
from tenderguard.application.evidence_independence import (
    require_distinct_qualified_independence,
    resolve_observation_leaves,
)
from tenderguard.application.projects import OptimisticLockError, ProjectService
from tenderguard.config import Settings
from tenderguard.domain.common import content_hash, ensure_utc, utc_now
from tenderguard.domain.enums import (
    ActorRole,
    ApprovalDecision,
    ApprovalState,
    EvidenceMethod,
    Severity,
    VerificationStatus,
)
from tenderguard.domain.models import DomainModel, Observation, ValidationFinding
from tenderguard.domain.passport import (
    PassportFact,
    PassportRequirementsPolicy,
    ProjectPassport,
    validate_passport,
)
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.object_store import ObjectStore
from tenderguard.infrastructure.orm import (
    AdapterQualificationRow,
    ApprovalRecordRow,
    ApprovalTaskRow,
    ConflictRow,
    ControlledVersionRow,
    DocumentSetRevisionRow,
    ObservationRow,
    ProjectPassportFactRow,
    VerificationFindingRow,
)

_EDITABLE_STATES = frozenset(
    {
        ApprovalState.EXTRACTION_IN_PROGRESS,
        ApprovalState.EXTRACTION_REVIEW,
        ApprovalState.BOQ_IN_PROGRESS,
        ApprovalState.BOQ_REVIEW,
    }
)


class PassportFactDraft(DomainModel):
    field_name: str = Field(min_length=1, max_length=200)
    value: Any
    unit: str | None = Field(default=None, max_length=100)
    observation_ids: tuple[str, ...] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def identifiers_are_normalized_and_unique(self) -> PassportFactDraft:
        if self.field_name != self.field_name.strip():
            raise ValueError("Passport field name must be normalized")
        if len(self.observation_ids) != len(set(self.observation_ids)):
            raise ValueError("Passport observation IDs must be unique")
        if any(
            not item
            or item != item.strip()
            or len(item) > 64
            or any(character.isspace() for character in item)
            for item in self.observation_ids
        ):
            raise ValueError("Passport observation ID is invalid")
        return self


class PassportFactView(DomainModel):
    fact_id: str
    field_name: str
    value: Any
    unit: str | None
    observation_ids: tuple[str, ...]
    independence_source_ids: tuple[str, ...]
    status: VerificationStatus
    supersedes_fact_id: str | None
    is_current: bool
    created_by: str
    verified_by: str | None = None
    reviewed_by: str | None = None
    requirements_version_id: str
    document_set_revision_id: str
    approval_task_id: str
    updated_at: datetime


class PassportValidationResult(DomainModel):
    passport: ProjectPassport
    findings: tuple[ValidationFinding, ...]
    requirements_version_id: str


class PassportEvidenceCandidateView(DomainModel):
    observation: Observation
    adapter_qualification_id: str | None
    adapter_status: str | None
    adapter_valid_until: date | None = None
    independence_domain: str | None
    eligible: bool
    blockers: tuple[str, ...] = ()


class PassportFactReviewView(DomainModel):
    fact: PassportFactView
    task_status: str
    task_updated_at: datetime
    assigned_role: ActorRole
    decision_allowed: bool
    decision_blockers: tuple[str, ...]


class PassportContextView(DomainModel):
    project_id: str
    project_state: ApprovalState
    document_set_revision_id: str
    requirements_version_id: str
    requirements_content_hash: str
    required_fields: tuple[str, ...]
    independently_verified_fields: tuple[str, ...]
    optional_fields: tuple[str, ...]
    review_role: ActorRole
    selected_field_name: str
    facts: tuple[PassportFactReviewView, ...]
    evidence_candidates: tuple[PassportEvidenceCandidateView, ...]
    candidates_truncated: bool
    validation: PassportValidationResult
    unresolved_conflict_ids: tuple[str, ...]


class PassportFactDecisionCommand(DomainModel):
    decision: ApprovalDecision
    expected_fact_updated_at: datetime
    expected_task_updated_at: datetime

    @field_validator("expected_fact_updated_at", "expected_task_updated_at")
    @classmethod
    def timestamps_are_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Expected timestamps must include a timezone")
        return value


class PassportFactDecisionResult(DomainModel):
    fact: PassportFactView
    validation: PassportValidationResult
    approval_id: str
    decision: ApprovalDecision


class PassportService:
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

    def context(
        self,
        *,
        actor: Actor,
        project_id: str,
        selected_field_name: str | None,
        limit: int,
    ) -> PassportContextView:
        if limit < 1 or limit > 100:
            raise ValueError("Passport context limit must be between 1 and 100")
        project = self._project_service().get_project(
            actor=actor,
            project_id=project_id,
            required_roles=(
                ActorRole.ESTIMATOR,
                ActorRole.TECHNICAL_EXPERT,
                ActorRole.REVIEWER,
                ActorRole.AUDITOR,
            ),
        )
        document_set = self._document_set(
            project_id,
            project.current_document_set_revision_id,
        )
        policy, requirements_row = self._requirements(
            project_id,
            actor.organization_id,
        )
        if selected_field_name is None:
            selected_field_name = sorted(policy.required_fields)[0]
        selected_field_name = selected_field_name.strip()
        if selected_field_name not in policy.declared_fields:
            raise ValueError("Selected passport field is outside the approved requirements")

        fact_rows = tuple(
            self.session.scalars(
                select(ProjectPassportFactRow)
                .where(
                    ProjectPassportFactRow.project_id == project_id,
                    ProjectPassportFactRow.is_current.is_(True),
                )
                .order_by(ProjectPassportFactRow.field_name)
            )
        )
        unresolved_conflicts = self._unresolved_conflicts(
            project_id,
            selected_field_name,
        )
        reviews = tuple(
            self._review_view(
                actor=actor,
                project_state=ApprovalState(project.state),
                row=row,
                policy=policy,
                requirements_row=requirements_row,
                document_set=document_set,
            )
            for row in fact_rows
        )
        candidate_rows = list(
            self.session.scalars(
                select(ObservationRow)
                .where(
                    ObservationRow.project_id == project_id,
                    ObservationRow.document_revision_id.in_(tuple(document_set.revision_ids)),
                    ObservationRow.field_name == selected_field_name,
                )
                .order_by(ObservationRow.created_at.desc(), ObservationRow.id)
                .limit(limit + 1)
            )
        )
        selected_fact_source_ids = {
            str(source_id)
            for row in fact_rows
            if row.field_name == selected_field_name
            for source_id in row.payload.get("observation_ids", [])
        }
        missing_selected_rows = list(
            self.session.scalars(
                select(ObservationRow).where(
                    ObservationRow.project_id == project_id,
                    ObservationRow.id.in_(
                        selected_fact_source_ids.difference({row.id for row in candidate_rows})
                    ),
                )
            )
        )
        visible_rows = {row.id: row for row in (*candidate_rows[:limit], *missing_selected_rows)}
        candidates = tuple(
            self._candidate_view(
                row=row,
                organization_id=actor.organization_id,
                independent=(selected_field_name in policy.independently_verified_fields),
                document_revision_ids=frozenset(document_set.revision_ids),
            )
            for row in sorted(
                visible_rows.values(),
                key=lambda item: (item.created_at, item.id),
                reverse=True,
            )
        )
        validation = self._build_validation(
            project_id,
            policy=policy,
            requirements_version_id=requirements_row.id,
            rows=fact_rows,
        )
        return PassportContextView(
            project_id=project_id,
            project_state=ApprovalState(project.state),
            document_set_revision_id=document_set.id,
            requirements_version_id=requirements_row.id,
            requirements_content_hash=requirements_row.content_hash,
            required_fields=tuple(sorted(policy.required_fields)),
            independently_verified_fields=tuple(sorted(policy.independently_verified_fields)),
            optional_fields=tuple(sorted(policy.optional_fields)),
            review_role=policy.review_role,
            selected_field_name=selected_field_name,
            facts=reviews,
            evidence_candidates=candidates,
            candidates_truncated=len(candidate_rows) > limit,
            validation=validation,
            unresolved_conflict_ids=tuple(row.id for row in unresolved_conflicts),
        )

    def submit_fact(
        self,
        *,
        actor: Actor,
        project_id: str,
        draft: PassportFactDraft,
        expected_document_set_revision_id: str,
        requirements_version_id: str,
        request_id: str,
        reason: str,
    ) -> PassportFactView:
        reason = self._reason(reason)
        project_service = self._project_service()
        project = project_service.get_project(
            actor=actor,
            project_id=project_id,
            lock=True,
            required_roles=(ActorRole.TECHNICAL_EXPERT, ActorRole.REVIEWER),
        )
        if ApprovalState(project.state) not in _EDITABLE_STATES:
            raise ValueError("Passport facts may only change before pricing")
        document_set = self._document_set(
            project_id,
            project.current_document_set_revision_id,
        )
        if document_set.id != expected_document_set_revision_id:
            raise OptimisticLockError(
                "Current document set changed after passport context was loaded"
            )
        policy, requirements_row = self._requirements(
            project_id,
            actor.organization_id,
            expected_version_id=requirements_version_id,
        )
        if draft.field_name not in policy.declared_fields:
            raise ValueError("Passport field is outside the approved requirements")
        if self._unresolved_conflicts(project_id, draft.field_name):
            raise ValueError(
                "Passport fact cannot be submitted while its evidence conflict is unresolved"
            )
        observations = self._observations(
            project_id,
            draft.observation_ids,
            document_revision_ids=frozenset(document_set.revision_ids),
        )
        self._validate_observation_values(draft, observations)
        provenance_leaves = resolve_observation_leaves(
            self.session,
            project_id=project_id,
            observations=observations,
        )
        self._validate_observation_values(
            draft,
            provenance_leaves,
            require_eligible_status=False,
        )
        independence_source_ids: tuple[str, ...] = draft.observation_ids
        if draft.field_name in policy.independently_verified_fields:
            independence_source_ids = require_distinct_qualified_independence(
                self.session,
                project_id=project_id,
                observations=observations,
            )
            if independence_source_ids != tuple(row.id for row in provenance_leaves):
                raise ValueError("Passport independence leaf resolution is inconsistent")
            self._validate_observation_values(draft, provenance_leaves)

        new_fact_id = f"passport-fact-{uuid4()}"
        previous = self.session.scalar(
            select(ProjectPassportFactRow)
            .where(
                ProjectPassportFactRow.project_id == project_id,
                ProjectPassportFactRow.field_name == draft.field_name,
                ProjectPassportFactRow.is_current.is_(True),
            )
            .with_for_update()
        )
        now = utc_now()
        superseded_task_id: str | None = None
        if previous is not None:
            previous.is_current = False
            previous.updated_at = now
            previous_task = self._task_for_fact(previous, lock=True)
            if previous_task is not None and previous_task.status != "SUPERSEDED":
                superseded_task_id = previous_task.id
                previous_task.status = "SUPERSEDED"
                previous_task.payload = {
                    **previous_task.payload,
                    "superseded_at": now.isoformat(),
                    "superseded_by_entity_id": new_fact_id,
                    "supersession_reason": "PASSPORT_FACT_REPLACED",
                }
                previous_task.updated_at = now
        row = ProjectPassportFactRow(
            id=new_fact_id,
            project_id=project_id,
            field_name=draft.field_name,
            status=VerificationStatus.IN_REVIEW.value,
            supersedes_fact_id=previous.id if previous else None,
            is_current=True,
            payload={
                **draft.model_dump(mode="json"),
                "independence_source_ids": list(independence_source_ids),
                "created_by": actor.actor_id,
                "requirements_version_id": requirements_row.id,
                "requirements_content_hash": requirements_row.content_hash,
                "document_set_revision_id": document_set.id,
                "review_role": policy.review_role.value,
            },
            created_at=now,
            updated_at=now,
        )
        task = self._ensure_review_task(
            row=row,
            policy=policy,
            requirements_row=requirements_row,
            document_set=document_set,
        )
        row.payload = {**row.payload, "approval_task_id": task.id}
        self.session.add(row)
        project_service.record_event(
            aggregate_type="project",
            aggregate_id=project_id,
            event_type="passport_fact_submitted",
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload={
                "fact_id": row.id,
                "field_name": row.field_name,
                "value_hash": content_hash(draft.value),
                "observation_ids": list(draft.observation_ids),
                "independence_source_ids": list(independence_source_ids),
                "supersedes_fact_id": row.supersedes_fact_id,
                "superseded_approval_task_id": superseded_task_id,
                "requirements_version_id": requirements_row.id,
                "requirements_content_hash": requirements_row.content_hash,
                "document_set_revision_id": document_set.id,
                "approval_task_id": task.id,
            },
        )
        return self._view(row)

    def decide_fact(
        self,
        *,
        actor: Actor,
        project_id: str,
        fact_id: str,
        command: PassportFactDecisionCommand,
        request_id: str,
        reason: str,
    ) -> PassportFactDecisionResult:
        reason = self._reason(reason)
        project_service = self._project_service()
        project = project_service.get_project(
            actor=actor,
            project_id=project_id,
            lock=True,
            required_roles=(ActorRole.TECHNICAL_EXPERT, ActorRole.REVIEWER),
        )
        if ApprovalState(project.state) not in _EDITABLE_STATES:
            raise ValueError("Passport fact review is not allowed in the current project state")
        row = self.session.scalar(
            select(ProjectPassportFactRow)
            .where(
                ProjectPassportFactRow.id == fact_id,
                ProjectPassportFactRow.project_id == project_id,
                ProjectPassportFactRow.is_current.is_(True),
            )
            .with_for_update()
        )
        if row is None:
            raise LookupError(fact_id)
        expected_fact_updated_at = ensure_utc(command.expected_fact_updated_at)
        assert expected_fact_updated_at is not None
        if ensure_utc(row.updated_at) != expected_fact_updated_at:
            raise OptimisticLockError(
                "Passport fact changed after it was loaded; reload before deciding"
            )
        if row.status != VerificationStatus.IN_REVIEW.value:
            raise ValueError("Only a current IN_REVIEW passport fact can be reviewed")
        document_set = self._document_set(
            project_id,
            project.current_document_set_revision_id,
        )
        if row.payload.get("document_set_revision_id") != document_set.id:
            raise ValueError("Passport fact belongs to a superseded document-set revision")
        requirements_version_id = row.payload.get("requirements_version_id")
        if not isinstance(requirements_version_id, str):
            raise ValueError("Passport fact requirements version is missing")
        policy, requirements_row = self._requirements(
            project_id,
            actor.organization_id,
            expected_version_id=requirements_version_id,
        )
        project_service.get_project(
            actor=actor,
            project_id=project_id,
            required_roles=(policy.review_role,),
        )
        if row.payload.get("created_by") == actor.actor_id:
            raise ValueError("Passport fact review requires a different actor")
        if self._unresolved_conflicts(project_id, row.field_name):
            raise ValueError(
                "Passport fact cannot be reviewed while its evidence conflict is unresolved"
            )
        draft = self._draft(row)
        observations = self._observations(
            project_id,
            draft.observation_ids,
            document_revision_ids=frozenset(document_set.revision_ids),
        )
        self._validate_observation_values(draft, observations)
        provenance_leaves = resolve_observation_leaves(
            self.session,
            project_id=project_id,
            observations=observations,
        )
        self._validate_observation_values(
            draft,
            provenance_leaves,
            require_eligible_status=False,
        )
        independence_source_ids: tuple[str, ...] = draft.observation_ids
        if draft.field_name in policy.independently_verified_fields:
            independence_source_ids = require_distinct_qualified_independence(
                self.session,
                project_id=project_id,
                observations=observations,
            )
            if independence_source_ids != tuple(row.id for row in provenance_leaves):
                raise ValueError("Passport independence leaf resolution is inconsistent")
            self._validate_observation_values(draft, provenance_leaves)
        if tuple(row.payload.get("independence_source_ids", [])) != (independence_source_ids):
            raise ValueError("Passport independence evidence changed after submission")
        task = self._task_for_fact(row, lock=True)
        if task is None:
            raise ValueError("Passport review task is missing")
        expected_task_updated_at = ensure_utc(command.expected_task_updated_at)
        assert expected_task_updated_at is not None
        if ensure_utc(task.updated_at) != expected_task_updated_at:
            raise OptimisticLockError(
                "Passport review task changed after it was loaded; reload before deciding"
            )
        blockers = self._review_blockers(
            actor=actor,
            project_state=ApprovalState(project.state),
            row=row,
            task=task,
            policy=policy,
            requirements_row=requirements_row,
            document_set=document_set,
        )
        if blockers:
            raise ValueError("Passport fact review is blocked: " + ", ".join(blockers))

        now = utc_now()
        approval_id = f"approval-{uuid4()}"
        if command.decision is ApprovalDecision.APPROVED:
            row.status = VerificationStatus.VERIFIED.value
            row.payload = {
                **row.payload,
                "verified_by": actor.actor_id,
                "verified_at": now.isoformat(),
                "reviewed_by": actor.actor_id,
                "reviewed_at": now.isoformat(),
                "review_decision": command.decision.value,
            }
        else:
            row.status = VerificationStatus.REJECTED.value
            row.payload = {
                **row.payload,
                "reviewed_by": actor.actor_id,
                "reviewed_at": now.isoformat(),
                "review_decision": command.decision.value,
            }
        row.updated_at = now
        task.status = command.decision.value
        task.updated_at = now
        approval_payload = {
            "project_id": project_id,
            "fact_id": row.id,
            "expected_fact_updated_at": expected_fact_updated_at.isoformat(),
            "expected_task_updated_at": expected_task_updated_at.isoformat(),
            "evidence_ids": list(draft.observation_ids),
            "independence_source_ids": list(independence_source_ids),
            "requirements_version_id": requirements_row.id,
            "requirements_content_hash": requirements_row.content_hash,
            "document_set_revision_id": document_set.id,
        }
        self.session.add(
            ApprovalRecordRow(
                id=approval_id,
                task_id=task.id,
                decision=command.decision.value,
                decided_by=actor.actor_id,
                reason=reason,
                payload=approval_payload,
                decided_at=now,
            )
        )
        validation = self._validate_current(
            project_id,
            policy=policy,
            requirements_version_id=requirements_row.id,
        )
        project_service.record_event(
            aggregate_type="project",
            aggregate_id=project_id,
            event_type="passport_fact_review_decided",
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload={
                "approval_id": approval_id,
                "approval_task_id": task.id,
                "fact_id": row.id,
                "field_name": row.field_name,
                "decision": command.decision.value,
                **approval_payload,
                "remaining_finding_ids": [
                    list(finding.entity_ids) for finding in validation.findings
                ],
            },
        )
        return PassportFactDecisionResult(
            fact=self._view(row),
            validation=validation,
            approval_id=approval_id,
            decision=command.decision,
        )

    def verify_fact(
        self,
        *,
        actor: Actor,
        project_id: str,
        fact_id: str,
        expected_fact_updated_at: datetime,
        expected_task_updated_at: datetime,
        request_id: str,
        reason: str,
    ) -> tuple[PassportFactView, PassportValidationResult]:
        result = self.decide_fact(
            actor=actor,
            project_id=project_id,
            fact_id=fact_id,
            command=PassportFactDecisionCommand(
                decision=ApprovalDecision.APPROVED,
                expected_fact_updated_at=expected_fact_updated_at,
                expected_task_updated_at=expected_task_updated_at,
            ),
            request_id=request_id,
            reason=reason,
        )
        return result.fact, result.validation

    def validate_current(
        self,
        *,
        actor: Actor,
        project_id: str,
        request_id: str,
        reason: str,
    ) -> PassportValidationResult:
        reason = self._reason(reason)
        project_service = self._project_service()
        project = project_service.get_project(
            actor=actor,
            project_id=project_id,
            lock=True,
            required_roles=(
                ActorRole.ESTIMATOR,
                ActorRole.TECHNICAL_EXPERT,
                ActorRole.REVIEWER,
                ActorRole.AUDITOR,
            ),
        )
        self._document_set(project_id, project.current_document_set_revision_id)
        policy, requirements_row = self._requirements(
            project_id,
            actor.organization_id,
        )
        result = self._validate_current(
            project_id,
            policy=policy,
            requirements_version_id=requirements_row.id,
        )
        project_service.record_event(
            aggregate_type="project",
            aggregate_id=project_id,
            event_type="project_passport_validated",
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload={
                "passport_version": result.passport.passport_version,
                "requirements_version_id": result.requirements_version_id,
                "finding_codes": [finding.code for finding in result.findings],
            },
        )
        return result

    def _validate_current(
        self,
        project_id: str,
        *,
        policy: PassportRequirementsPolicy,
        requirements_version_id: str,
    ) -> PassportValidationResult:
        rows = tuple(
            self.session.scalars(
                select(ProjectPassportFactRow)
                .where(
                    ProjectPassportFactRow.project_id == project_id,
                    ProjectPassportFactRow.is_current.is_(True),
                )
                .order_by(ProjectPassportFactRow.field_name)
            )
        )
        result = self._build_validation(
            project_id,
            policy=policy,
            requirements_version_id=requirements_version_id,
            rows=rows,
        )
        now = utc_now()
        prior = list(
            self.session.scalars(
                select(VerificationFindingRow).where(
                    VerificationFindingRow.project_id == project_id,
                    VerificationFindingRow.contour == "PASSPORT",
                    VerificationFindingRow.resolved.is_(False),
                )
            )
        )
        for old_finding in prior:
            old_finding.resolved = True
            old_finding.updated_at = now
            old_finding.payload = {
                **old_finding.payload,
                "resolved_by_passport_validation": result.passport.passport_version,
                "resolved_at": now.isoformat(),
            }
        for finding in result.findings:
            identity = {
                "project_id": project_id,
                "contour": "PASSPORT",
                "finding": finding,
            }
            finding_id = f"finding-{content_hash(identity)[:24]}"
            existing = self.session.get(VerificationFindingRow, finding_id)
            payload = {
                **finding.model_dump(mode="json"),
                "passport_version": result.passport.passport_version,
                "requirements_version_id": requirements_version_id,
            }
            if existing is None:
                self.session.add(
                    VerificationFindingRow(
                        id=finding_id,
                        project_id=project_id,
                        contour="PASSPORT",
                        code=finding.code.value,
                        severity=Severity.BLOCKER.value,
                        resolved=False,
                        payload=payload,
                        created_at=now,
                        updated_at=now,
                    )
                )
            else:
                existing.resolved = False
                existing.payload = payload
                existing.updated_at = now
        return result

    @staticmethod
    def _build_validation(
        project_id: str,
        *,
        policy: PassportRequirementsPolicy,
        requirements_version_id: str,
        rows: tuple[ProjectPassportFactRow, ...],
    ) -> PassportValidationResult:
        facts = tuple(
            PassportFact(
                field_name=row.field_name,
                value=row.payload.get("value"),
                unit=row.payload.get("unit"),
                observation_ids=tuple(row.payload.get("observation_ids", [])),
                independence_source_ids=tuple(row.payload.get("independence_source_ids", [])),
                status=VerificationStatus(row.status),
            )
            for row in rows
        )
        passport = ProjectPassport(
            project_id=project_id,
            facts=facts,
            passport_version=content_hash(
                {
                    "fact_ids": [row.id for row in rows],
                    "fact_updated_at": [
                        (ensure_utc(row.updated_at) or row.updated_at).isoformat() for row in rows
                    ],
                    "requirements_version_id": requirements_version_id,
                }
            ),
        )
        findings = validate_passport(
            passport,
            required_fields=policy.required_fields,
            independently_verified_fields=policy.independently_verified_fields,
        )
        return PassportValidationResult(
            passport=passport,
            findings=findings,
            requirements_version_id=requirements_version_id,
        )

    def _requirements(
        self,
        project_id: str,
        organization_id: str,
        *,
        expected_version_id: str | None = None,
    ) -> tuple[PassportRequirementsPolicy, ControlledVersionRow]:
        row = require_bound_controlled_version(
            session=self.session,
            settings=self.settings,
            project_id=project_id,
            organization_id=organization_id,
            purpose="document_requirements",
            kind="document_requirements",
            expected_version_id=expected_version_id,
        )
        passport = row.payload.get("passport")
        if not isinstance(passport, dict):
            raise ValueError("Document requirements lack a passport section")
        return PassportRequirementsPolicy.model_validate(passport), row

    def _document_set(
        self,
        project_id: str,
        document_set_revision_id: str | None,
    ) -> DocumentSetRevisionRow:
        return require_confirmed_document_set_integrity(
            session=self.session,
            settings=self.settings,
            project_id=project_id,
            document_set_revision_id=document_set_revision_id,
        )

    def _observations(
        self,
        project_id: str,
        observation_ids: tuple[str, ...],
        *,
        document_revision_ids: frozenset[str],
    ) -> tuple[ObservationRow, ...]:
        rows = list(
            self.session.scalars(
                select(ObservationRow).where(
                    ObservationRow.project_id == project_id,
                    ObservationRow.id.in_(observation_ids),
                )
            )
        )
        by_id = {row.id: row for row in rows}
        if len(by_id) != len(set(observation_ids)):
            raise ValueError("One or more passport evidence observations are missing")
        ordered = tuple(by_id[item] for item in observation_ids)
        if any(row.document_revision_id not in document_revision_ids for row in ordered):
            raise ValueError("Passport evidence must belong to the confirmed current document set")
        return ordered

    @classmethod
    def _validate_observation_values(
        cls,
        draft: PassportFactDraft,
        observations: tuple[ObservationRow, ...],
        *,
        require_eligible_status: bool = True,
    ) -> None:
        value_hash = content_hash(draft.value)
        for row in observations:
            observation = cls._observation(row)
            if require_eligible_status and observation.status not in {
                VerificationStatus.UNVERIFIED,
                VerificationStatus.VERIFIED,
            }:
                raise ValueError("Passport evidence status is not eligible")
            if (
                require_eligible_status
                and observation.method is EvidenceMethod.MANUAL
                and observation.status is not VerificationStatus.VERIFIED
            ):
                raise ValueError("Manual passport evidence requires its dedicated review first")
            if observation.field_name != draft.field_name:
                raise ValueError("Passport evidence belongs to another field")
            if content_hash(observation.value) != value_hash:
                raise ValueError("Passport evidence observations do not reproduce the fact")
            if observation.unit != draft.unit:
                raise ValueError("Passport evidence unit differs from the submitted fact")

    @staticmethod
    def _observation(row: ObservationRow) -> Observation:
        observation = Observation.model_validate(row.payload.get("observation"))
        if (
            row.id != observation.observation_id
            or row.document_revision_id != observation.location.document_revision_id
            or row.field_name != observation.field_name
            or row.method != observation.method.value
            or row.method_version != observation.method_version
            or row.status != observation.status.value
        ):
            raise ValueError("Passport evidence row does not reproduce its payload")
        return observation

    def _candidate_view(
        self,
        *,
        row: ObservationRow,
        organization_id: str,
        independent: bool,
        document_revision_ids: frozenset[str],
    ) -> PassportEvidenceCandidateView:
        blockers: list[str] = []
        try:
            observation = self._observation(row)
        except ValueError:
            raw = row.payload.get("observation")
            observation = Observation.model_validate(raw)
            blockers.append("EVIDENCE_INTEGRITY_FAILED")
        if observation.status not in {
            VerificationStatus.UNVERIFIED,
            VerificationStatus.VERIFIED,
        }:
            blockers.append("EVIDENCE_STATUS_NOT_ELIGIBLE")
        if (
            observation.method is EvidenceMethod.MANUAL
            and observation.status is not VerificationStatus.VERIFIED
        ):
            blockers.append("MANUAL_EVIDENCE_REVIEW_REQUIRED")
        if row.document_revision_id not in document_revision_ids:
            blockers.append("DOCUMENT_SET_CHANGED")
        qualification_id = row.payload.get("adapter_qualification_id")
        qualification = (
            self.session.get(AdapterQualificationRow, qualification_id)
            if isinstance(qualification_id, str)
            else None
        )
        domain = (
            qualification.payload.get("independence_domain") if qualification is not None else None
        )
        if independent:
            if observation.method in {
                EvidenceMethod.MANUAL,
                EvidenceMethod.RULE_ENGINE,
            }:
                blockers.append("INDEPENDENT_AUTOMATIC_SOURCE_REQUIRED")
            if qualification is None:
                blockers.append("QUALIFICATION_MISSING")
            elif (
                qualification.status != "APPROVED"
                or qualification.adapter_version != observation.method_version
                or observation.method.value
                not in qualification.payload.get("supported_methods", [])
                or qualification.payload.get("organization_id") != organization_id
                or qualification.payload.get("service_actor_id") != observation.actor_id
                or not isinstance(domain, str)
                or not domain
            ):
                blockers.append("QUALIFICATION_IDENTITY_FAILED")
            if (
                qualification is not None
                and qualification.valid_until is not None
                and qualification.valid_until < utc_now().date()
            ):
                blockers.append("QUALIFICATION_EXPIRED")
        return PassportEvidenceCandidateView(
            observation=observation,
            adapter_qualification_id=(
                str(qualification_id) if isinstance(qualification_id, str) else None
            ),
            adapter_status=qualification.status if qualification is not None else None,
            adapter_valid_until=(qualification.valid_until if qualification is not None else None),
            independence_domain=domain if isinstance(domain, str) else None,
            eligible=not blockers,
            blockers=tuple(dict.fromkeys(blockers)),
        )

    def _review_view(
        self,
        *,
        actor: Actor,
        project_state: ApprovalState,
        row: ProjectPassportFactRow,
        policy: PassportRequirementsPolicy,
        requirements_row: ControlledVersionRow,
        document_set: DocumentSetRevisionRow,
    ) -> PassportFactReviewView:
        task = self._task_for_fact(row)
        if task is None:
            return PassportFactReviewView(
                fact=self._view(row),
                task_status="MISSING",
                task_updated_at=self._timestamp(row.updated_at),
                assigned_role=policy.review_role,
                decision_allowed=False,
                decision_blockers=("TASK_MISSING",),
            )
        blockers = self._review_blockers(
            actor=actor,
            project_state=project_state,
            row=row,
            task=task,
            policy=policy,
            requirements_row=requirements_row,
            document_set=document_set,
        )
        return PassportFactReviewView(
            fact=self._view(row),
            task_status=task.status,
            task_updated_at=self._timestamp(task.updated_at),
            assigned_role=ActorRole(task.assigned_role),
            decision_allowed=not blockers,
            decision_blockers=tuple(blockers),
        )

    def _review_blockers(
        self,
        *,
        actor: Actor,
        project_state: ApprovalState,
        row: ProjectPassportFactRow,
        task: ApprovalTaskRow,
        policy: PassportRequirementsPolicy,
        requirements_row: ControlledVersionRow,
        document_set: DocumentSetRevisionRow,
    ) -> list[str]:
        blockers: list[str] = []
        if project_state not in _EDITABLE_STATES:
            blockers.append("PROJECT_STATE_NOT_ALLOWED")
        if not row.is_current:
            blockers.append("FACT_SUPERSEDED")
        if row.status != VerificationStatus.IN_REVIEW.value:
            blockers.append("FACT_NOT_IN_REVIEW")
        if row.payload.get("created_by") == actor.actor_id:
            blockers.append("FOUR_EYES_FACT_AUTHOR")
        if policy.review_role not in actor.roles:
            blockers.append("REVIEW_ROLE_REQUIRED")
        if row.payload.get("requirements_version_id") != requirements_row.id:
            blockers.append("REQUIREMENTS_VERSION_CHANGED")
        if row.payload.get("requirements_content_hash") != requirements_row.content_hash:
            blockers.append("REQUIREMENTS_HASH_MISMATCH")
        if row.payload.get("document_set_revision_id") != document_set.id:
            blockers.append("DOCUMENT_SET_CHANGED")
        expected_task_payload = self._task_payload(
            row=row,
            policy=policy,
            requirements_row=requirements_row,
            document_set=document_set,
        )
        if (
            task.project_id != row.project_id
            or task.task_type != "PASSPORT_FACT_REVIEW"
            or task.entity_type != "passport_fact"
            or task.entity_id != row.id
            or task.assigned_role != policy.review_role.value
            or not task.required
            or task.payload != expected_task_payload
        ):
            blockers.append("TASK_INTEGRITY_FAILED")
        if task.status != "PENDING":
            blockers.append("TASK_NOT_PENDING")
        if task.payload.get("created_by") == actor.actor_id:
            blockers.append("FOUR_EYES_TASK_CREATOR")
        if self._unresolved_conflicts(row.project_id, row.field_name):
            blockers.append("UNRESOLVED_EVIDENCE_CONFLICT")
        try:
            draft = self._draft(row)
            observations = self._observations(
                row.project_id,
                draft.observation_ids,
                document_revision_ids=frozenset(document_set.revision_ids),
            )
            self._validate_observation_values(draft, observations)
            provenance_leaves = resolve_observation_leaves(
                self.session,
                project_id=row.project_id,
                observations=observations,
            )
            self._validate_observation_values(
                draft,
                provenance_leaves,
                require_eligible_status=False,
            )
            if draft.field_name in policy.independently_verified_fields:
                leaves = require_distinct_qualified_independence(
                    self.session,
                    project_id=row.project_id,
                    observations=observations,
                )
                if tuple(row.payload.get("independence_source_ids", [])) != leaves:
                    blockers.append("INDEPENDENCE_EVIDENCE_CHANGED")
                self._validate_observation_values(draft, provenance_leaves)
        except (KeyError, LookupError, TypeError, ValueError):
            blockers.append("EVIDENCE_INTEGRITY_FAILED")
        return list(dict.fromkeys(blockers))

    def _ensure_review_task(
        self,
        *,
        row: ProjectPassportFactRow,
        policy: PassportRequirementsPolicy,
        requirements_row: ControlledVersionRow,
        document_set: DocumentSetRevisionRow,
    ) -> ApprovalTaskRow:
        task_id = self._task_id(row.id, requirements_row.id)
        task = ApprovalTaskRow(
            id=task_id,
            project_id=row.project_id,
            task_type="PASSPORT_FACT_REVIEW",
            entity_type="passport_fact",
            entity_id=row.id,
            assigned_role=policy.review_role.value,
            status="PENDING",
            required=True,
            payload=self._task_payload(
                row=row,
                policy=policy,
                requirements_row=requirements_row,
                document_set=document_set,
            ),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        existing = self.session.get(ApprovalTaskRow, task_id)
        if existing is not None:
            raise RuntimeError("Passport review task identifier collision")
        self.session.add(task)
        return task

    @staticmethod
    def _task_payload(
        *,
        row: ProjectPassportFactRow,
        policy: PassportRequirementsPolicy,
        requirements_row: ControlledVersionRow,
        document_set: DocumentSetRevisionRow,
    ) -> dict[str, Any]:
        submission = {
            "field_name": row.field_name,
            "value": row.payload.get("value"),
            "unit": row.payload.get("unit"),
            "observation_ids": row.payload.get("observation_ids", []),
            "independence_source_ids": row.payload.get("independence_source_ids", []),
            "created_by": row.payload.get("created_by"),
            "requirements_version_id": requirements_row.id,
            "requirements_content_hash": requirements_row.content_hash,
            "document_set_revision_id": document_set.id,
            "review_role": policy.review_role.value,
        }
        return {
            "created_by": row.payload.get("created_by"),
            "fact_id": row.id,
            "fact_submission_hash": content_hash(submission),
            "observation_ids": list(row.payload.get("observation_ids", [])),
            "independence_source_ids": list(row.payload.get("independence_source_ids", [])),
            "requirements_version_id": requirements_row.id,
            "requirements_content_hash": requirements_row.content_hash,
            "document_set_revision_id": document_set.id,
            "review_role": policy.review_role.value,
        }

    def _task_for_fact(
        self,
        row: ProjectPassportFactRow,
        *,
        lock: bool = False,
    ) -> ApprovalTaskRow | None:
        task_id = row.payload.get("approval_task_id")
        if not isinstance(task_id, str) or not task_id:
            return None
        statement = select(ApprovalTaskRow).where(
            ApprovalTaskRow.id == task_id,
            ApprovalTaskRow.project_id == row.project_id,
            ApprovalTaskRow.entity_id == row.id,
        )
        if lock:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def _unresolved_conflicts(
        self,
        project_id: str,
        field_name: str,
    ) -> tuple[ConflictRow, ...]:
        return tuple(
            self.session.scalars(
                select(ConflictRow).where(
                    ConflictRow.project_id == project_id,
                    ConflictRow.field_name == field_name,
                    ConflictRow.status != VerificationStatus.VERIFIED.value,
                )
            )
        )

    @staticmethod
    def _draft(row: ProjectPassportFactRow) -> PassportFactDraft:
        return PassportFactDraft.model_validate(
            {
                "field_name": row.field_name,
                "value": row.payload.get("value"),
                "unit": row.payload.get("unit"),
                "observation_ids": row.payload.get("observation_ids"),
            }
        )

    @staticmethod
    def _task_id(fact_id: str, requirements_version_id: str) -> str:
        return (
            "approval-task-passport-"
            + content_hash(
                {
                    "fact_id": fact_id,
                    "requirements_version_id": requirements_version_id,
                }
            )[:24]
        )

    @staticmethod
    def _reason(reason: str) -> str:
        normalized = reason.strip()
        if not normalized or len(normalized) > 2000:
            raise ValueError("Passport workflow reason must contain 1 to 2000 characters")
        return normalized

    @staticmethod
    def _timestamp(value: datetime) -> datetime:
        normalized = ensure_utc(value)
        if normalized is None:
            raise ValueError("Passport workflow timestamp is missing")
        return normalized

    def _project_service(self) -> ProjectService:
        return ProjectService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        )

    @staticmethod
    def _view(row: ProjectPassportFactRow) -> PassportFactView:
        requirements_version_id = row.payload.get("requirements_version_id")
        document_set_revision_id = row.payload.get("document_set_revision_id")
        approval_task_id = row.payload.get("approval_task_id")
        created_by = row.payload.get("created_by")
        if not all(
            isinstance(item, str) and item
            for item in (
                requirements_version_id,
                document_set_revision_id,
                approval_task_id,
                created_by,
            )
        ):
            raise ValueError("Passport fact provenance is incomplete")
        return PassportFactView(
            fact_id=row.id,
            field_name=row.field_name,
            value=row.payload.get("value"),
            unit=row.payload.get("unit"),
            observation_ids=tuple(row.payload.get("observation_ids", [])),
            independence_source_ids=tuple(row.payload.get("independence_source_ids", [])),
            status=VerificationStatus(row.status),
            supersedes_fact_id=row.supersedes_fact_id,
            is_current=row.is_current,
            created_by=str(created_by),
            verified_by=row.payload.get("verified_by"),
            reviewed_by=row.payload.get("reviewed_by"),
            requirements_version_id=str(requirements_version_id),
            document_set_revision_id=str(document_set_revision_id),
            approval_task_id=str(approval_task_id),
            updated_at=PassportService._timestamp(row.updated_at),
        )
