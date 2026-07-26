from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from pydantic import Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from tenderguard.application.approvals import ApprovalService
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
from tenderguard.domain.approvals import ApprovalSubject
from tenderguard.domain.common import content_hash, ensure_utc, utc_now
from tenderguard.domain.contract import (
    ContractAssessment,
    ContractRequirementsPolicy,
    ContractTerm,
    validate_contract,
)
from tenderguard.domain.enums import (
    ActorRole,
    ApprovalDecision,
    ApprovalReason,
    ApprovalState,
    ContractTermKind,
    CostCategory,
    EvidenceMethod,
    Severity,
    VerificationStatus,
    VersionStatus,
)
from tenderguard.domain.models import DomainModel, Observation, ValidationFinding
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.object_store import ObjectStore
from tenderguard.infrastructure.orm import (
    AdapterQualificationRow,
    ApprovalRecordRow,
    ApprovalTaskRow,
    BoqLineRow,
    CommercialCostModelRow,
    ConflictRow,
    ContractTermRow,
    ControlledVersionRow,
    DocumentSetRevisionRow,
    ObservationRow,
    ProjectControlledVersionRow,
    VerificationFindingRow,
)

_EDITABLE_STATES = frozenset(
    {
        ApprovalState.EXTRACTION_IN_PROGRESS,
        ApprovalState.EXTRACTION_REVIEW,
        ApprovalState.BOQ_IN_PROGRESS,
        ApprovalState.BOQ_REVIEW,
        ApprovalState.PRICING_IN_PROGRESS,
        ApprovalState.RFQ_REQUIRED,
    }
)


class ContractTermDraft(DomainModel):
    kind: ContractTermKind
    value: str = Field(min_length=1, max_length=4000)
    observation_ids: tuple[str, ...] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def identifiers_and_value_are_normalized(self) -> ContractTermDraft:
        if self.value != self.value.strip():
            raise ValueError("Contract term value must be normalized")
        if len(self.observation_ids) != len(set(self.observation_ids)):
            raise ValueError("Contract observation IDs must be unique")
        if any(
            not item
            or item != item.strip()
            or len(item) > 64
            or any(character.isspace() for character in item)
            for item in self.observation_ids
        ):
            raise ValueError("Contract observation ID is invalid")
        return self


class ContractCostImpactCommand(DomainModel):
    amount: Decimal = Field(ge=0)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    cost_component_line_id: str | None = None
    cost_component_semantic_key: str | None = None
    derived_cost_model_id: str | None = None
    no_cost_reason: str | None = None

    @model_validator(mode="after")
    def resolution_is_explicit(self) -> ContractCostImpactCommand:
        if self.amount == 0:
            if not self.no_cost_reason:
                raise ValueError("Zero contract cost impact requires an explicit reason")
            if (
                self.cost_component_line_id
                or self.cost_component_semantic_key
                or self.derived_cost_model_id
            ):
                raise ValueError("Zero contract cost impact cannot reference a cost component")
        elif not (
            self.currency
            and self.cost_component_line_id
            and self.cost_component_semantic_key
            and self.derived_cost_model_id
        ):
            raise ValueError(
                "Non-zero contract impact requires currency, a planned component, "
                "and a validated derived cost model"
            )
        return self


class ContractTermView(DomainModel):
    term_id: str
    kind: ContractTermKind
    value: str
    observation_ids: tuple[str, ...]
    verified: bool
    cost_impact_resolved: bool
    supersedes_term_id: str | None
    is_current: bool
    independence_source_ids: tuple[str, ...] = ()
    created_by: str
    verified_by: str | None = None
    rules_version_id: str
    document_set_revision_id: str
    approval_task_id: str
    updated_at: datetime
    approval_task_ids: tuple[str, ...] = ()
    cost_impact_proposal: ContractCostImpactCommand | None = None
    cost_impact_task_statuses: dict[str, str] = Field(default_factory=dict)
    cost_impact_approved_by: str | None = None
    cost_impact_finalized_at: datetime | None = None


class ContractValidationResult(DomainModel):
    assessment: ContractAssessment
    findings: tuple[ValidationFinding, ...]
    rules_version_id: str


class ContractEvidenceCandidateView(DomainModel):
    observation: Observation
    adapter_qualification_id: str | None
    adapter_status: str | None
    adapter_valid_until: date | None = None
    independence_domain: str | None
    eligible: bool
    blockers: tuple[str, ...] = ()


class ContractTermReviewView(DomainModel):
    term: ContractTermView
    task_status: str
    task_updated_at: datetime
    assigned_role: ActorRole
    decision_allowed: bool
    decision_blockers: tuple[str, ...]


class ContractCostImpactCandidateView(DomainModel):
    derived_cost_model_id: str
    amount: Decimal
    currency: str
    cost_component_line_id: str
    cost_component_semantic_key: str
    eligible: bool
    blockers: tuple[str, ...] = ()


class ContractContextView(DomainModel):
    project_id: str
    project_state: ApprovalState
    document_set_revision_id: str
    rules_version_id: str
    rules_content_hash: str
    required_term_kinds: tuple[ContractTermKind, ...]
    independently_verified_term_kinds: tuple[ContractTermKind, ...]
    evidence_field_names: dict[ContractTermKind, str]
    review_role: ActorRole
    selected_kind: ContractTermKind
    terms: tuple[ContractTermReviewView, ...]
    evidence_candidates: tuple[ContractEvidenceCandidateView, ...]
    impact_candidates: tuple[ContractCostImpactCandidateView, ...]
    candidates_truncated: bool
    validation: ContractValidationResult
    unresolved_conflict_ids: tuple[str, ...]


class ContractTermDecisionCommand(DomainModel):
    decision: ApprovalDecision
    expected_term_updated_at: datetime
    expected_task_updated_at: datetime

    @field_validator("expected_term_updated_at", "expected_task_updated_at")
    @classmethod
    def timestamps_are_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Expected timestamps must include a timezone")
        return value


class ContractTermDecisionResult(DomainModel):
    term: ContractTermView
    validation: ContractValidationResult
    approval_id: str
    decision: ApprovalDecision


