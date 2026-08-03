from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from pydantic import Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from tenderguard.application.controlled_version_integrity import (
    require_bound_controlled_version,
    require_controlled_version_integrity,
)
from tenderguard.application.document_set_integrity import (
    require_confirmed_document_set_integrity,
)
from tenderguard.application.projects import (
    OptimisticLockError,
    ProjectService,
    SystemProjectAccess,
)
from tenderguard.config import Settings
from tenderguard.domain.common import content_hash, ensure_utc, utc_now
from tenderguard.domain.enums import (
    ActorRole,
    ApprovalDecision,
    ApprovalState,
    EvidenceMethod,
    VerificationStatus,
)
from tenderguard.domain.models import (
    Conflict,
    DomainModel,
    EvidenceLocation,
    Observation,
)
from tenderguard.domain.reconciliation import reconcile_observations
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.object_store import ObjectStore
from tenderguard.infrastructure.orm import (
    AdapterQualificationRow,
    ApprovalRecordRow,
    ApprovalTaskRow,
    ConflictRow,
    ControlledVersionRow,
    DocumentRevisionRow,
    DocumentRow,
    DocumentSetRevisionRow,
    ObservationRow,
    ProjectControlledVersionRow,
    ProjectRow,
)


class ObservationDraft(DomainModel):
    field_name: str = Field(min_length=1, max_length=300)
    value: Any
    unit: str | None = Field(default=None, max_length=100)
    method: EvidenceMethod
    method_version: str = Field(min_length=1, max_length=200)
    source_priority: int = Field(ge=0)
    location: EvidenceLocation
    observed_at: datetime
    confidence: Decimal | None = Field(default=None, ge=0, le=1)
    adapter_qualification_id: str | None = None
    basis_metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator("basis_metadata")
    @classmethod
    def basis_metadata_is_closed_and_normalized(
        cls,
        value: dict[str, str],
    ) -> dict[str, str]:
        if not value:
            return {}
        allowed = {"basis_type", "currency", "unit"}
        unexpected = set(value) - allowed
        if unexpected:
            raise ValueError(
                "Unsupported evidence basis metadata: " + ", ".join(sorted(unexpected))
            )
        normalized = {key: item.strip() for key, item in value.items()}
        if any(not item for item in normalized.values()):
            raise ValueError("Evidence basis metadata values cannot be blank")
        if set(normalized) != allowed:
            raise ValueError("Normalized price evidence requires basis_type, currency, and unit")
        if normalized["basis_type"] != "NORMALIZED_PRICE":
            raise ValueError("Only NORMALIZED_PRICE commercial evidence is supported")
        currency = normalized["currency"]
        if len(currency) != 3 or not currency.isascii() or not currency.isalpha():
            raise ValueError("Evidence currency must be a three-letter ISO code")
        normalized["currency"] = currency.upper()
        return normalized

    @model_validator(mode="after")
    def normalized_price_matches_observation(self) -> ObservationDraft:
        if not self.basis_metadata:
            return self
        if self.unit != self.basis_metadata["unit"]:
            raise ValueError("Normalized price evidence unit must match the observation unit")
        try:
            rate = Decimal(str(self.value))
        except (InvalidOperation, TypeError, ValueError) as error:
            raise ValueError("Normalized price evidence value must be a decimal rate") from error
        if not rate.is_finite() or rate <= 0:
            raise ValueError("Normalized price evidence rate must be finite and positive")
        return self


class ReconciliationOutcome(DomainModel):
    agreed_value: Any | None = None
    verified_observation_id: str | None = None
    conflict: Conflict | None = None


class ReconciliationCandidateView(DomainModel):
    observation: Observation
    adapter_qualification_id: str | None
    adapter_status: str | None
    adapter_valid_until: date | None
    independence_domain: str | None
    eligible: bool
    blockers: tuple[str, ...] = ()


class ReconciliationContextView(DomainModel):
    project_id: str
    document_set_revision_id: str
    reconciliation_version_id: str
    available_field_names: tuple[str, ...]
    field_names_truncated: bool
    selected_field_name: str | None = None
    candidates: tuple[ReconciliationCandidateView, ...] = ()
    candidates_truncated: bool = False


class ConflictResolutionCommand(DomainModel):
    selected_observation_id: str = Field(min_length=1, max_length=64)
    resolution_reason: str = Field(min_length=1, max_length=2000)
    expected_conflict_updated_at: datetime
    expected_task_updated_at: datetime

    @field_validator("expected_conflict_updated_at", "expected_task_updated_at")
    @classmethod
    def timestamps_are_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Expected timestamps must include a timezone")
        return value


class ConflictResolutionResult(DomainModel):
    conflict: Conflict
    verified_observation: Observation


class ManualEvidencePolicy(DomainModel):
    review_role: ActorRole
    allowed_project_states: frozenset[ApprovalState] = Field(min_length=1)

    @model_validator(mode="after")
    def reviewer_is_an_independent_technical_role(self) -> ManualEvidencePolicy:
        if self.review_role not in {
            ActorRole.REVIEWER,
            ActorRole.TECHNICAL_EXPERT,
        }:
            raise ValueError("Manual evidence review role must be REVIEWER or TECHNICAL_EXPERT")
        return self


class ManualEvidenceDocumentView(DomainModel):
    document_id: str
    document_revision_id: str
    title: str
    revision_label: str
    original_filename: str
    original_object_hash: str


class ManualEvidenceContextView(DomainModel):
    project_id: str
    project_state: ApprovalState
    document_set_revision_id: str
    policy_version_id: str
    review_role: ActorRole
    allowed_project_states: tuple[ApprovalState, ...]
    documents: tuple[ManualEvidenceDocumentView, ...]


class ManualEvidenceDecisionCommand(DomainModel):
    decision: ApprovalDecision
    reason: str = Field(min_length=1, max_length=4000)
    expected_task_updated_at: datetime

    @field_validator("expected_task_updated_at")
    @classmethod
    def task_timestamp_is_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Expected task timestamp must include a timezone")
        return value


class ManualEvidenceReviewView(DomainModel):
    source_observation: Observation
    source_observation_hash: str
    submission_reason: str
    task_id: str
    task_status: str
    task_updated_at: datetime
    task_created_by: str
    policy_version_id: str
    document_set_revision_id: str
    review_role: ActorRole
    decision_allowed: bool
    decision_blockers: tuple[str, ...]
    verified_observation_id: str | None = None


class ManualEvidenceDecisionResult(DomainModel):
    review: ManualEvidenceReviewView
    approval_id: str
    decision: ApprovalDecision
    verified_observation: Observation | None = None


class ConflictObservationView(Observation):
    adapter_qualification_id: str | None = None
    adapter_qualification_status: str | None = None
    adapter_qualification_valid_until: date | None = None
    independence_domain: str | None = None
    basis_metadata: dict[str, str] = Field(default_factory=dict)


class ConflictReviewView(DomainModel):
    conflict: Conflict
    conflict_updated_at: datetime
    observations: tuple[ConflictObservationView, ...]
    missing_observation_ids: tuple[str, ...] = ()
    task_id: str
    task_status: str | None
    task_required: bool | None
    task_updated_at: datetime | None
    task_created_by: str | None
    resolution_allowed: bool
    resolution_blockers: tuple[str, ...] = ()


class EvidenceService:
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

    def record_observation(
        self,
        *,
        actor: Actor,
        project_id: str,
        draft: ObservationDraft,
        request_id: str,
        reason: str,
        upstream_observation_ids: tuple[str, ...] = (),
    ) -> Observation:
        reason = reason.strip()
        if not reason or len(reason) > 2000:
            raise ValueError("Evidence observation reason must contain 1 to 2000 characters")
        project_service = ProjectService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        )
        if ActorRole.SYSTEM in actor.roles:
            project = project_service.get_project(
                actor=actor,
                project_id=project_id,
                lock=True,
                system_access=SystemProjectAccess(
                    qualification_id=draft.adapter_qualification_id or "",
                    capability=draft.method.value,
                ),
            )
        else:
            project = project_service.get_project(
                actor=actor,
                project_id=project_id,
                lock=True,
                required_roles=(ActorRole.TECHNICAL_EXPERT, ActorRole.REVIEWER),
            )
        revision = self.session.scalar(
            select(DocumentRevisionRow)
            .join(DocumentRow, DocumentRow.id == DocumentRevisionRow.document_id)
            .where(
                DocumentRevisionRow.id == draft.location.document_revision_id,
                DocumentRow.id == draft.location.document_id,
                DocumentRow.project_id == project_id,
            )
        )
        if revision is None:
            raise ValueError("Evidence location is not part of this project")
        if draft.location.original_object_hash != revision.object_hash:
            raise ValueError("Evidence location object hash does not match document revision")
        self._validate_adapter(actor, draft)
        manual_policy_row: ControlledVersionRow | None = None
        manual_policy: ManualEvidencePolicy | None = None
        document_set: DocumentSetRevisionRow | None = None
        if len(upstream_observation_ids) != len(set(upstream_observation_ids)):
            raise ValueError("Upstream evidence observation IDs must be unique")
        if upstream_observation_ids and draft.method is not EvidenceMethod.MANUAL:
            raise ValueError("Only governed manual evidence may declare upstream observations")
        if draft.method is EvidenceMethod.MANUAL:
            manual_policy_row, manual_policy = self._manual_evidence_policy(
                project_id=project_id,
                organization_id=actor.organization_id,
            )
            if ApprovalState(project.state) not in manual_policy.allowed_project_states:
                raise ValueError(
                    "Manual evidence is not allowed in the current project state by policy"
                )
            if draft.method_version != manual_policy_row.id:
                raise ValueError(
                    "Manual observation method version must equal the bound policy version"
                )
            document_set = self._confirmed_current_document_set(
                project_id=project_id,
                document_set_revision_id=project.current_document_set_revision_id,
            )
            if revision.id not in document_set.revision_ids:
                raise ValueError(
                    "Manual evidence must point to a revision in the confirmed current document set"
                )
        upstream_lineage = self._validated_upstream_lineage(
            project_id=project_id,
            document_set=document_set,
            observation_ids=upstream_observation_ids,
        )
        identity = {
            "project_id": project_id,
            "field_name": draft.field_name,
            "value": draft.value,
            "unit": draft.unit,
            "method": draft.method,
            "method_version": draft.method_version,
            "location": draft.location,
            "observed_at": draft.observed_at,
            "actor_id": actor.actor_id,
            "basis_metadata": draft.basis_metadata,
            **({"upstream_observations": upstream_lineage} if upstream_lineage else {}),
        }
        observation = Observation(
            observation_id=f"observation-{content_hash(identity)[:24]}",
            field_name=draft.field_name,
            value=draft.value,
            unit=draft.unit,
            method=draft.method,
            method_version=draft.method_version,
            source_priority=draft.source_priority,
            location=draft.location,
            observed_at=draft.observed_at,
            actor_id=actor.actor_id,
            confidence=draft.confidence,
            status=VerificationStatus.UNVERIFIED,
        )
        payload: dict[str, Any] = {
            **draft.basis_metadata,
            "observation": observation.model_dump(mode="json"),
            "adapter_qualification_id": draft.adapter_qualification_id,
            **({"manual_reason": reason} if draft.method is EvidenceMethod.MANUAL else {}),
            **({"upstream_observations": upstream_lineage} if upstream_lineage else {}),
        }
        existing = self.session.get(ObservationRow, observation.observation_id)
        if existing is not None:
            if (
                existing.project_id != project_id
                or existing.document_revision_id != revision.id
                or existing.field_name != observation.field_name
                or existing.method != observation.method.value
                or existing.method_version != observation.method_version
                or existing.status != observation.status.value
                or existing.payload != payload
            ):
                raise RuntimeError("Existing observation does not reproduce its identity")
            if (
                manual_policy_row is not None
                and manual_policy is not None
                and document_set is not None
            ):
                self._ensure_manual_evidence_task(
                    source=existing,
                    policy_row=manual_policy_row,
                    policy=manual_policy,
                    document_set=document_set,
                    created_by=actor.actor_id,
                )
            return Observation.model_validate(existing.payload["observation"])
        source_row = ObservationRow(
            id=observation.observation_id,
            project_id=project_id,
            document_revision_id=revision.id,
            field_name=observation.field_name,
            method=observation.method.value,
            method_version=observation.method_version,
            status=observation.status.value,
            payload=payload,
            created_at=utc_now(),
        )
        self.session.add(source_row)
        if manual_policy_row is not None and manual_policy is not None and document_set is not None:
            task = self._ensure_manual_evidence_task(
                source=source_row,
                policy_row=manual_policy_row,
                policy=manual_policy,
                document_set=document_set,
                created_by=actor.actor_id,
            )
        else:
            task = None
        project_service.record_event(
            aggregate_type="project",
            aggregate_id=project_id,
            event_type="evidence_observation_recorded",
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload={
                "observation_id": observation.observation_id,
                "document_revision_id": revision.id,
                "field_name": observation.field_name,
                "method": observation.method,
                "method_version": observation.method_version,
                "adapter_qualification_id": draft.adapter_qualification_id,
                "manual_evidence_review_task_id": task.id if task is not None else None,
                "upstream_observation_ids": [item["observation_id"] for item in upstream_lineage],
            },
        )
        return observation

    def manual_evidence_context(
        self,
        *,
        actor: Actor,
        project_id: str,
    ) -> ManualEvidenceContextView:
        project_service = ProjectService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        )
        project = project_service.get_project(
            actor=actor,
            project_id=project_id,
            required_roles=(ActorRole.TECHNICAL_EXPERT, ActorRole.REVIEWER),
        )
        policy_row, policy = self._manual_evidence_policy(
            project_id=project_id,
            organization_id=actor.organization_id,
        )
        document_set = self._confirmed_current_document_set(
            project_id=project_id,
            document_set_revision_id=project.current_document_set_revision_id,
        )
        revisions = list(
            self.session.execute(
                select(DocumentRevisionRow, DocumentRow)
                .join(DocumentRow, DocumentRow.id == DocumentRevisionRow.document_id)
                .where(
                    DocumentRow.project_id == project_id,
                    DocumentRevisionRow.id.in_(tuple(document_set.revision_ids)),
                )
            ).all()
        )
        revisions_by_id = {revision.id: (revision, document) for revision, document in revisions}
        if set(revisions_by_id) != set(document_set.revision_ids):
            raise RuntimeError("Confirmed document set contains a missing project revision")
        documents = tuple(
            ManualEvidenceDocumentView(
                document_id=revisions_by_id[revision_id][1].id,
                document_revision_id=revision_id,
                title=revisions_by_id[revision_id][1].title,
                revision_label=revisions_by_id[revision_id][0].revision_label,
                original_filename=revisions_by_id[revision_id][0].original_filename,
                original_object_hash=revisions_by_id[revision_id][0].object_hash,
            )
            for revision_id in document_set.revision_ids
        )
        return ManualEvidenceContextView(
            project_id=project_id,
            project_state=ApprovalState(project.state),
            document_set_revision_id=document_set.id,
            policy_version_id=policy_row.id,
            review_role=policy.review_role,
            allowed_project_states=tuple(
                sorted(policy.allowed_project_states, key=lambda state: state.value)
            ),
            documents=documents,
        )

    def require_verified_manual_derivation(
        self,
        *,
        project_id: str,
        observation_id: str,
    ) -> Observation:
        """Replay the complete current manual-review chain before evidence use."""

        project = self.session.get(ProjectRow, project_id)
        if project is None:
            raise LookupError(project_id)
        derived = self.session.scalar(
            select(ObservationRow).where(
                ObservationRow.id == observation_id,
                ObservationRow.project_id == project_id,
            )
        )
        if derived is None:
            raise LookupError(observation_id)
        observation = Observation.model_validate(derived.payload.get("observation"))
        if (
            derived.payload.get("derivation_type") != "MANUAL_EVIDENCE_REVIEW"
            or derived.id != observation.observation_id
            or derived.document_revision_id != observation.location.document_revision_id
            or derived.field_name != observation.field_name
            or derived.method != EvidenceMethod.RULE_ENGINE.value
            or observation.method is not EvidenceMethod.RULE_ENGINE
            or derived.method_version != observation.method_version
            or derived.status != VerificationStatus.VERIFIED.value
            or observation.status is not VerificationStatus.VERIFIED
        ):
            raise RuntimeError("Verified manual evidence derivation identity does not reproduce")
        policy_row, policy = self._manual_evidence_policy(
            project_id=project_id,
            organization_id=project.organization_id,
        )
        document_set = self._confirmed_current_document_set(
            project_id=project_id,
            document_set_revision_id=project.current_document_set_revision_id,
        )
        source_ids = derived.payload.get("source_observation_ids")
        if not (
            isinstance(source_ids, list) and len(source_ids) == 1 and isinstance(source_ids[0], str)
        ):
            raise RuntimeError("Verified manual evidence has invalid direct-source lineage")
        source = self.session.get(ObservationRow, source_ids[0])
        if source is None or source.project_id != project_id:
            raise RuntimeError("Verified manual evidence source is unavailable")
        source_observation = Observation.model_validate(source.payload.get("observation"))
        source_hash = content_hash(source.payload)
        if (
            source.id != source_observation.observation_id
            or source.document_revision_id != source_observation.location.document_revision_id
            or source.document_revision_id not in document_set.revision_ids
            or source.field_name != source_observation.field_name
            or source.field_name != derived.field_name
            or source.method != EvidenceMethod.MANUAL.value
            or source_observation.method is not EvidenceMethod.MANUAL
            or source.method_version != policy_row.id
            or source_observation.method_version != policy_row.id
            or source.status != VerificationStatus.UNVERIFIED.value
            or source_observation.status is not VerificationStatus.UNVERIFIED
            or not isinstance(source.payload.get("manual_reason"), str)
            or not source.payload["manual_reason"].strip()
            or derived.method_version != policy_row.id
            or derived.document_revision_id != source.document_revision_id
            or derived.payload.get("source_observation_hash") != source_hash
            or derived.payload.get("manual_evidence_policy_version_id") != policy_row.id
            or derived.payload.get("document_set_revision_id") != document_set.id
            or derived.payload.get("upstream_observations")
            != source.payload.get("upstream_observations")
        ):
            raise RuntimeError("Verified manual evidence source chain does not reproduce")
        upstream_blockers = self._manual_upstream_lineage_blockers(
            source=source,
            document_set=document_set,
        )
        if upstream_blockers:
            raise RuntimeError(
                "Verified manual evidence upstream chain is invalid: "
                + ", ".join(upstream_blockers)
            )
        task_id = derived.payload.get("approval_task_id")
        approval_id = derived.payload.get("approval_record_id")
        if not isinstance(task_id, str) or not isinstance(approval_id, str):
            raise RuntimeError("Verified manual evidence approval identity is missing")
        task = self.session.get(ApprovalTaskRow, task_id)
        approval = self.session.get(ApprovalRecordRow, approval_id)
        expected_task_payload = {
            "created_by": source_observation.actor_id,
            "source_observation_id": source.id,
            "source_observation_hash": source_hash,
            "observation_ids": [source.id],
            "policy_version_id": policy_row.id,
            "policy_content_hash": policy_row.content_hash,
            "document_set_revision_id": document_set.id,
            "document_revision_id": source.document_revision_id,
            "review_role": policy.review_role.value,
        }
        if (
            task is None
            or task.project_id != project_id
            or task.task_type != "MANUAL_EVIDENCE_REVIEW"
            or task.entity_type != "evidence_observation"
            or task.entity_id != source.id
            or task.assigned_role != policy.review_role.value
            or not task.required
            or task.status != ApprovalDecision.APPROVED.value
            or task.payload != expected_task_payload
            or approval is None
            or approval.task_id != task.id
            or approval.decision != ApprovalDecision.APPROVED.value
            or approval.decided_by != observation.actor_id
            or approval.decided_by == source_observation.actor_id
            or approval.decided_by == task.payload.get("created_by")
        ):
            raise RuntimeError("Verified manual evidence approval chain does not reproduce")
        upstream = source.payload.get("upstream_observations")
        expected_evidence_ids = [source.id]
        if isinstance(upstream, list):
            expected_evidence_ids.extend(str(item["observation_id"]) for item in upstream)
        expected_evidence_ids.append(derived.id)
        task_created_at = ensure_utc(task.created_at)
        if (
            approval.payload.get("project_id") != project_id
            or approval.payload.get("evidence_ids") != expected_evidence_ids
            or approval.payload.get("source_observation_hash") != source_hash
            or approval.payload.get("policy_version_id") != policy_row.id
            or approval.payload.get("document_set_revision_id") != document_set.id
            or approval.payload.get("verified_observation_id") != derived.id
            or task_created_at is None
            or approval.payload.get("expected_task_updated_at") != task_created_at.isoformat()
            or not isinstance(approval.reason, str)
            or not approval.reason.strip()
            or len(approval.reason) > 2000
            or ensure_utc(task.updated_at) != ensure_utc(approval.decided_at)
            or ensure_utc(approval.decided_at) != ensure_utc(derived.created_at)
            or ensure_utc(approval.decided_at) != ensure_utc(observation.observed_at)
        ):
            raise RuntimeError("Verified manual evidence decision payload does not reproduce")
        expected_observation = source_observation.model_copy(
            update={
                "observation_id": derived.id,
                "method": EvidenceMethod.RULE_ENGINE,
                "method_version": policy_row.id,
                "observed_at": observation.observed_at,
                "actor_id": approval.decided_by,
                "confidence": None,
                "status": VerificationStatus.VERIFIED,
            }
        )
        if observation != expected_observation:
            raise RuntimeError("Verified manual evidence derived content does not reproduce")
        return observation

    def manual_evidence_review(
        self,
        *,
        actor: Actor,
        project_id: str,
        observation_id: str,
        lock: bool = False,
    ) -> ManualEvidenceReviewView:
        project_service = ProjectService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        )
        project = project_service.get_project(
            actor=actor,
            project_id=project_id,
            lock=lock,
        )
        source_statement = select(ObservationRow).where(
            ObservationRow.id == observation_id,
            ObservationRow.project_id == project_id,
        )
        if lock:
            source_statement = source_statement.with_for_update()
        source = self.session.scalar(source_statement)
        if source is None:
            raise LookupError(observation_id)
        observation = Observation.model_validate(source.payload.get("observation"))
        if (
            source.method != EvidenceMethod.MANUAL.value
            or observation.method is not EvidenceMethod.MANUAL
            or source.status != VerificationStatus.UNVERIFIED.value
            or observation.status is not VerificationStatus.UNVERIFIED
        ):
            raise ValueError("Only an immutable UNVERIFIED manual observation can be reviewed")
        policy_row, policy = self._manual_evidence_policy(
            project_id=project_id,
            organization_id=actor.organization_id,
        )
        project_service.get_project(
            actor=actor,
            project_id=project_id,
            required_roles=(policy.review_role,),
        )
        document_set = self._confirmed_current_document_set(
            project_id=project_id,
            document_set_revision_id=project.current_document_set_revision_id,
        )
        task_id = self._manual_evidence_task_id(source.id, policy_row.id)
        task_statement = select(ApprovalTaskRow).where(
            ApprovalTaskRow.id == task_id,
            ApprovalTaskRow.project_id == project_id,
            ApprovalTaskRow.task_type == "MANUAL_EVIDENCE_REVIEW",
            ApprovalTaskRow.entity_type == "evidence_observation",
            ApprovalTaskRow.entity_id == source.id,
        )
        if lock:
            task_statement = task_statement.with_for_update()
        task = self.session.scalar(task_statement)
        if task is None:
            raise RuntimeError("Manual observation has no dedicated review task")
        blockers = self._manual_evidence_review_blockers(
            actor=actor,
            project_state=ApprovalState(project.state),
            source=source,
            observation=observation,
            task=task,
            policy_row=policy_row,
            policy=policy,
            document_set=document_set,
        )
        blockers.extend(
            self._manual_upstream_lineage_blockers(
                source=source,
                document_set=document_set,
            )
        )
        verified_observation_id = None
        decisions = tuple(
            self.session.scalars(
                select(ApprovalRecordRow)
                .where(ApprovalRecordRow.task_id == task.id)
                .order_by(ApprovalRecordRow.decided_at.desc(), ApprovalRecordRow.id.desc())
            )
        )
        if decisions:
            candidate = decisions[0].payload.get("verified_observation_id")
            if isinstance(candidate, str) and candidate:
                verified_observation_id = candidate
        task_updated_at = ensure_utc(task.updated_at)
        if task_updated_at is None:
            raise RuntimeError("Manual evidence task update timestamp is missing")
        return ManualEvidenceReviewView(
            source_observation=observation,
            source_observation_hash=content_hash(source.payload),
            submission_reason=str(source.payload.get("manual_reason", "")),
            task_id=task.id,
            task_status=task.status,
            task_updated_at=task_updated_at,
            task_created_by=str(task.payload.get("created_by")),
            policy_version_id=policy_row.id,
            document_set_revision_id=document_set.id,
            review_role=policy.review_role,
            decision_allowed=not blockers,
            decision_blockers=tuple(blockers),
            verified_observation_id=verified_observation_id,
        )

    def decide_manual_evidence(
        self,
        *,
        actor: Actor,
        project_id: str,
        observation_id: str,
        command: ManualEvidenceDecisionCommand,
        request_id: str,
    ) -> ManualEvidenceDecisionResult:
        reason = command.reason.strip()
        if not reason:
            raise ValueError("Manual evidence decision reason is required")
        review = self.manual_evidence_review(
            actor=actor,
            project_id=project_id,
            observation_id=observation_id,
            lock=True,
        )
        if review.decision_blockers:
            raise ValueError(
                "Manual evidence review is blocked: " + ", ".join(review.decision_blockers)
            )
        expected_task_updated_at = ensure_utc(command.expected_task_updated_at)
        assert expected_task_updated_at is not None
        if ensure_utc(review.task_updated_at) != expected_task_updated_at:
            raise OptimisticLockError(
                "Manual evidence review task changed after it was loaded; reload before deciding"
            )
        source = self.session.get(ObservationRow, observation_id)
        task = self.session.get(ApprovalTaskRow, review.task_id)
        assert source is not None
        assert task is not None
        source_observation = Observation.model_validate(source.payload["observation"])
        now = utc_now()
        approval_id = f"approval-{uuid4()}"
        verified: Observation | None = None
        if command.decision is ApprovalDecision.APPROVED:
            verified_id = (
                "observation-"
                + content_hash(
                    {
                        "source_observation_id": source.id,
                        "source_observation_hash": review.source_observation_hash,
                        "policy_version_id": review.policy_version_id,
                        "document_set_revision_id": review.document_set_revision_id,
                        "approval_task_id": task.id,
                    }
                )[:24]
            )
            verified = source_observation.model_copy(
                update={
                    "observation_id": verified_id,
                    "method": EvidenceMethod.RULE_ENGINE,
                    "method_version": review.policy_version_id,
                    "observed_at": now,
                    "actor_id": actor.actor_id,
                    "confidence": None,
                    "status": VerificationStatus.VERIFIED,
                }
            )
            existing = self.session.get(ObservationRow, verified_id)
            if existing is not None:
                raise RuntimeError("Manual evidence review already produced a derived observation")
            basis_metadata = self._validated_basis_metadata(source, source_observation)
            upstream_lineage = source.payload.get("upstream_observations")
            self.session.add(
                ObservationRow(
                    id=verified.observation_id,
                    project_id=project_id,
                    document_revision_id=source.document_revision_id,
                    field_name=verified.field_name,
                    method=verified.method.value,
                    method_version=verified.method_version,
                    status=verified.status.value,
                    payload={
                        "observation": verified.model_dump(mode="json"),
                        "source_observation_ids": [source.id],
                        "source_observation_hash": review.source_observation_hash,
                        "derivation_type": "MANUAL_EVIDENCE_REVIEW",
                        "manual_evidence_policy_version_id": review.policy_version_id,
                        "document_set_revision_id": review.document_set_revision_id,
                        "approval_task_id": task.id,
                        "approval_record_id": approval_id,
                        **({"upstream_observations": upstream_lineage} if upstream_lineage else {}),
                        **basis_metadata,
                    },
                    created_at=now,
                )
            )
        task.status = command.decision.value
        task.updated_at = now
        upstream_lineage = source.payload.get("upstream_observations")
        evidence_ids = [source.id]
        if isinstance(upstream_lineage, list):
            evidence_ids.extend(
                str(item["observation_id"])
                for item in upstream_lineage
                if isinstance(item, dict) and isinstance(item.get("observation_id"), str)
            )
        if verified is not None:
            evidence_ids.append(verified.observation_id)
        self.session.add(
            ApprovalRecordRow(
                id=approval_id,
                task_id=task.id,
                decision=command.decision.value,
                decided_by=actor.actor_id,
                reason=reason,
                payload={
                    "project_id": project_id,
                    "evidence_ids": evidence_ids,
                    "source_observation_hash": review.source_observation_hash,
                    "policy_version_id": review.policy_version_id,
                    "document_set_revision_id": review.document_set_revision_id,
                    "expected_task_updated_at": expected_task_updated_at.isoformat(),
                    "verified_observation_id": (
                        verified.observation_id if verified is not None else None
                    ),
                },
                decided_at=now,
            )
        )
        ProjectService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        ).record_event(
            aggregate_type="project",
            aggregate_id=project_id,
            event_type="manual_evidence_review_decided",
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload={
                "source_observation_id": source.id,
                "source_observation_hash": review.source_observation_hash,
                "decision": command.decision,
                "verified_observation_id": (
                    verified.observation_id if verified is not None else None
                ),
                "approval_task_id": task.id,
                "approval_record_id": approval_id,
                "policy_version_id": review.policy_version_id,
                "document_set_revision_id": review.document_set_revision_id,
                "upstream_observation_ids": evidence_ids[1:-1] if verified else evidence_ids[1:],
            },
        )
        final_review = review.model_copy(
            update={
                "task_status": command.decision.value,
                "task_updated_at": now,
                "decision_allowed": False,
                "decision_blockers": ("TASK_NOT_PENDING",),
                "verified_observation_id": (
                    verified.observation_id if verified is not None else None
                ),
            }
        )
        return ManualEvidenceDecisionResult(
            review=final_review,
            approval_id=approval_id,
            decision=command.decision,
            verified_observation=verified,
        )

    def reconcile(
        self,
        *,
        actor: Actor,
        project_id: str,
        observation_ids: tuple[str, ...],
        reconciliation_version_id: str,
        request_id: str,
        reason: str,
    ) -> ReconciliationOutcome:
        reason = reason.strip()
        if not reason or len(reason) > 2000:
            raise ValueError("Reconciliation reason must contain 1 to 2000 characters")
        if len(observation_ids) != len(set(observation_ids)):
            raise ValueError("Reconciliation observation IDs must be unique")
        project_service = ProjectService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        )
        project = project_service.get_project(
            actor=actor,
            project_id=project_id,
            lock=True,
            required_roles=(ActorRole.REVIEWER, ActorRole.TECHNICAL_EXPERT),
        )
        rule_version = require_bound_controlled_version(
            session=self.session,
            settings=self.settings,
            project_id=project_id,
            organization_id=actor.organization_id,
            purpose="reconciliation_rules",
            kind="reconciliation_rules",
            expected_version_id=reconciliation_version_id,
        )
        reconciliation_version_id = rule_version.id
        document_set = self._confirmed_current_document_set(
            project_id=project_id,
            document_set_revision_id=project.current_document_set_revision_id,
        )
        rows = list(
            self.session.scalars(
                select(ObservationRow).where(
                    ObservationRow.project_id == project_id,
                    ObservationRow.id.in_(observation_ids),
                )
            )
        )
        if len(rows) != len(set(observation_ids)):
            raise ValueError("One or more evidence observations do not exist")
        if any(row.document_revision_id not in document_set.revision_ids for row in rows):
            raise ValueError(
                "Reconciliation observations must belong to the confirmed current document set"
            )
        rows.sort(key=lambda row: row.id)
        observations = tuple(Observation.model_validate(row.payload["observation"]) for row in rows)
        self._validate_independence_domains(
            rows,
            organization_id=actor.organization_id,
        )
        value, conflict = reconcile_observations(
            conflict_namespace=f"{project_id}:{reconciliation_version_id}",
            observations=observations,
        )
        if value is None and conflict is None:
            raise ValueError("At least two independent observations are required")
        if conflict is not None:
            existing_conflict = self.session.get(ConflictRow, conflict.conflict_id)
            if existing_conflict is not None:
                self._require_existing_conflict_integrity(
                    project_id=project_id,
                    conflict=conflict,
                    row=existing_conflict,
                )
            else:
                now = utc_now()
                self.session.add(
                    ConflictRow(
                        id=conflict.conflict_id,
                        project_id=project_id,
                        field_name=conflict.field_name,
                        status=conflict.status.value,
                        payload=conflict.model_dump(mode="json"),
                        created_at=now,
                        updated_at=now,
                    )
                )
                task_id = self._conflict_task_id(conflict.conflict_id)
                self.session.add(
                    ApprovalTaskRow(
                        id=task_id,
                        project_id=project_id,
                        task_type="CONFLICT_RESOLUTION",
                        entity_type="evidence_conflict",
                        entity_id=conflict.conflict_id,
                        assigned_role=ActorRole.REVIEWER.value,
                        status="PENDING",
                        required=True,
                        payload={
                            "created_by": actor.actor_id,
                            "observation_ids": list(conflict.observation_ids),
                        },
                        created_at=now,
                        updated_at=now,
                    )
                )
                project_service.record_event(
                    aggregate_type="project",
                    aggregate_id=project_id,
                    event_type="evidence_conflict_created",
                    actor=actor,
                    request_id=request_id,
                    reason=reason,
                    payload=conflict.model_dump(mode="json"),
                )
            return ReconciliationOutcome(conflict=conflict)

        basis_metadata = self._common_basis_metadata(rows, value)
        source = observations[0]
        derived_identity = {
            "project_id": project_id,
            "source_observation_ids": sorted(observation_ids),
            "reconciliation_version_id": reconciliation_version_id,
            "value": value,
        }
        verified = Observation(
            observation_id=f"observation-{content_hash(derived_identity)[:24]}",
            field_name=source.field_name,
            value=value,
            unit=source.unit,
            method=EvidenceMethod.RULE_ENGINE,
            method_version=reconciliation_version_id,
            source_priority=min(item.source_priority for item in observations),
            location=source.location,
            observed_at=utc_now(),
            actor_id=actor.actor_id,
            confidence=None,
            status=VerificationStatus.VERIFIED,
        )
        verified_payload = {
            "observation": verified.model_dump(mode="json"),
            "source_observation_ids": sorted(observation_ids),
            "reconciliation_version_id": reconciliation_version_id,
            **basis_metadata,
        }
        existing_verified = self.session.get(ObservationRow, verified.observation_id)
        if existing_verified is not None:
            existing_observation = Observation.model_validate(
                existing_verified.payload.get("observation")
            )
            replay_observation = verified.model_copy(
                update={
                    "observed_at": existing_observation.observed_at,
                    "actor_id": existing_observation.actor_id,
                }
            )
            replay_payload = {
                "observation": replay_observation.model_dump(mode="json"),
                "source_observation_ids": sorted(observation_ids),
                "reconciliation_version_id": reconciliation_version_id,
                **basis_metadata,
            }
            if (
                existing_verified.project_id != project_id
                or existing_verified.document_revision_id
                != replay_observation.location.document_revision_id
                or existing_verified.field_name != replay_observation.field_name
                or existing_verified.method != replay_observation.method.value
                or existing_verified.method_version != replay_observation.method_version
                or existing_verified.status != replay_observation.status.value
                or existing_verified.payload != replay_payload
            ):
                raise ValueError(
                    "Existing reconciled observation does not reproduce the deterministic result"
                )
            return ReconciliationOutcome(
                agreed_value=value,
                verified_observation_id=verified.observation_id,
            )
        self.session.add(
            ObservationRow(
                id=verified.observation_id,
                project_id=project_id,
                document_revision_id=source.location.document_revision_id,
                field_name=verified.field_name,
                method=verified.method.value,
                method_version=verified.method_version,
                status=verified.status.value,
                payload=verified_payload,
                created_at=utc_now(),
            )
        )
        project_service.record_event(
            aggregate_type="project",
            aggregate_id=project_id,
            event_type="evidence_reconciled",
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload={
                "verified_observation_id": verified.observation_id,
                "source_observation_ids": sorted(observation_ids),
                "reconciliation_version_id": reconciliation_version_id,
            },
        )
        return ReconciliationOutcome(
            agreed_value=value,
            verified_observation_id=verified.observation_id,
        )

    def _require_existing_conflict_integrity(
        self,
        *,
        project_id: str,
        conflict: Conflict,
        row: ConflictRow,
    ) -> None:
        task = self.session.get(ApprovalTaskRow, self._conflict_task_id(conflict.conflict_id))
        if (
            row.project_id != project_id
            or row.field_name != conflict.field_name
            or row.status != conflict.status.value
            or row.payload != conflict.model_dump(mode="json")
            or task is None
            or task.project_id != project_id
            or task.task_type != "CONFLICT_RESOLUTION"
            or task.entity_type != "evidence_conflict"
            or task.entity_id != conflict.conflict_id
            or task.assigned_role != ActorRole.REVIEWER.value
            or task.status != "PENDING"
            or not task.required
            or task.payload.get("observation_ids") != list(conflict.observation_ids)
            or not isinstance(task.payload.get("created_by"), str)
            or not task.payload["created_by"]
        ):
            raise ValueError(
                "Existing evidence conflict or its mandatory review task does not reproduce"
            )

    def reconciliation_context(
        self,
        *,
        actor: Actor,
        project_id: str,
        field_name: str | None,
        limit: int,
    ) -> ReconciliationContextView:
        if limit < 1 or limit > 100:
            raise ValueError("Reconciliation context limit must be between 1 and 100")
        project = ProjectService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        ).get_project(
            actor=actor,
            project_id=project_id,
            required_roles=(ActorRole.REVIEWER, ActorRole.TECHNICAL_EXPERT),
        )
        rules = require_bound_controlled_version(
            session=self.session,
            settings=self.settings,
            project_id=project_id,
            organization_id=actor.organization_id,
            purpose="reconciliation_rules",
            kind="reconciliation_rules",
        )
        document_set = self._confirmed_current_document_set(
            project_id=project_id,
            document_set_revision_id=project.current_document_set_revision_id,
        )
        raw_fields = list(
            self.session.scalars(
                select(ObservationRow.field_name)
                .where(
                    ObservationRow.project_id == project_id,
                    ObservationRow.document_revision_id.in_(tuple(document_set.revision_ids)),
                    ObservationRow.status == VerificationStatus.UNVERIFIED.value,
                )
                .distinct()
                .order_by(ObservationRow.field_name)
                .limit(501)
            )
        )
        available_fields = tuple(raw_fields[:500])
        selected_field = field_name.strip() if field_name is not None else None
        if selected_field == "":
            selected_field = None
        if selected_field is not None and len(selected_field) > 300:
            raise ValueError("Reconciliation field name exceeds 300 characters")
        candidates: tuple[ReconciliationCandidateView, ...] = ()
        candidates_truncated = False
        if selected_field is not None:
            rows = list(
                self.session.scalars(
                    select(ObservationRow)
                    .where(
                        ObservationRow.project_id == project_id,
                        ObservationRow.document_revision_id.in_(tuple(document_set.revision_ids)),
                        ObservationRow.field_name == selected_field,
                        ObservationRow.status == VerificationStatus.UNVERIFIED.value,
                    )
                    .order_by(ObservationRow.created_at.desc(), ObservationRow.id)
                    .limit(limit + 1)
                )
            )
            candidates_truncated = len(rows) > limit
            rows = rows[:limit]
            qualification_ids = {
                qualification_id
                for row in rows
                if isinstance(
                    qualification_id := row.payload.get("adapter_qualification_id"),
                    str,
                )
                and qualification_id
            }
            qualifications = {
                row.id: row
                for row in self.session.scalars(
                    select(AdapterQualificationRow).where(
                        AdapterQualificationRow.id.in_(tuple(qualification_ids))
                    )
                )
            }
            candidates = tuple(
                self._reconciliation_candidate_view(
                    row=row,
                    qualification=qualifications.get(
                        str(row.payload.get("adapter_qualification_id"))
                    ),
                    organization_id=actor.organization_id,
                )
                for row in rows
            )
        return ReconciliationContextView(
            project_id=project_id,
            document_set_revision_id=document_set.id,
            reconciliation_version_id=rules.id,
            available_field_names=available_fields,
            field_names_truncated=len(raw_fields) > 500,
            selected_field_name=selected_field,
            candidates=candidates,
            candidates_truncated=candidates_truncated,
        )

    def resolve_conflict(
        self,
        *,
        actor: Actor,
        project_id: str,
        conflict_id: str,
        command: ConflictResolutionCommand,
        request_id: str,
    ) -> ConflictResolutionResult:
        resolution_reason = command.resolution_reason.strip()
        if not resolution_reason:
            raise ValueError("resolution_reason is required")
        project_service = ProjectService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        )
        project_service.get_project(
            actor=actor,
            project_id=project_id,
            lock=True,
            required_roles=(ActorRole.REVIEWER, ActorRole.TECHNICAL_EXPERT),
        )
        row = self.session.scalar(
            select(ConflictRow)
            .where(
                ConflictRow.id == conflict_id,
                ConflictRow.project_id == project_id,
            )
            .with_for_update()
        )
        if row is None:
            raise LookupError(conflict_id)
        conflict = Conflict.model_validate(row.payload)
        if row.field_name != conflict.field_name or row.status != conflict.status.value:
            raise RuntimeError("Conflict row and payload state differ")
        if conflict.status is not VerificationStatus.CONFLICT:
            raise ValueError("Only an unresolved conflict can be resolved")
        expected_conflict_updated_at = ensure_utc(command.expected_conflict_updated_at)
        assert expected_conflict_updated_at is not None
        if ensure_utc(row.updated_at) != expected_conflict_updated_at:
            raise OptimisticLockError(
                "Conflict changed after it was loaded; reload before resolving"
            )
        task_id = self._conflict_task_id(conflict_id)
        task = self.session.scalar(
            select(ApprovalTaskRow)
            .where(
                ApprovalTaskRow.id == task_id,
                ApprovalTaskRow.project_id == project_id,
                ApprovalTaskRow.entity_type == "evidence_conflict",
                ApprovalTaskRow.entity_id == conflict_id,
                ApprovalTaskRow.task_type == "CONFLICT_RESOLUTION",
            )
            .with_for_update()
        )
        if task is None:
            raise RuntimeError("Conflict has no mandatory review task")
        task_observation_ids = tuple(
            value for value in task.payload.get("observation_ids", []) if isinstance(value, str)
        )
        if (
            not task.required
            or task_observation_ids != conflict.observation_ids
            or not isinstance(task.payload.get("created_by"), str)
            or not str(task.payload["created_by"]).strip()
        ):
            raise ValueError("Conflict review task integrity check failed")
        expected_task_updated_at = ensure_utc(command.expected_task_updated_at)
        assert expected_task_updated_at is not None
        if ensure_utc(task.updated_at) != expected_task_updated_at:
            raise OptimisticLockError(
                "Conflict review task changed after it was loaded; reload before resolving"
            )
        if task.status != "PENDING":
            raise ValueError("Conflict review task is not pending")
        if task.payload.get("created_by") == actor.actor_id:
            raise ValueError("Four-eyes violation: conflict creator cannot resolve its review task")
        if command.selected_observation_id not in conflict.observation_ids:
            raise ValueError("Selected observation is not part of the conflict")
        source_rows_by_id = {
            source.id: source
            for source in self.session.scalars(
                select(ObservationRow).where(
                    ObservationRow.project_id == project_id,
                    ObservationRow.id.in_(conflict.observation_ids),
                )
            )
        }
        if len(source_rows_by_id) != len(conflict.observation_ids):
            raise ValueError("One or more conflict observations are missing")
        source_rows = [
            source_rows_by_id[observation_id] for observation_id in conflict.observation_ids
        ]
        if any(source.field_name != conflict.field_name for source in source_rows):
            raise ValueError("Conflict observation field does not match the conflict")
        self._validate_independence_domains(
            source_rows,
            organization_id=actor.organization_id,
        )
        source_observations_by_id = {
            source.id: Observation.model_validate(source.payload["observation"])
            for source in source_rows
        }
        for source in source_rows:
            self._validated_basis_metadata(
                source,
                source_observations_by_id[source.id],
            )
        selected = source_rows_by_id[command.selected_observation_id]
        selected_observation = source_observations_by_id[selected.id]
        if selected_observation.field_name != conflict.field_name:
            raise ValueError("Selected observation field does not match the conflict")
        if selected_observation.actor_id == actor.actor_id:
            raise ValueError("Conflict resolution requires an actor different from the source")
        basis_metadata = self._validated_basis_metadata(selected, selected_observation)

        now = utc_now()
        resolved_conflict = conflict.model_copy(
            update={
                "status": VerificationStatus.VERIFIED,
                "resolved_value": selected_observation.value,
                "resolved_by": actor.actor_id,
                "resolved_at": now,
                "resolution_reason": resolution_reason,
            }
        )
        row.status = VerificationStatus.VERIFIED.value
        row.payload = resolved_conflict.model_dump(mode="json")
        row.updated_at = now

        derived_id = (
            "observation-"
            + content_hash(
                {
                    "conflict_id": conflict_id,
                    "selected_observation_id": selected.id,
                    "resolved_value": selected_observation.value,
                }
            )[:24]
        )
        verified = selected_observation.model_copy(
            update={
                "observation_id": derived_id,
                "method": EvidenceMethod.RULE_ENGINE,
                "method_version": "conflict-resolution-v1",
                "observed_at": now,
                "actor_id": actor.actor_id,
                "confidence": None,
                "status": VerificationStatus.VERIFIED,
            }
        )
        self.session.add(
            ObservationRow(
                id=verified.observation_id,
                project_id=project_id,
                document_revision_id=selected.document_revision_id,
                field_name=verified.field_name,
                method=verified.method.value,
                method_version=verified.method_version,
                status=verified.status.value,
                payload={
                    "observation": verified.model_dump(mode="json"),
                    "source_observation_ids": [selected.id],
                    "conflict_id": conflict_id,
                    "derivation_type": "CONFLICT_RESOLUTION",
                    **basis_metadata,
                },
                created_at=now,
            )
        )
        task.status = "APPROVED"
        task.updated_at = now
        approval_id = f"approval-{uuid4()}"
        self.session.add(
            ApprovalRecordRow(
                id=approval_id,
                task_id=task.id,
                decision="APPROVED",
                decided_by=actor.actor_id,
                decided_at=now,
                reason=resolution_reason,
                payload={
                    "project_id": project_id,
                    "evidence_ids": [selected.id, verified.observation_id],
                    "conflict_id": conflict_id,
                },
            )
        )
        project_service.record_event(
            aggregate_type="project",
            aggregate_id=project_id,
            event_type="evidence_conflict_resolved",
            actor=actor,
            request_id=request_id,
            reason=resolution_reason,
            payload={
                "conflict_id": conflict_id,
                "selected_observation_id": selected.id,
                "verified_observation_id": verified.observation_id,
                "approval_task_id": task.id,
                "approval_record_id": approval_id,
            },
        )
        return ConflictResolutionResult(
            conflict=resolved_conflict,
            verified_observation=verified,
        )

    def conflict_review(
        self,
        *,
        actor: Actor,
        project_id: str,
        conflict_id: str,
    ) -> ConflictReviewView:
        project_service = ProjectService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        )
        project_service.get_project(
            actor=actor,
            project_id=project_id,
            required_roles=(ActorRole.REVIEWER, ActorRole.TECHNICAL_EXPERT),
        )
        row = self.session.scalar(
            select(ConflictRow).where(
                ConflictRow.id == conflict_id,
                ConflictRow.project_id == project_id,
            )
        )
        if row is None:
            raise LookupError(conflict_id)
        conflict = Conflict.model_validate(row.payload)
        state_consistent = (
            row.field_name == conflict.field_name and row.status == conflict.status.value
        )
        observation_rows = {
            observation.id: observation
            for observation in self.session.scalars(
                select(ObservationRow).where(
                    ObservationRow.project_id == project_id,
                    ObservationRow.id.in_(conflict.observation_ids),
                )
            )
        }
        qualification_ids = {
            value
            for value in (
                observation.payload.get("adapter_qualification_id")
                for observation in observation_rows.values()
            )
            if isinstance(value, str) and value
        }
        qualifications = {
            qualification.id: qualification
            for qualification in self.session.scalars(
                select(AdapterQualificationRow).where(
                    AdapterQualificationRow.id.in_(qualification_ids)
                )
            )
        }
        observations = tuple(
            self._conflict_observation_view(
                observation_rows[observation_id],
                qualifications.get(
                    str(observation_rows[observation_id].payload.get("adapter_qualification_id"))
                ),
            )
            for observation_id in conflict.observation_ids
            if observation_id in observation_rows
        )
        missing_observation_ids = tuple(
            observation_id
            for observation_id in conflict.observation_ids
            if observation_id not in observation_rows
        )
        task_id = self._conflict_task_id(conflict_id)
        task = self.session.scalar(
            select(ApprovalTaskRow).where(
                ApprovalTaskRow.id == task_id,
                ApprovalTaskRow.project_id == project_id,
                ApprovalTaskRow.entity_type == "evidence_conflict",
                ApprovalTaskRow.entity_id == conflict_id,
                ApprovalTaskRow.task_type == "CONFLICT_RESOLUTION",
            )
        )
        blockers: list[str] = []
        if conflict.status is not VerificationStatus.CONFLICT:
            blockers.append("CONFLICT_NOT_OPEN")
        if not state_consistent:
            blockers.append("CONFLICT_STATE_MISMATCH")
        if missing_observation_ids:
            blockers.append("CONFLICT_OBSERVATION_MISSING")
        if any(observation.field_name != conflict.field_name for observation in observations):
            blockers.append("CONFLICT_OBSERVATION_FIELD_MISMATCH")
        try:
            self._validate_independence_domains(
                list(observation_rows.values()),
                organization_id=actor.organization_id,
            )
        except ValueError:
            blockers.append("CONFLICT_INDEPENDENCE_INVALID")
        try:
            for observation_row in observation_rows.values():
                self._validated_basis_metadata(
                    observation_row,
                    Observation.model_validate(observation_row.payload["observation"]),
                )
        except (KeyError, TypeError, ValueError):
            blockers.append("CONFLICT_COMMERCIAL_BASIS_INVALID")
        if task is None:
            blockers.append("CONFLICT_TASK_MISSING")
        else:
            task_observation_ids = tuple(
                value for value in task.payload.get("observation_ids", []) if isinstance(value, str)
            )
            task_created_by = task.payload.get("created_by")
            if not task.required:
                blockers.append("CONFLICT_TASK_NOT_REQUIRED")
            if task_observation_ids != conflict.observation_ids:
                blockers.append("CONFLICT_TASK_SCOPE_MISMATCH")
            if not isinstance(task_created_by, str) or not task_created_by.strip():
                blockers.append("CONFLICT_TASK_CREATOR_MISSING")
            if task.status != "PENDING":
                blockers.append("TASK_NOT_PENDING")
            if task_created_by == actor.actor_id:
                blockers.append("FOUR_EYES_TASK_CREATOR")
        if observations and all(
            observation.actor_id == actor.actor_id for observation in observations
        ):
            blockers.append("NO_INDEPENDENT_OBSERVATION")
        updated_at = ensure_utc(row.updated_at)
        if updated_at is None:
            raise RuntimeError("Conflict update timestamp is missing")
        return ConflictReviewView(
            conflict=conflict,
            conflict_updated_at=updated_at,
            observations=observations,
            missing_observation_ids=missing_observation_ids,
            task_id=task_id,
            task_status=task.status if task is not None else None,
            task_required=task.required if task is not None else None,
            task_updated_at=ensure_utc(task.updated_at) if task is not None else None,
            task_created_by=(
                str(task.payload.get("created_by"))
                if task is not None and task.payload.get("created_by")
                else None
            ),
            resolution_allowed=not blockers,
            resolution_blockers=tuple(blockers),
        )

    @staticmethod
    def _conflict_task_id(conflict_id: str) -> str:
        return f"approval-task-conflict-{content_hash(conflict_id)[:24]}"

    def _validated_upstream_lineage(
        self,
        *,
        project_id: str,
        document_set: DocumentSetRevisionRow | None,
        observation_ids: tuple[str, ...],
    ) -> list[dict[str, str]]:
        if not observation_ids:
            return []
        if document_set is None:
            raise ValueError("Upstream evidence requires a confirmed current document set")
        rows = list(
            self.session.scalars(
                select(ObservationRow).where(
                    ObservationRow.project_id == project_id,
                    ObservationRow.id.in_(observation_ids),
                )
            )
        )
        rows_by_id = {row.id: row for row in rows}
        if set(rows_by_id) != set(observation_ids):
            raise ValueError("One or more upstream evidence observations do not exist")
        lineage: list[dict[str, str]] = []
        for observation_id in observation_ids:
            row = rows_by_id[observation_id]
            observation = Observation.model_validate(row.payload.get("observation"))
            if (
                row.document_revision_id not in document_set.revision_ids
                or row.id != observation.observation_id
                or row.document_revision_id != observation.location.document_revision_id
                or row.field_name != observation.field_name
                or row.method != observation.method.value
                or row.method_version != observation.method_version
                or row.status != observation.status.value
            ):
                raise RuntimeError("Upstream evidence identity does not reproduce")
            lineage.append(
                {
                    "observation_id": row.id,
                    "observation_hash": content_hash(row.payload),
                }
            )
        return lineage

    def _manual_upstream_lineage_blockers(
        self,
        *,
        source: ObservationRow,
        document_set: DocumentSetRevisionRow,
    ) -> list[str]:
        stored = source.payload.get("upstream_observations")
        if stored is None:
            return []
        if not isinstance(stored, list) or not stored:
            return ["UPSTREAM_EVIDENCE_LINEAGE_INVALID"]
        observation_ids: list[str] = []
        for item in stored:
            if (
                not isinstance(item, dict)
                or set(item) != {"observation_id", "observation_hash"}
                or not isinstance(item.get("observation_id"), str)
                or not isinstance(item.get("observation_hash"), str)
            ):
                return ["UPSTREAM_EVIDENCE_LINEAGE_INVALID"]
            observation_ids.append(item["observation_id"])
        if len(observation_ids) != len(set(observation_ids)):
            return ["UPSTREAM_EVIDENCE_LINEAGE_INVALID"]
        try:
            current = self._validated_upstream_lineage(
                project_id=source.project_id,
                document_set=document_set,
                observation_ids=tuple(observation_ids),
            )
        except (RuntimeError, ValueError):
            return ["UPSTREAM_EVIDENCE_DRIFT"]
        if current != stored:
            return ["UPSTREAM_EVIDENCE_DRIFT"]
        return []

    def _manual_evidence_policy(
        self,
        *,
        project_id: str,
        organization_id: str,
    ) -> tuple[ControlledVersionRow, ManualEvidencePolicy]:
        bindings = list(
            self.session.execute(
                select(ControlledVersionRow, ProjectControlledVersionRow)
                .join(
                    ProjectControlledVersionRow,
                    ProjectControlledVersionRow.controlled_version_id == ControlledVersionRow.id,
                )
                .where(
                    ProjectControlledVersionRow.project_id == project_id,
                    ProjectControlledVersionRow.purpose == "manual_evidence_policy",
                )
            ).all()
        )
        if len(bindings) != 1:
            raise ValueError("Project must bind exactly one approved manual evidence policy")
        row, _binding = bindings[0]
        require_controlled_version_integrity(
            session=self.session,
            settings=self.settings,
            row=row,
            expected_organization_id=organization_id,
            expected_kind="manual_evidence_policy",
        )
        policy = ManualEvidencePolicy.model_validate(
            {
                "review_role": row.payload.get("review_role"),
                "allowed_project_states": row.payload.get("allowed_project_states"),
            }
        )
        return row, policy

    def _confirmed_current_document_set(
        self,
        *,
        project_id: str,
        document_set_revision_id: str | None,
    ) -> DocumentSetRevisionRow:
        return require_confirmed_document_set_integrity(
            session=self.session,
            settings=self.settings,
            project_id=project_id,
            document_set_revision_id=document_set_revision_id,
        )

    @staticmethod
    def _manual_evidence_task_id(
        observation_id: str,
        policy_version_id: str,
    ) -> str:
        return (
            "approval-task-manual-evidence-"
            + content_hash(
                {
                    "observation_id": observation_id,
                    "policy_version_id": policy_version_id,
                }
            )[:24]
        )

    def _ensure_manual_evidence_task(
        self,
        *,
        source: ObservationRow,
        policy_row: ControlledVersionRow,
        policy: ManualEvidencePolicy,
        document_set: DocumentSetRevisionRow,
        created_by: str,
    ) -> ApprovalTaskRow:
        observation = Observation.model_validate(source.payload.get("observation"))
        if (
            source.project_id != document_set.project_id
            or source.document_revision_id not in document_set.revision_ids
            or source.method != EvidenceMethod.MANUAL.value
            or source.status != VerificationStatus.UNVERIFIED.value
            or observation.method is not EvidenceMethod.MANUAL
            or observation.status is not VerificationStatus.UNVERIFIED
            or source.method_version != policy_row.id
            or observation.method_version != policy_row.id
            or source.field_name != observation.field_name
        ):
            raise RuntimeError(
                "Manual evidence source does not reproduce its governed review scope"
            )
        task_id = self._manual_evidence_task_id(source.id, policy_row.id)
        source_hash = content_hash(source.payload)
        expected_payload = {
            "created_by": created_by,
            "source_observation_id": source.id,
            "source_observation_hash": source_hash,
            "observation_ids": [source.id],
            "policy_version_id": policy_row.id,
            "policy_content_hash": policy_row.content_hash,
            "document_set_revision_id": document_set.id,
            "document_revision_id": source.document_revision_id,
            "review_role": policy.review_role.value,
        }
        existing = self.session.get(ApprovalTaskRow, task_id)
        if existing is not None:
            if (
                existing.project_id != source.project_id
                or existing.task_type != "MANUAL_EVIDENCE_REVIEW"
                or existing.entity_type != "evidence_observation"
                or existing.entity_id != source.id
                or existing.assigned_role != policy.review_role.value
                or not existing.required
                or existing.payload != expected_payload
            ):
                raise RuntimeError("Existing manual evidence task does not reproduce its identity")
            return existing
        now = utc_now()
        task = ApprovalTaskRow(
            id=task_id,
            project_id=source.project_id,
            task_type="MANUAL_EVIDENCE_REVIEW",
            entity_type="evidence_observation",
            entity_id=source.id,
            assigned_role=policy.review_role.value,
            status="PENDING",
            required=True,
            payload=expected_payload,
            created_at=now,
            updated_at=now,
        )
        self.session.add(task)
        return task

    @staticmethod
    def _manual_evidence_review_blockers(
        *,
        actor: Actor,
        project_state: ApprovalState,
        source: ObservationRow,
        observation: Observation,
        task: ApprovalTaskRow,
        policy_row: ControlledVersionRow,
        policy: ManualEvidencePolicy,
        document_set: DocumentSetRevisionRow,
    ) -> list[str]:
        blockers: list[str] = []
        expected_payload = {
            "created_by": observation.actor_id,
            "source_observation_id": source.id,
            "source_observation_hash": content_hash(source.payload),
            "observation_ids": [source.id],
            "policy_version_id": policy_row.id,
            "policy_content_hash": policy_row.content_hash,
            "document_set_revision_id": document_set.id,
            "document_revision_id": source.document_revision_id,
            "review_role": policy.review_role.value,
        }
        if (
            source.project_id != document_set.project_id
            or source.document_revision_id not in document_set.revision_ids
            or source.field_name != observation.field_name
            or source.method != observation.method.value
            or source.method_version != observation.method_version
            or source.status != observation.status.value
            or observation.method is not EvidenceMethod.MANUAL
            or observation.status is not VerificationStatus.UNVERIFIED
            or source.method_version != policy_row.id
            or not isinstance(source.payload.get("manual_reason"), str)
            or not source.payload["manual_reason"].strip()
        ):
            blockers.append("MANUAL_EVIDENCE_SCOPE_MISMATCH")
        if project_state not in policy.allowed_project_states:
            blockers.append("PROJECT_STATE_NOT_ALLOWED")
        if (
            task.task_type != "MANUAL_EVIDENCE_REVIEW"
            or task.entity_type != "evidence_observation"
            or task.entity_id != source.id
            or task.project_id != source.project_id
            or task.payload != expected_payload
        ):
            blockers.append("TASK_INTEGRITY_FAILED")
        if not task.required:
            blockers.append("TASK_NOT_REQUIRED")
        if task.assigned_role != policy.review_role.value:
            blockers.append("TASK_ROLE_MISMATCH")
        if task.status != "PENDING":
            blockers.append("TASK_NOT_PENDING")
        if observation.actor_id == actor.actor_id:
            blockers.append("FOUR_EYES_SOURCE_AUTHOR")
        if task.payload.get("created_by") == actor.actor_id:
            blockers.append("FOUR_EYES_TASK_CREATOR")
        return list(dict.fromkeys(blockers))

    def _validate_adapter(self, actor: Actor, draft: ObservationDraft) -> None:
        if draft.method is EvidenceMethod.MANUAL:
            if draft.adapter_qualification_id is not None:
                raise ValueError("Manual evidence cannot claim an adapter qualification")
            if ActorRole.SYSTEM in actor.roles:
                raise ValueError("SYSTEM actor cannot create manual evidence")
            return
        actor.require_any(ActorRole.SYSTEM)
        if not draft.adapter_qualification_id:
            raise ValueError("Automated evidence requires an adapter qualification")
        qualification = self.session.scalar(
            select(AdapterQualificationRow).where(
                AdapterQualificationRow.id == draft.adapter_qualification_id,
                AdapterQualificationRow.status == "APPROVED",
                AdapterQualificationRow.adapter_version == draft.method_version,
            )
        )
        if qualification is None:
            raise ValueError("Evidence adapter is not qualified for this version")
        if qualification.valid_until and qualification.valid_until < utc_now().date():
            raise ValueError("Evidence adapter qualification has expired")
        if qualification.payload.get("organization_id") != actor.organization_id:
            raise ValueError("Evidence adapter qualification belongs to another organisation")
        if qualification.payload.get("service_actor_id") != actor.actor_id:
            raise ValueError("Evidence adapter qualification belongs to another service identity")
        if draft.method.value not in qualification.payload.get("supported_methods", []):
            raise ValueError("Evidence method is outside the adapter qualification")

    def _validate_independence_domains(
        self,
        rows: list[ObservationRow],
        *,
        organization_id: str,
    ) -> None:
        qualification_ids: list[str] = []
        for row in rows:
            qualification_id = row.payload.get("adapter_qualification_id")
            if not isinstance(qualification_id, str) or not qualification_id:
                raise ValueError(
                    "Independent automatic reconciliation requires a qualified adapter "
                    "for every observation"
                )
            qualification_ids.append(qualification_id)
        if len(set(qualification_ids)) != len(rows):
            raise ValueError(
                "Independent automatic reconciliation requires a qualified adapter "
                "for every observation"
            )
        qualifications = list(
            self.session.scalars(
                select(AdapterQualificationRow).where(
                    AdapterQualificationRow.id.in_(qualification_ids),
                    AdapterQualificationRow.status == "APPROVED",
                )
            )
        )
        domains = [
            qualification.payload.get("independence_domain") for qualification in qualifications
        ]
        if (
            len(qualifications) != len(rows)
            or any(not isinstance(domain, str) or not domain for domain in domains)
            or len(set(domains)) != len(rows)
        ):
            raise ValueError(
                "Observations do not come from distinct qualified independence domains"
            )
        qualifications_by_id = {qualification.id: qualification for qualification in qualifications}
        for row in rows:
            qualification_id = row.payload.get("adapter_qualification_id")
            qualification = qualifications_by_id.get(str(qualification_id))
            observation = Observation.model_validate(row.payload["observation"])
            if qualification is None:
                raise ValueError("An observation adapter qualification is unavailable")
            if (
                row.id != observation.observation_id
                or row.document_revision_id != observation.location.document_revision_id
                or row.field_name != observation.field_name
                or row.method != observation.method.value
                or row.method_version != observation.method_version
                or row.status != observation.status.value
                or observation.status is not VerificationStatus.UNVERIFIED
                or observation.method in {EvidenceMethod.MANUAL, EvidenceMethod.RULE_ENGINE}
                or qualification.adapter_version != row.method_version
                or row.method not in qualification.payload.get("supported_methods", [])
                or qualification.payload.get("organization_id") != organization_id
                or qualification.payload.get("service_actor_id") != observation.actor_id
                or (
                    qualification.valid_until is not None
                    and qualification.valid_until < utc_now().date()
                )
            ):
                raise ValueError("An observation no longer matches its qualified adapter identity")

    @staticmethod
    def _reconciliation_candidate_view(
        *,
        row: ObservationRow,
        qualification: AdapterQualificationRow | None,
        organization_id: str,
    ) -> ReconciliationCandidateView:
        observation = Observation.model_validate(row.payload["observation"])
        qualification_id = row.payload.get("adapter_qualification_id")
        blockers: list[str] = []
        if (
            row.id != observation.observation_id
            or row.document_revision_id != observation.location.document_revision_id
            or row.field_name != observation.field_name
            or row.method != observation.method.value
            or row.method_version != observation.method_version
            or row.status != observation.status.value
            or observation.status is not VerificationStatus.UNVERIFIED
        ):
            blockers.append("EVIDENCE_INTEGRITY_FAILED")
        if observation.method in {EvidenceMethod.MANUAL, EvidenceMethod.RULE_ENGINE}:
            blockers.append("AUTOMATIC_SOURCE_REQUIRED")
        if not isinstance(qualification_id, str) or not qualification_id or qualification is None:
            blockers.append("QUALIFICATION_MISSING")
        elif (
            qualification.status != "APPROVED"
            or qualification.adapter_version != observation.method_version
            or observation.method.value not in qualification.payload.get("supported_methods", [])
            or qualification.payload.get("organization_id") != organization_id
            or qualification.payload.get("service_actor_id") != observation.actor_id
        ):
            blockers.append("QUALIFICATION_IDENTITY_FAILED")
        if (
            qualification is not None
            and qualification.valid_until is not None
            and qualification.valid_until < utc_now().date()
        ):
            blockers.append("QUALIFICATION_EXPIRED")
        independence_domain = (
            qualification.payload.get("independence_domain") if qualification is not None else None
        )
        if not isinstance(independence_domain, str) or not independence_domain:
            blockers.append("INDEPENDENCE_DOMAIN_MISSING")
            independence_domain = None
        return ReconciliationCandidateView(
            observation=observation,
            adapter_qualification_id=(
                qualification_id if isinstance(qualification_id, str) and qualification_id else None
            ),
            adapter_status=qualification.status if qualification is not None else None,
            adapter_valid_until=(qualification.valid_until if qualification is not None else None),
            independence_domain=independence_domain,
            eligible=not blockers,
            blockers=tuple(blockers),
        )

    @staticmethod
    def _conflict_observation_view(
        row: ObservationRow,
        qualification: AdapterQualificationRow | None,
    ) -> ConflictObservationView:
        observation = Observation.model_validate(row.payload["observation"])
        basis_metadata = {
            key: str(row.payload[key])
            for key in ("basis_type", "currency", "unit")
            if row.payload.get(key) is not None
        }
        qualification_id = row.payload.get("adapter_qualification_id")
        return ConflictObservationView.model_validate(
            {
                **observation.model_dump(mode="python"),
                "adapter_qualification_id": (
                    qualification_id
                    if isinstance(qualification_id, str) and qualification_id
                    else None
                ),
                "adapter_qualification_status": (
                    qualification.status if qualification is not None else None
                ),
                "adapter_qualification_valid_until": (
                    qualification.valid_until if qualification is not None else None
                ),
                "independence_domain": (
                    str(qualification.payload.get("independence_domain"))
                    if qualification is not None
                    and qualification.payload.get("independence_domain")
                    else None
                ),
                "basis_metadata": basis_metadata,
            }
        )

    @staticmethod
    def _common_basis_metadata(rows: list[ObservationRow], agreed_value: Any) -> dict[str, str]:
        metadata = [
            EvidenceService._validated_basis_metadata(
                row,
                Observation.model_validate(row.payload["observation"]),
            )
            for row in rows
        ]
        first = metadata[0]
        if any(item != first for item in metadata[1:]):
            raise ValueError("Independent observations disagree in commercial basis")
        if first and Decimal(first["unit_rate"]) != Decimal(str(agreed_value)):
            raise ValueError("Commercial basis rate differs from the reconciled value")
        return first

    @staticmethod
    def _validated_basis_metadata(
        row: ObservationRow,
        observation: Observation,
    ) -> dict[str, str]:
        basis_type = row.payload.get("basis_type")
        currency = row.payload.get("currency")
        unit = row.payload.get("unit")
        if basis_type is None and currency is None and unit is None:
            return {}
        if basis_type != "NORMALIZED_PRICE":
            raise ValueError("Evidence declares an unsupported commercial basis type")
        if (
            not isinstance(currency, str)
            or len(currency) != 3
            or not currency.isascii()
            or not currency.isalpha()
            or currency != currency.upper()
        ):
            raise ValueError("Normalized price evidence has an invalid currency")
        if not isinstance(unit, str) or not unit.strip() or unit != unit.strip():
            raise ValueError("Normalized price evidence has an invalid unit")
        if observation.unit != unit:
            raise ValueError("Normalized price evidence unit differs from its observation unit")
        try:
            rate = Decimal(str(observation.value))
        except (InvalidOperation, TypeError, ValueError) as error:
            raise ValueError("Normalized price evidence has no valid unit rate") from error
        if not rate.is_finite() or rate <= 0:
            raise ValueError("Normalized price evidence unit rate must be finite and positive")
        return {
            "basis_type": "NORMALIZED_PRICE",
            "unit_rate": str(rate),
            "currency": currency,
            "unit": unit,
        }