class ContractService:
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
        selected_kind: ContractTermKind | None,
        limit: int,
    ) -> ContractContextView:
        if limit < 1 or limit > 100:
            raise ValueError("Contract context limit must be between 1 and 100")
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
        policy, rules = self._requirements(project_id, actor.organization_id)
        if selected_kind is None:
            selected_kind = sorted(
                policy.required_term_kinds,
                key=lambda item: item.value,
            )[0]
        if selected_kind not in policy.required_term_kinds:
            raise ValueError("Selected contract term is outside the approved requirements")
        term_rows = tuple(
            self.session.scalars(
                select(ContractTermRow)
                .where(
                    ContractTermRow.project_id == project_id,
                    ContractTermRow.is_current.is_(True),
                )
                .order_by(ContractTermRow.kind)
            )
        )
        reviews = tuple(
            self._review_view(
                actor=actor,
                project_state=ApprovalState(project.state),
                row=row,
                policy=policy,
                rules=rules,
                document_set=document_set,
            )
            for row in term_rows
        )
        field_name = policy.evidence_field_names[selected_kind]
        candidate_rows = list(
            self.session.scalars(
                select(ObservationRow)
                .where(
                    ObservationRow.project_id == project_id,
                    ObservationRow.field_name == field_name,
                    ObservationRow.document_revision_id.in_(document_set.revision_ids),
                )
                .order_by(ObservationRow.created_at.desc(), ObservationRow.id)
                .limit(limit + 1)
            )
        )
        candidates = tuple(
            self._candidate_view(
                row=row,
                organization_id=actor.organization_id,
                independent=(selected_kind in policy.independently_verified_term_kinds),
                document_revision_ids=frozenset(document_set.revision_ids),
            )
            for row in candidate_rows[:limit]
        )
        conflicts = self._unresolved_conflicts(project_id, field_name)
        validation = self._current_validation(
            project_id,
            policy=policy,
            rules=rules,
        )
        return ContractContextView(
            project_id=project_id,
            project_state=ApprovalState(project.state),
            document_set_revision_id=document_set.id,
            rules_version_id=rules.id,
            rules_content_hash=rules.content_hash,
            required_term_kinds=tuple(
                sorted(policy.required_term_kinds, key=lambda item: item.value)
            ),
            independently_verified_term_kinds=tuple(
                sorted(
                    policy.independently_verified_term_kinds,
                    key=lambda item: item.value,
                )
            ),
            evidence_field_names=policy.evidence_field_names,
            review_role=policy.review_role,
            selected_kind=selected_kind,
            terms=reviews,
            evidence_candidates=candidates,
            impact_candidates=self._impact_candidates(
                project_id=project_id,
                document_set_revision_id=document_set.id,
            ),
            candidates_truncated=len(candidate_rows) > limit,
            validation=validation,
            unresolved_conflict_ids=tuple(row.id for row in conflicts),
        )

    def submit_term(
        self,
        *,
        actor: Actor,
        project_id: str,
        draft: ContractTermDraft,
        expected_document_set_revision_id: str,
        rules_version_id: str,
        request_id: str,
        reason: str,
    ) -> ContractTermView:
        reason = self._reason(reason)
        project = self._require_editable_state(
            actor,
            project_id,
            required_roles=(ActorRole.TECHNICAL_EXPERT, ActorRole.REVIEWER),
        )
        document_set = self._document_set(
            project.id,
            project.current_document_set_revision_id,
        )
        if document_set.id != expected_document_set_revision_id:
            raise OptimisticLockError(
                "Current document set changed after contract context was loaded"
            )
        policy, rules = self._requirements(
            project.id,
            actor.organization_id,
            expected_version_id=rules_version_id,
        )
        if draft.kind not in policy.required_term_kinds:
            raise ValueError("Contract term is outside the approved requirements")
        field_name = policy.evidence_field_names[draft.kind]
        if self._unresolved_conflicts(project.id, field_name):
            raise ValueError(
                "Contract term cannot be submitted while its evidence conflict is unresolved"
            )
        observations = self._observations(
            project.id,
            draft.observation_ids,
            document_revision_ids=frozenset(document_set.revision_ids),
        )
        self._validate_observation_values(
            draft,
            observations,
            expected_field_name=field_name,
        )
        provenance_leaves = resolve_observation_leaves(
            self.session,
            project_id=project.id,
            observations=observations,
        )
        self._validate_observation_values(
            draft,
            provenance_leaves,
            expected_field_name=field_name,
            require_eligible_status=False,
        )
        independence_source_ids: tuple[str, ...] = draft.observation_ids
        if draft.kind in policy.independently_verified_term_kinds:
            independence_source_ids = require_distinct_qualified_independence(
                self.session,
                project_id=project.id,
                observations=observations,
            )
            if independence_source_ids != tuple(row.id for row in provenance_leaves):
                raise ValueError("Contract independence leaf resolution is inconsistent")
            self._validate_observation_values(
                draft,
                provenance_leaves,
                expected_field_name=field_name,
            )
        previous = self.session.scalar(
            select(ContractTermRow)
            .where(
                ContractTermRow.project_id == project.id,
                ContractTermRow.kind == draft.kind.value,
                ContractTermRow.is_current.is_(True),
            )
            .with_for_update()
        )
        now = utc_now()
        new_term_id = f"contract-term-{uuid4()}"
        superseded_task_ids: list[str] = []
        if previous is not None:
            previous.is_current = False
            previous.updated_at = now
            for previous_task in self._tasks_for_term(previous, lock=True):
                if previous_task.status == "SUPERSEDED":
                    continue
                previous_task.status = "SUPERSEDED"
                previous_task.payload = {
                    **previous_task.payload,
                    "superseded_at": now.isoformat(),
                    "superseded_by_entity_id": new_term_id,
                    "supersession_reason": "CONTRACT_TERM_REPLACED",
                }
                previous_task.updated_at = now
                superseded_task_ids.append(previous_task.id)
        row = ContractTermRow(
            id=new_term_id,
            project_id=project.id,
            kind=draft.kind.value,
            verified=False,
            cost_impact_resolved=False,
            supersedes_term_id=previous.id if previous else None,
            is_current=True,
            payload={
                **draft.model_dump(mode="json"),
                "evidence_field_name": field_name,
                "independence_source_ids": list(independence_source_ids),
                "created_by": actor.actor_id,
                "rules_version_id": rules.id,
                "rules_content_hash": rules.content_hash,
                "document_set_revision_id": document_set.id,
                "review_role": policy.review_role.value,
            },
            created_at=now,
            updated_at=now,
        )
        task = self._ensure_review_task(
            row=row,
            policy=policy,
            rules=rules,
            document_set=document_set,
        )
        row.payload = {**row.payload, "approval_task_id": task.id}
        self.session.add(row)
        self._audit(
            project.id,
            actor,
            request_id,
            reason,
            "contract_term_submitted",
            {
                "term_id": row.id,
                "kind": draft.kind,
                "value_hash": content_hash(draft.value),
                "observation_ids": list(draft.observation_ids),
                "independence_source_ids": list(independence_source_ids),
                "rules_version_id": rules.id,
                "rules_content_hash": rules.content_hash,
                "document_set_revision_id": document_set.id,
                "supersedes_term_id": row.supersedes_term_id,
                "superseded_approval_task_ids": superseded_task_ids,
                "approval_task_id": task.id,
            },
        )
        return self._view(row)

    def decide_term(
        self,
        *,
        actor: Actor,
        project_id: str,
        term_id: str,
        command: ContractTermDecisionCommand,
        request_id: str,
        reason: str,
    ) -> ContractTermDecisionResult:
        reason = self._reason(reason)
        project = self._require_editable_state(
            actor,
            project_id,
            required_roles=(ActorRole.TECHNICAL_EXPERT, ActorRole.REVIEWER),
        )
        row = self._current_term(project.id, term_id)
        expected_term_updated_at = ensure_utc(command.expected_term_updated_at)
        assert expected_term_updated_at is not None
        if ensure_utc(row.updated_at) != expected_term_updated_at:
            raise OptimisticLockError(
                "Contract term changed after it was loaded; reload before deciding"
            )
        if row.verified or row.payload.get("review_decision") is not None:
            raise ValueError("Only a current pending contract term can be reviewed")
        document_set = self._document_set(
            project.id,
            project.current_document_set_revision_id,
        )
        if row.payload.get("document_set_revision_id") != document_set.id:
            raise ValueError("Contract term belongs to a superseded document-set revision")
        stored_rules_version = row.payload.get("rules_version_id")
        if not isinstance(stored_rules_version, str) or not stored_rules_version:
            raise ValueError("Contract term rules version is missing")
        policy, rules = self._requirements(
            project.id,
            actor.organization_id,
            expected_version_id=stored_rules_version,
        )
        self._project_service().get_project(
            actor=actor,
            project_id=project.id,
            required_roles=(policy.review_role,),
        )
        if row.payload.get("created_by") == actor.actor_id:
            raise ValueError("Contract term review requires a different actor")
        draft = ContractTermDraft.model_validate(
            {
                "kind": row.kind,
                "value": row.payload.get("value"),
                "observation_ids": row.payload.get("observation_ids"),
            }
        )
        field_name = policy.evidence_field_names[draft.kind]
        if self._unresolved_conflicts(project.id, field_name):
            raise ValueError(
                "Contract term cannot be reviewed while its evidence conflict is unresolved"
            )
        observations = self._observations(
            project.id,
            draft.observation_ids,
            document_revision_ids=frozenset(document_set.revision_ids),
        )
        self._validate_observation_values(
            draft,
            observations,
            expected_field_name=field_name,
        )
        provenance_leaves = resolve_observation_leaves(
            self.session,
            project_id=project.id,
            observations=observations,
        )
        self._validate_observation_values(
            draft,
            provenance_leaves,
            expected_field_name=field_name,
            require_eligible_status=False,
        )
        independence_source_ids: tuple[str, ...] = draft.observation_ids
        if draft.kind in policy.independently_verified_term_kinds:
            independence_source_ids = require_distinct_qualified_independence(
                self.session,
                project_id=project.id,
                observations=observations,
            )
            if independence_source_ids != tuple(row.id for row in provenance_leaves):
                raise ValueError("Contract independence leaf resolution is inconsistent")
            self._validate_observation_values(
                draft,
                provenance_leaves,
                expected_field_name=field_name,
            )
        if tuple(row.payload.get("independence_source_ids", [])) != (independence_source_ids):
            raise ValueError("Contract independence evidence changed after submission")
        task = self._review_task(row, lock=True)
        if task is None:
            raise ValueError("Contract review task is missing")
        expected_task_updated_at = ensure_utc(command.expected_task_updated_at)
        assert expected_task_updated_at is not None
        if ensure_utc(task.updated_at) != expected_task_updated_at:
            raise OptimisticLockError(
                "Contract review task changed after it was loaded; reload before deciding"
            )
        blockers = self._review_blockers(
            actor=actor,
            project_state=ApprovalState(project.state),
            row=row,
            task=task,
            policy=policy,
            rules=rules,
            document_set=document_set,
        )
        if blockers:
            raise ValueError("Contract term review is blocked: " + ", ".join(blockers))
        now = utc_now()
        approval_id = f"approval-{uuid4()}"
        row.verified = command.decision is ApprovalDecision.APPROVED
        row.updated_at = now
        row.payload = {
            **row.payload,
            "reviewed_by": actor.actor_id,
            "reviewed_at": now.isoformat(),
            "review_decision": command.decision.value,
        }
        if row.verified:
            row.payload = {
                **row.payload,
                "verified_by": actor.actor_id,
                "verified_at": now.isoformat(),
            }
        task.status = command.decision.value
        task.updated_at = now
        approval_payload = {
            "project_id": project.id,
            "term_id": row.id,
            "kind": row.kind,
            "expected_term_updated_at": expected_term_updated_at.isoformat(),
            "expected_task_updated_at": expected_task_updated_at.isoformat(),
            "evidence_ids": list(draft.observation_ids),
            "independence_source_ids": list(independence_source_ids),
            "rules_version_id": rules.id,
            "rules_content_hash": rules.content_hash,
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
        validation = self._validate_current(project.id, policy=policy, rules=rules)
        self._audit(
            project.id,
            actor,
            request_id,
            reason,
            "contract_term_review_decided",
            {
                "approval_id": approval_id,
                "approval_task_id": task.id,
                "term_id": row.id,
                "kind": row.kind,
                "decision": command.decision.value,
                **approval_payload,
                "remaining_findings": [
                    finding.model_dump(mode="json") for finding in validation.findings
                ],
            },
        )
        return ContractTermDecisionResult(
            term=self._view(row),
            validation=validation,
            approval_id=approval_id,
            decision=command.decision,
        )

    def verify_term(
        self,
        *,
        actor: Actor,
        project_id: str,
        term_id: str,
        expected_term_updated_at: datetime,
        expected_task_updated_at: datetime,
        request_id: str,
        reason: str,
    ) -> tuple[ContractTermView, ContractValidationResult]:
        result = self.decide_term(
            actor=actor,
            project_id=project_id,
            term_id=term_id,
            command=ContractTermDecisionCommand(
                decision=ApprovalDecision.APPROVED,
                expected_term_updated_at=expected_term_updated_at,
                expected_task_updated_at=expected_task_updated_at,
            ),
            request_id=request_id,
            reason=reason,
        )
        return result.term, result.validation

    def propose_cost_impact(
        self,
        *,
        actor: Actor,
        project_id: str,
        term_id: str,
        command: ContractCostImpactCommand,
        expected_term_updated_at: datetime,
        request_id: str,
        reason: str,
    ) -> ContractTermView:
        reason = self._reason(reason)
        project = self._require_editable_state(
            actor,
            project_id,
            required_roles=(ActorRole.ESTIMATOR, ActorRole.TECHNICAL_EXPERT),
        )
        source = self._current_term(project.id, term_id)
        expected_updated_at = ensure_utc(expected_term_updated_at)
        assert expected_updated_at is not None
        if ensure_utc(source.updated_at) != expected_updated_at:
            raise OptimisticLockError(
                "Contract term changed after it was loaded; reload before proposing cost impact"
            )
        if not source.verified:
            raise ValueError("Contract cost impact requires a verified term")
        document_set = self._document_set(
            project.id,
            project.current_document_set_revision_id,
        )
        if source.payload.get("document_set_revision_id") != document_set.id:
            raise ValueError("Contract term belongs to a superseded document-set revision")
        policy, rules = self._requirements(project.id, project.organization_id)
        self._require_verified_term_integrity(
            row=source,
            policy=policy,
            rules=rules,
            document_set=document_set,
        )
        if command.amount > 0:
            self._validate_contract_cost_component(
                project.id,
                document_set.id,
                command,
            )
        now = utc_now()
        new_term_id = f"contract-term-{uuid4()}"
        superseded_task_ids: list[str] = []
        for old_task in self._cost_tasks_for_term(source, lock=True):
            if old_task.status == "SUPERSEDED":
                continue
            old_task.status = "SUPERSEDED"
            old_task.payload = {
                **old_task.payload,
                "superseded_at": now.isoformat(),
                "superseded_by_entity_id": new_term_id,
                "supersession_reason": "CONTRACT_COST_IMPACT_REPLACED",
            }
            old_task.updated_at = now
            superseded_task_ids.append(old_task.id)
        source.is_current = False
        source.updated_at = now
        row = ContractTermRow(
            id=new_term_id,
            project_id=project.id,
            kind=source.kind,
            verified=True,
            cost_impact_resolved=False,
            supersedes_term_id=source.id,
            is_current=True,
            payload={
                **source.payload,
                "cost_impact_proposal": command.model_dump(mode="json"),
                "cost_impact_proposed_by": actor.actor_id,
                "cost_impact_proposal_reason": reason,
                "cost_impact_proposed_at": now.isoformat(),
                "cost_impact_source_term_id": source.id,
            },
            created_at=now,
            updated_at=now,
        )
        self.session.add(row)
        self.session.flush()
        approval = self._approval_service().plan(
            actor=actor,
            project_id=project.id,
            subjects=(
                ApprovalSubject(
                    entity_type="contract_term",
                    entity_id=row.id,
                    reasons=frozenset({ApprovalReason.CONTRACT_COST_IMPACT}),
                    monetary_value=command.amount,
                ),
            ),
            request_id=request_id,
            reason=reason,
        )
        task_ids = tuple(approval.task_ids_by_key.values())
        row.payload = {
            **row.payload,
            "approval_task_ids": list(task_ids),
            "approval_findings": [
                finding.model_dump(mode="json") for finding in approval.plan.findings
            ],
        }
        self._audit(
            project.id,
            actor,
            request_id,
            reason,
            "contract_cost_impact_proposed",
            {
                "term_id": row.id,
                "supersedes_term_id": source.id,
                "amount": command.amount,
                "currency": command.currency,
                "approval_task_ids": list(task_ids),
                "superseded_approval_task_ids": superseded_task_ids,
            },
        )
        return self._view(row)

    def finalize_cost_impact(
        self,
        *,
        actor: Actor,
        project_id: str,
        term_id: str,
        expected_term_updated_at: datetime,
        request_id: str,
        reason: str,
    ) -> tuple[ContractTermView, ContractValidationResult]:
        reason = self._reason(reason)
        project = self._require_editable_state(
            actor,
            project_id,
            required_roles=(
                ActorRole.ESTIMATOR,
                ActorRole.TECHNICAL_EXPERT,
                ActorRole.REVIEWER,
            ),
        )
        row = self._current_term(project.id, term_id)
        expected_updated_at = ensure_utc(expected_term_updated_at)
        assert expected_updated_at is not None
        if ensure_utc(row.updated_at) != expected_updated_at:
            raise OptimisticLockError(
                "Contract term changed after it was loaded; reload before finalizing cost impact"
            )
        if row.cost_impact_resolved:
            raise ValueError("Contract cost impact is already resolved")
        document_set = self._document_set(
            project.id,
            project.current_document_set_revision_id,
        )
        if row.payload.get("document_set_revision_id") != document_set.id:
            raise ValueError("Contract term belongs to a superseded document-set revision")
        policy, rules = self._requirements(project.id, project.organization_id)
        self._require_verified_term_integrity(
            row=row,
            policy=policy,
            rules=rules,
            document_set=document_set,
        )
        proposal = row.payload.get("cost_impact_proposal")
        if not isinstance(proposal, dict):
            raise ValueError("Contract term has no cost impact proposal")
        command = ContractCostImpactCommand.model_validate(proposal)
        if command.amount > 0:
            self._validate_contract_cost_component(
                project.id,
                document_set.id,
                command,
            )
        task_ids = tuple(row.payload.get("approval_task_ids", []))
        if not task_ids:
            raise ValueError("Contract cost impact has no approval task")
        tasks = list(
            self.session.scalars(
                select(ApprovalTaskRow).where(
                    ApprovalTaskRow.project_id == project.id,
                    ApprovalTaskRow.id.in_(task_ids),
                    ApprovalTaskRow.task_type == ApprovalReason.CONTRACT_COST_IMPACT.value,
                    ApprovalTaskRow.entity_type == "contract_term",
                    ApprovalTaskRow.entity_id == row.id,
                    ApprovalTaskRow.status == "APPROVED",
                )
            )
        )
        if len(tasks) != len(task_ids):
            raise ValueError("Contract cost impact approvals are incomplete")
        approval = self.session.scalar(
            select(ApprovalRecordRow)
            .where(
                ApprovalRecordRow.task_id.in_(task_ids),
                ApprovalRecordRow.decision == "APPROVED",
            )
            .order_by(ApprovalRecordRow.decided_at.desc())
        )
        if approval is None:
            raise ValueError("Approved contract task has no approval record")
        if approval.decided_by == row.payload.get("cost_impact_proposed_by"):
            raise ValueError("Contract cost impact approval violates four-eyes")
        now = utc_now()
        row.cost_impact_resolved = True
        row.updated_at = now
        row.payload = {
            **row.payload,
            "cost_impact": proposal,
            "cost_impact_approval_id": approval.id,
            "cost_impact_approved_by": approval.decided_by,
            "cost_impact_finalized_by": actor.actor_id,
            "cost_impact_finalized_at": now.isoformat(),
        }
        validation = self._validate_current(project.id, policy=policy, rules=rules)
        self._audit(
            project.id,
            actor,
            request_id,
            reason,
            "contract_cost_impact_finalized",
            {
                "term_id": row.id,
                "approval_record_id": approval.id,
                "approved_by": approval.decided_by,
                "expected_term_updated_at": expected_updated_at.isoformat(),
            },
        )
        return self._view(row), validation

    def validate_current(
        self,
        *,
        actor: Actor,
        project_id: str,
        request_id: str,
        reason: str,
    ) -> ContractValidationResult:
        reason = self._reason(reason)
        project = self._project_service().get_project(
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
        policy, rules = self._requirements(project_id, project.organization_id)
        self._document_set(project_id, project.current_document_set_revision_id)
        result = self._validate_current(project_id, policy=policy, rules=rules)
        self._audit(
            project_id,
            actor,
            request_id,
            reason,
            "contract_assessment_validated",
            {
                "assessment_version": result.assessment.assessment_version,
                "rules_version_id": result.rules_version_id,
                "finding_codes": [finding.code for finding in result.findings],
            },
        )
        return result

    def _validate_current(
        self,
        project_id: str,
        *,
        policy: ContractRequirementsPolicy,
        rules: ControlledVersionRow,
    ) -> ContractValidationResult:
        result = self._current_validation(project_id, policy=policy, rules=rules)
        self._replace_findings(
            project_id,
            result.findings,
            result.assessment.assessment_version,
            rules.id,
        )
        return result

    def _current_validation(
        self,
        project_id: str,
        *,
        policy: ContractRequirementsPolicy,
        rules: ControlledVersionRow,
    ) -> ContractValidationResult:
        rows = list(
            self.session.scalars(
                select(ContractTermRow)
                .where(
                    ContractTermRow.project_id == project_id,
                    ContractTermRow.is_current.is_(True),
                )
                .order_by(ContractTermRow.kind)
            )
        )
        terms = tuple(self._domain_term(row) for row in rows)
        assessment = ContractAssessment(
            assessment_version=content_hash(
                {
                    "term_ids": [row.id for row in rows],
                    "rules_version_id": rules.id,
                }
            ),
            terms=terms,
            required_term_kinds=policy.required_term_kinds,
        )
        findings = validate_contract(assessment)
        return ContractValidationResult(
            assessment=assessment,
            findings=findings,
            rules_version_id=rules.id,
        )

    def _requirements(
        self,
        project_id: str,
        organization_id: str,
        *,
        expected_version_id: str | None = None,
    ) -> tuple[ContractRequirementsPolicy, ControlledVersionRow]:
        rules = require_bound_controlled_version(
            session=self.session,
            settings=self.settings,
            project_id=project_id,
            organization_id=organization_id,
            purpose="contract_risk_rules",
            kind="contract_risk_rules",
            expected_version_id=expected_version_id,
        )
        contract = rules.payload.get("contract")
        if not isinstance(contract, dict):
            raise ValueError("Contract risk rules lack a contract section")
        return ContractRequirementsPolicy.model_validate(contract), rules

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
            raise ValueError("One or more contract evidence observations are missing")
        ordered = tuple(by_id[item] for item in observation_ids)
        if any(row.document_revision_id not in document_revision_ids for row in ordered):
            raise ValueError("Contract evidence must belong to the confirmed current document set")
        return ordered

    @classmethod
    def _validate_observation_values(
        cls,
        draft: ContractTermDraft,
        observations: tuple[ObservationRow, ...],
        *,
        expected_field_name: str,
        require_eligible_status: bool = True,
    ) -> None:
        for row in observations:
            observation = cls._observation(row)
            if require_eligible_status and observation.status not in {
                VerificationStatus.UNVERIFIED,
                VerificationStatus.VERIFIED,
            }:
                raise ValueError("Contract evidence status is not eligible")
            if (
                require_eligible_status
                and observation.method is EvidenceMethod.MANUAL
                and observation.status is not VerificationStatus.VERIFIED
            ):
                raise ValueError("Manual contract evidence requires its dedicated review first")
            if observation.field_name != expected_field_name:
                raise ValueError("Contract evidence belongs to another field")
            if observation.value != draft.value:
                raise ValueError("Contract evidence observations do not reproduce the term")
            if observation.unit is not None:
                raise ValueError("Contract term evidence must not carry a measurement unit")

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
            raise ValueError("Contract evidence row does not reproduce its payload")
        return observation

    def _candidate_view(
        self,
        *,
        row: ObservationRow,
        organization_id: str,
        independent: bool,
        document_revision_ids: frozenset[str],
    ) -> ContractEvidenceCandidateView:
        blockers: list[str] = []
        try:
            observation = self._observation(row)
        except (KeyError, TypeError, ValueError):
            observation = Observation.model_validate(row.payload.get("observation"))
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
        return ContractEvidenceCandidateView(
            observation=observation,
            adapter_qualification_id=(
                qualification_id if isinstance(qualification_id, str) else None
            ),
            adapter_status=qualification.status if qualification is not None else None,
            adapter_valid_until=(qualification.valid_until if qualification is not None else None),
            independence_domain=domain if isinstance(domain, str) else None,
            eligible=not blockers,
            blockers=tuple(dict.fromkeys(blockers)),
        )

    def _require_verified_term_integrity(
        self,
        *,
        row: ContractTermRow,
        policy: ContractRequirementsPolicy,
        rules: ControlledVersionRow,
        document_set: DocumentSetRevisionRow,
    ) -> None:
        draft = self._draft(row)
        if draft.kind not in policy.required_term_kinds:
            raise ValueError("Verified contract term is outside the approved requirements")
        field_name = policy.evidence_field_names[draft.kind]
        created_by = row.payload.get("created_by")
        verified_by = row.payload.get("verified_by")
        if (
            not row.is_current
            or not row.verified
            or not isinstance(created_by, str)
            or not created_by
            or not isinstance(verified_by, str)
            or not verified_by
            or created_by == verified_by
            or row.payload.get("reviewed_by") != verified_by
            or row.payload.get("review_decision") != "APPROVED"
            or row.payload.get("evidence_field_name") != field_name
            or row.payload.get("rules_version_id") != rules.id
            or row.payload.get("rules_content_hash") != rules.content_hash
            or row.payload.get("document_set_revision_id") != document_set.id
            or row.payload.get("review_role") != policy.review_role.value
        ):
            raise ValueError("Verified contract term provenance is invalid")
        observations = self._observations(
            row.project_id,
            draft.observation_ids,
            document_revision_ids=frozenset(document_set.revision_ids),
        )
        self._validate_observation_values(
            draft,
            observations,
            expected_field_name=field_name,
        )
        provenance_leaves = resolve_observation_leaves(
            self.session,
            project_id=row.project_id,
            observations=observations,
        )
        self._validate_observation_values(
            draft,
            provenance_leaves,
            expected_field_name=field_name,
            require_eligible_status=False,
        )
        independence_source_ids = tuple(row.payload.get("independence_source_ids", []))
        if draft.kind in policy.independently_verified_term_kinds:
            leaves = require_distinct_qualified_independence(
                self.session,
                project_id=row.project_id,
                observations=observations,
            )
            if independence_source_ids != leaves or leaves != tuple(
                item.id for item in provenance_leaves
            ):
                raise ValueError("Verified contract independence evidence changed")
            self._validate_observation_values(
                draft,
                provenance_leaves,
                expected_field_name=field_name,
            )
        task = self._review_task(row)
        if (
            task is None
            or task.task_type != "CONTRACT_TERM_REVIEW"
            or task.entity_type != "contract_term"
            or task.assigned_role != policy.review_role.value
            or not task.required
            or task.status != "APPROVED"
            or task.payload.get("created_by") != created_by
            or task.payload.get("observation_ids") != list(draft.observation_ids)
            or task.payload.get("independence_source_ids") != list(independence_source_ids)
            or task.payload.get("rules_version_id") != rules.id
            or task.payload.get("rules_content_hash") != rules.content_hash
            or task.payload.get("document_set_revision_id") != document_set.id
            or task.payload.get("review_role") != policy.review_role.value
            or not self._supersession_chain_contains(row, task.entity_id)
        ):
            raise ValueError("Verified contract review task integrity failed")
        expected_hash = content_hash(
            {
                "kind": row.kind,
                "value": row.payload.get("value"),
                "evidence_field_name": field_name,
                "observation_ids": list(draft.observation_ids),
                "independence_source_ids": list(independence_source_ids),
                "created_by": created_by,
                "rules_version_id": rules.id,
                "rules_content_hash": rules.content_hash,
                "document_set_revision_id": document_set.id,
                "review_role": policy.review_role.value,
            }
        )
        if task.payload.get("term_submission_hash") != expected_hash:
            raise ValueError("Verified contract submission hash changed")
        approval = self.session.scalar(
            select(ApprovalRecordRow).where(ApprovalRecordRow.task_id == task.id)
        )
        if (
            approval is None
            or approval.decision != "APPROVED"
            or approval.decided_by != verified_by
            or approval.payload.get("term_id") != task.entity_id
            or approval.payload.get("kind") != row.kind
            or approval.payload.get("evidence_ids") != list(draft.observation_ids)
            or approval.payload.get("independence_source_ids") != list(independence_source_ids)
            or approval.payload.get("rules_version_id") != rules.id
            or approval.payload.get("rules_content_hash") != rules.content_hash
            or approval.payload.get("document_set_revision_id") != document_set.id
        ):
            raise ValueError("Verified contract approval record integrity failed")

    def _supersession_chain_contains(
        self,
        current: ContractTermRow,
        ancestor_id: str,
    ) -> bool:
        seen: set[str] = set()
        candidate: ContractTermRow | None = current
        while candidate is not None and candidate.id not in seen:
            seen.add(candidate.id)
            if candidate.id == ancestor_id:
                return True
            candidate = (
                self.session.get(ContractTermRow, candidate.supersedes_term_id)
                if candidate.supersedes_term_id
                else None
            )
        return False

    def _review_view(
        self,
        *,
        actor: Actor,
        project_state: ApprovalState,
        row: ContractTermRow,
        policy: ContractRequirementsPolicy,
        rules: ControlledVersionRow,
        document_set: DocumentSetRevisionRow,
    ) -> ContractTermReviewView:
        task = self._review_task(row)
        if task is None:
            return ContractTermReviewView(
                term=self._view(row),
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
            rules=rules,
            document_set=document_set,
        )
        return ContractTermReviewView(
            term=self._view(row),
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
        row: ContractTermRow,
        task: ApprovalTaskRow,
        policy: ContractRequirementsPolicy,
        rules: ControlledVersionRow,
        document_set: DocumentSetRevisionRow,
    ) -> list[str]:
        blockers: list[str] = []
        if project_state not in _EDITABLE_STATES:
            blockers.append("PROJECT_STATE_NOT_ALLOWED")
        if not row.is_current:
            blockers.append("TERM_SUPERSEDED")
        if row.verified:
            blockers.append("TERM_ALREADY_REVIEWED")
            try:
                self._require_verified_term_integrity(
                    row=row,
                    policy=policy,
                    rules=rules,
                    document_set=document_set,
                )
            except (KeyError, LookupError, TypeError, ValueError):
                blockers.append("TERM_INTEGRITY_FAILED")
            return list(dict.fromkeys(blockers))
        if row.payload.get("review_decision") is not None:
            blockers.append("TERM_NOT_PENDING")
        if row.payload.get("created_by") == actor.actor_id:
            blockers.append("FOUR_EYES_TERM_AUTHOR")
        if policy.review_role not in actor.roles:
            blockers.append("REVIEW_ROLE_REQUIRED")
        if row.payload.get("rules_version_id") != rules.id:
            blockers.append("RULES_VERSION_CHANGED")
        if row.payload.get("rules_content_hash") != rules.content_hash:
            blockers.append("RULES_HASH_MISMATCH")
        if row.payload.get("document_set_revision_id") != document_set.id:
            blockers.append("DOCUMENT_SET_CHANGED")
        expected_payload = self._review_task_payload(
            row=row,
            policy=policy,
            rules=rules,
            document_set=document_set,
        )
        if (
            task.project_id != row.project_id
            or task.task_type != "CONTRACT_TERM_REVIEW"
            or task.entity_type != "contract_term"
            or task.entity_id != row.id
            or task.assigned_role != policy.review_role.value
            or not task.required
            or task.payload != expected_payload
        ):
            blockers.append("TASK_INTEGRITY_FAILED")
        if task.status != "PENDING":
            blockers.append("TASK_NOT_PENDING")
        if task.payload.get("created_by") == actor.actor_id:
            blockers.append("FOUR_EYES_TASK_CREATOR")
        try:
            draft = self._draft(row)
            field_name = policy.evidence_field_names[draft.kind]
            if self._unresolved_conflicts(row.project_id, field_name):
                blockers.append("UNRESOLVED_EVIDENCE_CONFLICT")
            observations = self._observations(
                row.project_id,
                draft.observation_ids,
                document_revision_ids=frozenset(document_set.revision_ids),
            )
            self._validate_observation_values(
                draft,
                observations,
                expected_field_name=field_name,
            )
            provenance_leaves = resolve_observation_leaves(
                self.session,
                project_id=row.project_id,
                observations=observations,
            )
            self._validate_observation_values(
                draft,
                provenance_leaves,
                expected_field_name=field_name,
                require_eligible_status=False,
            )
            if draft.kind in policy.independently_verified_term_kinds:
                leaves = require_distinct_qualified_independence(
                    self.session,
                    project_id=row.project_id,
                    observations=observations,
                )
                if tuple(row.payload.get("independence_source_ids", [])) != leaves:
                    blockers.append("INDEPENDENCE_EVIDENCE_CHANGED")
                if leaves != tuple(item.id for item in provenance_leaves):
                    blockers.append("INDEPENDENCE_RESOLUTION_CHANGED")
                self._validate_observation_values(
                    draft,
                    provenance_leaves,
                    expected_field_name=field_name,
                )
        except (KeyError, LookupError, TypeError, ValueError):
            blockers.append("EVIDENCE_INTEGRITY_FAILED")
        return list(dict.fromkeys(blockers))

    def _ensure_review_task(
        self,
        *,
        row: ContractTermRow,
        policy: ContractRequirementsPolicy,
        rules: ControlledVersionRow,
        document_set: DocumentSetRevisionRow,
    ) -> ApprovalTaskRow:
        task_id = (
            "approval-task-contract-"
            + content_hash(
                {
                    "term_id": row.id,
                    "rules_version_id": rules.id,
                }
            )[:24]
        )
        if self.session.get(ApprovalTaskRow, task_id) is not None:
            raise RuntimeError("Contract review task identifier collision")
        task = ApprovalTaskRow(
            id=task_id,
            project_id=row.project_id,
            task_type="CONTRACT_TERM_REVIEW",
            entity_type="contract_term",
            entity_id=row.id,
            assigned_role=policy.review_role.value,
            status="PENDING",
            required=True,
            payload=self._review_task_payload(
                row=row,
                policy=policy,
                rules=rules,
                document_set=document_set,
            ),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        self.session.add(task)
        return task

    @staticmethod
    def _review_task_payload(
        *,
        row: ContractTermRow,
        policy: ContractRequirementsPolicy,
        rules: ControlledVersionRow,
        document_set: DocumentSetRevisionRow,
    ) -> dict[str, Any]:
        submission = {
            "kind": row.kind,
            "value": row.payload.get("value"),
            "evidence_field_name": row.payload.get("evidence_field_name"),
            "observation_ids": row.payload.get("observation_ids", []),
            "independence_source_ids": row.payload.get("independence_source_ids", []),
            "created_by": row.payload.get("created_by"),
            "rules_version_id": rules.id,
            "rules_content_hash": rules.content_hash,
            "document_set_revision_id": document_set.id,
            "review_role": policy.review_role.value,
        }
        return {
            "created_by": row.payload.get("created_by"),
            "term_id": row.id,
            "term_submission_hash": content_hash(submission),
            "observation_ids": list(row.payload.get("observation_ids", [])),
            "independence_source_ids": list(row.payload.get("independence_source_ids", [])),
            "rules_version_id": rules.id,
            "rules_content_hash": rules.content_hash,
            "document_set_revision_id": document_set.id,
            "review_role": policy.review_role.value,
        }

    def _review_task(
        self,
        row: ContractTermRow,
        *,
        lock: bool = False,
    ) -> ApprovalTaskRow | None:
        task_id = row.payload.get("approval_task_id")
        if not isinstance(task_id, str) or not task_id:
            return None
        statement = select(ApprovalTaskRow).where(
            ApprovalTaskRow.id == task_id,
            ApprovalTaskRow.project_id == row.project_id,
        )
        if lock:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def _tasks_for_term(
        self,
        row: ContractTermRow,
        *,
        lock: bool = False,
    ) -> tuple[ApprovalTaskRow, ...]:
        ids = {
            item
            for item in (
                row.payload.get("approval_task_id"),
                *row.payload.get("approval_task_ids", []),
            )
            if isinstance(item, str) and item
        }
        if not ids:
            return ()
        statement = select(ApprovalTaskRow).where(
            ApprovalTaskRow.project_id == row.project_id,
            ApprovalTaskRow.id.in_(ids),
        )
        if lock:
            statement = statement.with_for_update()
        return tuple(self.session.scalars(statement))

    def _cost_tasks_for_term(
        self,
        row: ContractTermRow,
        *,
        lock: bool = False,
    ) -> tuple[ApprovalTaskRow, ...]:
        ids = tuple(
            item
            for item in row.payload.get("approval_task_ids", [])
            if isinstance(item, str) and item
        )
        if not ids:
            return ()
        statement = select(ApprovalTaskRow).where(
            ApprovalTaskRow.project_id == row.project_id,
            ApprovalTaskRow.id.in_(ids),
            ApprovalTaskRow.task_type == ApprovalReason.CONTRACT_COST_IMPACT.value,
        )
        if lock:
            statement = statement.with_for_update()
        return tuple(self.session.scalars(statement))

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

    def _impact_candidates(
        self,
        *,
        project_id: str,
        document_set_revision_id: str,
    ) -> tuple[ContractCostImpactCandidateView, ...]:
        models = tuple(
            self.session.scalars(
                select(CommercialCostModelRow)
                .where(
                    CommercialCostModelRow.project_id == project_id,
                    CommercialCostModelRow.status == "VALIDATED",
                    CommercialCostModelRow.is_current.is_(True),
                    CommercialCostModelRow.model_kind == "CONTRACT_FINANCE",
                    CommercialCostModelRow.category == CostCategory.CONTRACT_FINANCE.value,
                    CommercialCostModelRow.document_set_revision_id == document_set_revision_id,
                )
                .order_by(CommercialCostModelRow.id)
            )
        )
        candidates: list[ContractCostImpactCandidateView] = []
        for model in models:
            line = self.session.get(BoqLineRow, model.target_line_id)
            components = line.payload.get("cost_components") if line is not None else None
            component = (
                next(
                    (
                        item
                        for item in components
                        if isinstance(item, dict)
                        and item.get("semantic_key") == model.target_semantic_key
                    ),
                    None,
                )
                if isinstance(components, list)
                else None
            )
            blockers: list[str] = []
            if (
                line is None
                or line.project_id != project_id
                or not isinstance(component, dict)
                or component.get("category") != CostCategory.CONTRACT_FINANCE.value
                or component.get("basis_kind") != "DERIVED_MODEL"
            ):
                blockers.append("COST_COMPONENT_INTEGRITY_FAILED")
            candidates.append(
                ContractCostImpactCandidateView(
                    derived_cost_model_id=model.id,
                    amount=model.total,
                    currency=model.currency,
                    cost_component_line_id=model.target_line_id,
                    cost_component_semantic_key=model.target_semantic_key,
                    eligible=not blockers,
                    blockers=tuple(blockers),
                )
            )
        return tuple(candidates)

    def _validate_contract_cost_component(
        self,
        project_id: str,
        document_set_revision_id: str | None,
        command: ContractCostImpactCommand,
    ) -> None:
        commercial_policy_id = self.session.scalar(
            select(ControlledVersionRow.id)
            .join(
                ProjectControlledVersionRow,
                ProjectControlledVersionRow.controlled_version_id == ControlledVersionRow.id,
            )
            .where(
                ProjectControlledVersionRow.project_id == project_id,
                ProjectControlledVersionRow.purpose == "commercial_cost_model",
                ControlledVersionRow.kind == "commercial_cost_model",
                ControlledVersionRow.status == VersionStatus.APPROVED.value,
            )
        )
        if document_set_revision_id is None or commercial_policy_id is None:
            raise ValueError(
                "Contract impact requires a current document set and commercial cost policy"
            )
        line = self.session.scalar(
            select(BoqLineRow).where(
                BoqLineRow.id == command.cost_component_line_id,
                BoqLineRow.project_id == project_id,
            )
        )
        if line is None:
            raise ValueError("Contract cost component BoQ line does not exist")
        components = line.payload.get("cost_components")
        match = (
            next(
                (
                    item
                    for item in components
                    if isinstance(item, dict)
                    and item.get("semantic_key") == command.cost_component_semantic_key
                ),
                None,
            )
            if isinstance(components, list)
            else None
        )
        if (
            not isinstance(match, dict)
            or match.get("category") != CostCategory.CONTRACT_FINANCE.value
            or match.get("basis_kind") != "DERIVED_MODEL"
        ):
            raise ValueError("Contract impact must reference a derived CONTRACT_FINANCE component")
        model = self.session.scalar(
            select(CommercialCostModelRow).where(
                CommercialCostModelRow.id == command.derived_cost_model_id,
                CommercialCostModelRow.project_id == project_id,
                CommercialCostModelRow.status == "VALIDATED",
                CommercialCostModelRow.is_current.is_(True),
                CommercialCostModelRow.model_kind == "CONTRACT_FINANCE",
                CommercialCostModelRow.target_line_id == command.cost_component_line_id,
                CommercialCostModelRow.target_semantic_key == command.cost_component_semantic_key,
                CommercialCostModelRow.document_set_revision_id == document_set_revision_id,
                CommercialCostModelRow.policy_version_id == commercial_policy_id,
                CommercialCostModelRow.total == command.amount,
                CommercialCostModelRow.currency == command.currency,
            )
        )
        if model is None:
            raise ValueError("Contract impact does not reproduce a current validated finance model")

    def _current_term(self, project_id: str, term_id: str) -> ContractTermRow:
        row = self.session.scalar(
            select(ContractTermRow)
            .where(
                ContractTermRow.id == term_id,
                ContractTermRow.project_id == project_id,
                ContractTermRow.is_current.is_(True),
            )
            .with_for_update()
        )
        if row is None:
            raise LookupError(term_id)
        return row

    def _replace_findings(
        self,
        project_id: str,
        findings: tuple[ValidationFinding, ...],
        assessment_version: str,
        rules_version_id: str,
    ) -> None:
        now = utc_now()
        prior = list(
            self.session.scalars(
                select(VerificationFindingRow).where(
                    VerificationFindingRow.project_id == project_id,
                    VerificationFindingRow.contour == "CONTRACT",
                    VerificationFindingRow.resolved.is_(False),
                )
            )
        )
        for old_finding in prior:
            old_finding.resolved = True
            old_finding.updated_at = now
            old_finding.payload = {
                **old_finding.payload,
                "resolved_by_assessment_version": assessment_version,
                "resolved_at": now.isoformat(),
            }
        for finding in findings:
            identity = {
                "project_id": project_id,
                "contour": "CONTRACT",
                "finding": finding,
            }
            finding_id = f"finding-{content_hash(identity)[:24]}"
            existing = self.session.get(VerificationFindingRow, finding_id)
            payload = {
                **finding.model_dump(mode="json"),
                "assessment_version": assessment_version,
                "rules_version_id": rules_version_id,
            }
            if existing is None:
                self.session.add(
                    VerificationFindingRow(
                        id=finding_id,
                        project_id=project_id,
                        contour="CONTRACT",
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

    def _require_editable_state(
        self,
        actor: Actor,
        project_id: str,
        *,
        required_roles: tuple[ActorRole, ...],
    ) -> Any:
        project = self._project_service().get_project(
            actor=actor,
            project_id=project_id,
            lock=True,
            required_roles=required_roles,
        )
        if ApprovalState(project.state) not in _EDITABLE_STATES:
            raise ValueError("Contract terms must be resolved before calculation")
        return project

    def _audit(
        self,
        project_id: str,
        actor: Actor,
        request_id: str,
        reason: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        self._project_service().record_event(
            aggregate_type="project",
            aggregate_id=project_id,
            event_type=event_type,
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload=payload,
        )

    def _approval_service(self) -> ApprovalService:
        return ApprovalService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        )

    def _project_service(self) -> ProjectService:
        return ProjectService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        )

    @staticmethod
    def _domain_term(row: ContractTermRow) -> ContractTerm:
        impact = row.payload.get("cost_impact")
        impact_payload = impact if isinstance(impact, dict) else {}
        component_line = impact_payload.get("cost_component_line_id")
        component_key = impact_payload.get("cost_component_semantic_key")
        cost_input_id = (
            f"{component_line}:{component_key}" if component_line and component_key else None
        )
        return ContractTerm(
            term_id=row.id,
            kind=ContractTermKind(row.kind),
            value=str(row.payload.get("value")),
            observation_ids=tuple(row.payload.get("observation_ids", [])),
            independence_source_ids=tuple(row.payload.get("independence_source_ids", [])),
            verified=row.verified,
            cost_impact_resolved=row.cost_impact_resolved,
            cost_impact_amount=(
                Decimal(str(impact_payload["amount"])) if "amount" in impact_payload else None
            ),
            cost_impact_currency=impact_payload.get("currency"),
            cost_input_id=cost_input_id,
            approved_assumption_id=row.payload.get("cost_impact_approval_id"),
            derived_cost_model_id=impact_payload.get("derived_cost_model_id"),
        )

    def _view(self, row: ContractTermRow) -> ContractTermView:
        created_by = row.payload.get("created_by")
        rules_version_id = row.payload.get("rules_version_id")
        document_set_revision_id = row.payload.get("document_set_revision_id")
        approval_task_id = row.payload.get("approval_task_id")
        if not all(
            isinstance(item, str) and item
            for item in (
                created_by,
                rules_version_id,
                document_set_revision_id,
                approval_task_id,
            )
        ):
            raise ValueError("Contract term provenance is incomplete")
        updated_at = ensure_utc(row.updated_at)
        if updated_at is None:
            raise ValueError("Contract term update timestamp is missing")
        proposal_payload = row.payload.get("cost_impact_proposal")
        proposal = (
            ContractCostImpactCommand.model_validate(proposal_payload)
            if isinstance(proposal_payload, dict)
            else None
        )
        task_ids = tuple(row.payload.get("approval_task_ids", []))
        task_statuses = {
            task.id: task.status
            for task in self.session.scalars(
                select(ApprovalTaskRow).where(
                    ApprovalTaskRow.project_id == row.project_id,
                    ApprovalTaskRow.id.in_(task_ids),
                )
            )
        }
        finalized_at_raw = row.payload.get("cost_impact_finalized_at")
        finalized_at = (
            ensure_utc(datetime.fromisoformat(finalized_at_raw))
            if isinstance(finalized_at_raw, str)
            else None
        )
        return ContractTermView(
            term_id=row.id,
            kind=ContractTermKind(row.kind),
            value=str(row.payload.get("value")),
            observation_ids=tuple(row.payload.get("observation_ids", [])),
            verified=row.verified,
            cost_impact_resolved=row.cost_impact_resolved,
            supersedes_term_id=row.supersedes_term_id,
            is_current=row.is_current,
            independence_source_ids=tuple(row.payload.get("independence_source_ids", [])),
            created_by=created_by,
            verified_by=row.payload.get("verified_by"),
            rules_version_id=rules_version_id,
            document_set_revision_id=document_set_revision_id,
            approval_task_id=approval_task_id,
            updated_at=updated_at,
            approval_task_ids=task_ids,
            cost_impact_proposal=proposal,
            cost_impact_task_statuses=task_statuses,
            cost_impact_approved_by=row.payload.get("cost_impact_approved_by"),
            cost_impact_finalized_at=finalized_at,
        )

    @staticmethod
    def _draft(row: ContractTermRow) -> ContractTermDraft:
        return ContractTermDraft.model_validate(
            {
                "kind": row.kind,
                "value": row.payload.get("value"),
                "observation_ids": row.payload.get("observation_ids"),
            }
        )

    @staticmethod
    def _reason(reason: str) -> str:
        normalized = reason.strip()
        if not normalized or len(normalized) > 2000:
            raise ValueError("Contract workflow reason must contain 1 to 2000 characters")
        return normalized

    @staticmethod
    def _timestamp(value: datetime) -> datetime:
        normalized = ensure_utc(value)
        if normalized is None:
            raise ValueError("Contract workflow timestamp is missing")
        return normalized
