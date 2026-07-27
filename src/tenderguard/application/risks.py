from __future__ import annotations

from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
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
    CostBasisKind,
    CostCategory,
    EvidenceMethod,
    Severity,
    VerificationStatus,
)
from tenderguard.domain.models import DomainModel, Observation, ValidationFinding
from tenderguard.domain.risk import (
    RiskCalculation,
    RiskItem,
    RiskModelDefinition,
    calculate_risk_reserve,
)
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.object_store import ObjectStore
from tenderguard.infrastructure.orm import (
    AdapterQualificationRow,
    ApprovalRecordRow,
    ApprovalTaskRow,
    AuditEventRow,
    BoqLineRow,
    ConflictRow,
    ControlledVersionRow,
    DocumentSetRevisionRow,
    ObservationRow,
    ProjectRow,
    RiskCalculationRow,
    RiskItemRow,
    VerificationFindingRow,
)

_EDITABLE_STATES = frozenset(
    {
        ApprovalState.BOQ_IN_PROGRESS,
        ApprovalState.BOQ_REVIEW,
        ApprovalState.PRICING_IN_PROGRESS,
        ApprovalState.RFQ_REQUIRED,
    }
)


class RiskItemDraft(DomainModel):
    risk_key: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=4000)
    probability: Decimal = Field(ge=0, le=1)
    impact_min: Decimal = Field(ge=0)
    impact_most_likely: Decimal = Field(ge=0)
    impact_max: Decimal = Field(ge=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    observation_ids: tuple[str, ...] = Field(min_length=1, max_length=20)
    correlated: bool = False
    correlation_group: str | None = Field(default=None, max_length=128)
    mitigation_cost_input_id: str | None = Field(default=None, max_length=64)

    @field_validator("risk_key")
    @classmethod
    def risk_key_is_normalized(cls, value: str) -> str:
        if value != value.strip() or any(character.isspace() for character in value):
            raise ValueError("Risk key must be normalized and contain no whitespace")
        return value

    @field_validator("description")
    @classmethod
    def description_is_normalized(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("Risk description must be normalized")
        return value

    @field_validator("observation_ids")
    @classmethod
    def observation_ids_are_normalized(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("Risk observation IDs must be unique")
        if any(
            not value
            or value != value.strip()
            or len(value) > 64
            or any(character.isspace() for character in value)
            for value in values
        ):
            raise ValueError("Risk observation ID is invalid")
        return values

    @model_validator(mode="after")
    def impacts_and_correlation_are_valid(self) -> RiskItemDraft:
        if not self.impact_min <= self.impact_most_likely <= self.impact_max:
            raise ValueError("Risk impacts must be ordered min <= likely <= max")
        if self.correlated and not self.correlation_group:
            raise ValueError("Correlated risk requires a correlation group")
        if not self.correlated and self.correlation_group is not None:
            raise ValueError("Uncorrelated risk cannot declare a correlation group")
        return self

    def evidence_value(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"observation_ids"})


class RiskItemView(DomainModel):
    row_id: str
    risk_key: str
    risk: RiskItem
    independence_source_ids: tuple[str, ...]
    supersedes_risk_id: str | None
    is_current: bool
    created_by: str
    verified_by: str | None = None
    risk_model_version_id: str
    risk_model_content_hash: str
    document_set_revision_id: str
    approval_task_id: str
    updated_at: datetime


class RiskCalculationView(DomainModel):
    calculation_id: str
    calculation: RiskCalculation
    status: str
    input_signature: str
    output_hash: str
    independent_validation_passed: bool
    risk_item_ids: tuple[str, ...]
    document_set_revision_id: str
    supersedes_calculation_id: str | None
    created_at: datetime


class RiskEvidenceCandidateView(DomainModel):
    observation: Observation
    draft: RiskItemDraft | None
    adapter_qualification_id: str | None
    adapter_status: str | None
    adapter_valid_until: date | None = None
    independence_domain: str | None
    eligible: bool
    blockers: tuple[str, ...] = ()


class RiskItemReviewView(DomainModel):
    item: RiskItemView
    task_status: str
    task_updated_at: datetime
    assigned_role: ActorRole
    decision_allowed: bool
    decision_blockers: tuple[str, ...]


class RiskContextView(DomainModel):
    project_id: str
    project_state: ApprovalState
    document_set_revision_id: str
    risk_model_version_id: str
    risk_model_content_hash: str
    risk_keys: tuple[str, ...]
    required_risk_keys: tuple[str, ...]
    independently_verified_risk_keys: tuple[str, ...]
    evidence_field_names: dict[str, str]
    review_role: ActorRole
    minimum_risk_items: int
    selected_risk_key: str
    items: tuple[RiskItemReviewView, ...]
    evidence_candidates: tuple[RiskEvidenceCandidateView, ...]
    candidates_truncated: bool
    current_calculation: RiskCalculationView | None
    calculation_blockers: tuple[str, ...]
    unresolved_conflict_ids: tuple[str, ...]


class RiskItemDecisionCommand(DomainModel):
    decision: ApprovalDecision
    expected_risk_updated_at: datetime
    expected_task_updated_at: datetime

    @field_validator("decision")
    @classmethod
    def decision_is_terminal(cls, value: ApprovalDecision) -> ApprovalDecision:
        if value not in {ApprovalDecision.APPROVED, ApprovalDecision.REJECTED}:
            raise ValueError(
                "Risk review decision must be APPROVED or REJECTED; "
                "a revision is submitted as a new superseding risk item"
            )
        return value

    @field_validator("expected_risk_updated_at", "expected_task_updated_at")
    @classmethod
    def timestamps_are_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Expected timestamps must include a timezone")
        return value


class RiskItemDecisionResult(DomainModel):
    item: RiskItemView
    approval_id: str
    decision: ApprovalDecision


class RiskService:
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
        selected_risk_key: str | None,
        limit: int,
    ) -> RiskContextView:
        if limit < 1 or limit > 100:
            raise ValueError("Risk context limit must be between 1 and 100")
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
            project.id,
            project.current_document_set_revision_id,
        )
        definition, model = self._risk_model(
            project.id,
            actor.organization_id,
        )
        selected = selected_risk_key or definition.risk_keys[0]
        if selected not in definition.risk_keys:
            raise ValueError("Selected risk key is outside the approved risk model")
        item_rows = tuple(
            self.session.scalars(
                select(RiskItemRow)
                .where(
                    RiskItemRow.project_id == project.id,
                    RiskItemRow.is_current.is_(True),
                )
                .order_by(RiskItemRow.risk_key)
            )
        )
        reviews = tuple(
            self._review_view(
                actor=actor,
                project_state=ApprovalState(project.state),
                row=row,
                definition=definition,
                model=model,
                document_set=document_set,
            )
            for row in item_rows
        )
        candidate_rows = list(
            self.session.scalars(
                select(ObservationRow)
                .where(
                    ObservationRow.project_id == project.id,
                    ObservationRow.document_revision_id.in_(
                        tuple(document_set.revision_ids)
                    ),
                    ObservationRow.field_name
                    == definition.evidence_field_names[selected],
                )
                .order_by(ObservationRow.created_at.desc(), ObservationRow.id)
                .limit(limit + 1)
            )
        )
        selected_source_ids = {
            str(source_id)
            for row in item_rows
            if row.risk_key == selected
            for source_id in row.payload.get("observation_ids", [])
        }
        visible = {row.id: row for row in candidate_rows[:limit]}
        if selected_source_ids.difference(visible):
            for row in self.session.scalars(
                select(ObservationRow).where(
                    ObservationRow.project_id == project.id,
                    ObservationRow.id.in_(selected_source_ids.difference(visible)),
                )
            ):
                visible[row.id] = row
        independent = selected in definition.independently_verified_risk_keys
        candidates = tuple(
            self._candidate_view(
                row=row,
                risk_key=selected,
                definition=definition,
                organization_id=actor.organization_id,
                document_revision_ids=frozenset(document_set.revision_ids),
                independent=independent,
            )
            for row in sorted(
                visible.values(),
                key=lambda item: (item.created_at, item.id),
                reverse=True,
            )
        )
        current_calculation_row = self.session.scalar(
            select(RiskCalculationRow).where(
                RiskCalculationRow.project_id == project.id,
                RiskCalculationRow.is_current.is_(True),
            )
        )
        current_calculation: RiskCalculationView | None = None
        calculation_blockers = list(
            self._calculation_blockers(
                project_id=project.id,
                rows=item_rows,
                definition=definition,
                model=model,
                document_set=document_set,
            )
        )
        if current_calculation_row is not None:
            try:
                current_calculation = self._calculation_view(current_calculation_row)
            except (KeyError, TypeError, ValueError):
                calculation_blockers.append("RISK_CALCULATION_INTEGRITY_FAILED")
        unresolved = self._unresolved_conflicts(
            project.id,
            definition.evidence_field_names[selected],
        )
        return RiskContextView(
            project_id=project.id,
            project_state=ApprovalState(project.state),
            document_set_revision_id=document_set.id,
            risk_model_version_id=model.id,
            risk_model_content_hash=model.content_hash,
            risk_keys=definition.risk_keys,
            required_risk_keys=definition.required_risk_keys,
            independently_verified_risk_keys=(
                definition.independently_verified_risk_keys
            ),
            evidence_field_names=definition.evidence_field_names,
            review_role=definition.review_role,
            minimum_risk_items=definition.minimum_risk_items,
            selected_risk_key=selected,
            items=reviews,
            evidence_candidates=candidates,
            candidates_truncated=len(candidate_rows) > limit,
            current_calculation=current_calculation,
            calculation_blockers=tuple(dict.fromkeys(calculation_blockers)),
            unresolved_conflict_ids=tuple(row.id for row in unresolved),
        )

    def submit_risk(
        self,
        *,
        actor: Actor,
        project_id: str,
        draft: RiskItemDraft,
        expected_document_set_revision_id: str,
        risk_model_version_id: str,
        request_id: str,
        reason: str,
    ) -> RiskItemView:
        reason = self._reason(reason)
        project_service = self._project_service()
        project = project_service.get_project(
            actor=actor,
            project_id=project_id,
            lock=True,
            required_roles=(ActorRole.ESTIMATOR, ActorRole.TECHNICAL_EXPERT),
        )
        self._require_editable_state(project)
        document_set = self._document_set(
            project.id,
            project.current_document_set_revision_id,
        )
        if document_set.id != expected_document_set_revision_id:
            raise OptimisticLockError(
                "Current document set changed after risk context was loaded"
            )
        definition, model = self._risk_model(
            project.id,
            actor.organization_id,
            expected_version_id=risk_model_version_id,
        )
        if draft.risk_key not in definition.risk_keys:
            raise ValueError("Risk key is outside the approved risk model")
        if draft.correlated:
            raise ValueError(
                "Correlated risk submission is blocked until a qualified "
                "correlation calculation engine is integrated"
            )
        field_name = definition.evidence_field_names[draft.risk_key]
        if self._unresolved_conflicts(project.id, field_name):
            raise ValueError(
                "Risk cannot be submitted while its evidence conflict is unresolved"
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
        leaves = resolve_observation_leaves(
            self.session,
            project_id=project.id,
            observations=observations,
        )
        self._validate_observation_values(
            draft,
            leaves,
            expected_field_name=field_name,
            require_eligible_status=False,
        )
        independence_source_ids = tuple(row.id for row in leaves)
        if draft.risk_key in definition.independently_verified_risk_keys:
            qualified = require_distinct_qualified_independence(
                self.session,
                project_id=project.id,
                observations=observations,
            )
            if qualified != independence_source_ids:
                raise ValueError("Risk independence leaf resolution is inconsistent")
            self._validate_observation_values(
                draft,
                leaves,
                expected_field_name=field_name,
            )

        risk_id = f"risk-item-{uuid4()}"
        previous = self.session.scalar(
            select(RiskItemRow)
            .where(
                RiskItemRow.project_id == project.id,
                RiskItemRow.risk_key == draft.risk_key,
                RiskItemRow.is_current.is_(True),
            )
            .with_for_update()
        )
        now = utc_now()
        superseded_task_id: str | None = None
        if previous is not None:
            previous.is_current = False
            previous.updated_at = now
            previous_task = self._task_for_item(previous, lock=True)
            if previous_task is not None and previous_task.status != "SUPERSEDED":
                superseded_task_id = previous_task.id
                previous_task.status = "SUPERSEDED"
                previous_task.payload = {
                    **previous_task.payload,
                    "superseded_at": now.isoformat(),
                    "superseded_by_entity_id": risk_id,
                    "supersession_reason": "RISK_ITEM_REPLACED",
                }
                previous_task.updated_at = now
        risk = RiskItem(
            risk_id=risk_id,
            description=draft.description,
            probability=draft.probability,
            impact_min=draft.impact_min,
            impact_most_likely=draft.impact_most_likely,
            impact_max=draft.impact_max,
            currency=draft.currency,
            observation_ids=draft.observation_ids,
            status=VerificationStatus.IN_REVIEW,
            correlated=draft.correlated,
            correlation_group=draft.correlation_group,
            mitigation_cost_input_id=draft.mitigation_cost_input_id,
        )
        row = RiskItemRow(
            id=risk.risk_id,
            project_id=project.id,
            risk_key=draft.risk_key,
            status=VerificationStatus.IN_REVIEW.value,
            currency=draft.currency,
            expected_impact=None,
            supersedes_risk_id=previous.id if previous is not None else None,
            is_current=True,
            payload={
                "risk": risk.model_dump(mode="json"),
                "risk_key": draft.risk_key,
                "evidence_value": draft.evidence_value(),
                "observation_ids": list(draft.observation_ids),
                "independence_source_ids": list(independence_source_ids),
                "created_by": actor.actor_id,
                "risk_model_version_id": model.id,
                "risk_model_content_hash": model.content_hash,
                "document_set_revision_id": document_set.id,
                "review_role": definition.review_role.value,
            },
            created_at=now,
            updated_at=now,
        )
        task = self._ensure_review_task(
            row=row,
            definition=definition,
            model=model,
            document_set=document_set,
        )
        row.payload = {**row.payload, "approval_task_id": task.id}
        self.session.add(row)
        project_service.record_event(
            aggregate_type="project",
            aggregate_id=project.id,
            event_type="risk_item_submitted",
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload={
                "risk_item_id": row.id,
                "risk_key": row.risk_key,
                "risk_submission_hash": task.payload["risk_submission_hash"],
                "observation_ids": list(draft.observation_ids),
                "independence_source_ids": list(independence_source_ids),
                "risk_model_version_id": model.id,
                "risk_model_content_hash": model.content_hash,
                "document_set_revision_id": document_set.id,
                "approval_task_id": task.id,
                "supersedes_risk_id": row.supersedes_risk_id,
                "superseded_approval_task_id": superseded_task_id,
            },
        )
        return self._view(row)

    def decide_risk(
        self,
        *,
        actor: Actor,
        project_id: str,
        risk_item_id: str,
        command: RiskItemDecisionCommand,
        request_id: str,
        reason: str,
    ) -> RiskItemDecisionResult:
        reason = self._reason(reason)
        project_service = self._project_service()
        project = project_service.get_project(
            actor=actor,
            project_id=project_id,
            lock=True,
            required_roles=(ActorRole.TECHNICAL_EXPERT, ActorRole.REVIEWER),
        )
        self._require_editable_state(project)
        document_set = self._document_set(
            project.id,
            project.current_document_set_revision_id,
        )
        row = self._current_risk(project.id, risk_item_id)
        expected_risk_updated_at = ensure_utc(command.expected_risk_updated_at)
        assert expected_risk_updated_at is not None
        if ensure_utc(row.updated_at) != expected_risk_updated_at:
            raise OptimisticLockError(
                "Risk item changed after it was loaded; reload before deciding"
            )
        model_version_id = row.payload.get("risk_model_version_id")
        if not isinstance(model_version_id, str):
            raise ValueError("Risk model version is missing from the risk item")
        definition, model = self._risk_model(
            project.id,
            actor.organization_id,
            expected_version_id=model_version_id,
        )
        project_service.get_project(
            actor=actor,
            project_id=project.id,
            required_roles=(definition.review_role,),
        )
        task = self._task_for_item(row, lock=True)
        if task is None:
            raise ValueError("Risk review task is missing")
        expected_task_updated_at = ensure_utc(command.expected_task_updated_at)
        assert expected_task_updated_at is not None
        if ensure_utc(task.updated_at) != expected_task_updated_at:
            raise OptimisticLockError(
                "Risk review task changed after it was loaded; reload before deciding"
            )
        blockers = self._review_blockers(
            actor=actor,
            project_state=ApprovalState(project.state),
            row=row,
            task=task,
            definition=definition,
            model=model,
            document_set=document_set,
        )
        if blockers:
            raise ValueError("Risk review is blocked: " + ", ".join(blockers))

        draft = self._draft(row)
        now = utc_now()
        approval_id = f"approval-{uuid4()}"
        if command.decision is ApprovalDecision.APPROVED:
            verified = RiskItem.model_validate(row.payload["risk"]).model_copy(
                update={"status": VerificationStatus.VERIFIED}
            )
            row.status = VerificationStatus.VERIFIED.value
            row.payload = {
                **row.payload,
                "risk": verified.model_dump(mode="json"),
                "verified_by": actor.actor_id,
                "verified_at": now.isoformat(),
                "reviewed_by": actor.actor_id,
                "reviewed_at": now.isoformat(),
                "review_decision": command.decision.value,
            }
        else:
            rejected = RiskItem.model_validate(row.payload["risk"]).model_copy(
                update={"status": VerificationStatus.REJECTED}
            )
            row.status = VerificationStatus.REJECTED.value
            row.payload = {
                **row.payload,
                "risk": rejected.model_dump(mode="json"),
                "reviewed_by": actor.actor_id,
                "reviewed_at": now.isoformat(),
                "review_decision": command.decision.value,
            }
        row.updated_at = now
        task.status = command.decision.value
        task.updated_at = now
        approval_payload = {
            "project_id": project.id,
            "risk_item_id": row.id,
            "risk_key": row.risk_key,
            "expected_risk_updated_at": expected_risk_updated_at.isoformat(),
            "expected_task_updated_at": expected_task_updated_at.isoformat(),
            "evidence_ids": list(draft.observation_ids),
            "independence_source_ids": list(
                row.payload.get("independence_source_ids", [])
            ),
            "risk_model_version_id": model.id,
            "risk_model_content_hash": model.content_hash,
            "document_set_revision_id": document_set.id,
            "risk_submission_hash": task.payload.get("risk_submission_hash"),
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
        project_service.record_event(
            aggregate_type="project",
            aggregate_id=project.id,
            event_type="risk_item_review_decided",
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload={
                "approval_id": approval_id,
                "approval_task_id": task.id,
                "decision": command.decision.value,
                **approval_payload,
            },
        )
        return RiskItemDecisionResult(
            item=self._view(row),
            approval_id=approval_id,
            decision=command.decision,
        )

    def verify_risk(
        self,
        *,
        actor: Actor,
        project_id: str,
        risk_item_id: str,
        expected_risk_updated_at: datetime,
        expected_task_updated_at: datetime,
        request_id: str,
        reason: str,
    ) -> RiskItemView:
        return self.decide_risk(
            actor=actor,
            project_id=project_id,
            risk_item_id=risk_item_id,
            command=RiskItemDecisionCommand(
                decision=ApprovalDecision.APPROVED,
                expected_risk_updated_at=expected_risk_updated_at,
                expected_task_updated_at=expected_task_updated_at,
            ),
            request_id=request_id,
            reason=reason,
        ).item

    def calculate_reserve(
        self,
        *,
        actor: Actor,
        project_id: str,
        expected_document_set_revision_id: str,
        risk_model_version_id: str,
        request_id: str,
        reason: str,
    ) -> RiskCalculationView:
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
            ),
        )
        self._require_editable_state(project)
        document_set = self._document_set(
            project.id,
            project.current_document_set_revision_id,
        )
        if document_set.id != expected_document_set_revision_id:
            raise OptimisticLockError(
                "Current document set changed after risk context was loaded"
            )
        definition, model = self._risk_model(
            project.id,
            actor.organization_id,
            expected_version_id=risk_model_version_id,
        )
        rows = tuple(
            self.session.scalars(
                select(RiskItemRow)
                .where(
                    RiskItemRow.project_id == project.id,
                    RiskItemRow.is_current.is_(True),
                )
                .order_by(RiskItemRow.risk_key)
                .with_for_update()
            )
        )
        blockers = self._calculation_blockers(
            project_id=project.id,
            rows=rows,
            definition=definition,
            model=model,
            document_set=document_set,
        )
        if blockers:
            raise ValueError(
                "Risk reserve calculation is blocked: " + ", ".join(blockers)
            )
        for row in rows:
            self._require_verified_item_integrity(
                row=row,
                definition=definition,
                model=model,
                document_set=document_set,
            )
        risks = tuple(RiskItem.model_validate(row.payload["risk"]) for row in rows)
        policy = definition.calculation_policy(model.id)
        calculation = calculate_risk_reserve(risks, policy)
        independent = self._independent_recalculation(risks, definition, model.id)
        if (
            independent["expected_reserve"]
            != str(calculation.expected_reserve)
            or independent["per_risk_expected_impact"]
            != {
                key: str(value)
                for key, value in calculation.per_risk_expected_impact.items()
            }
        ):
            raise RuntimeError(
                "Independent risk reserve validation differs from the primary engine"
            )
        reserve_reference = self._reserve_cost_component(definition, project.id)
        risk_item_signatures = [
            {
                "risk_item_id": row.id,
                "risk_key": row.risk_key,
                "risk_submission_hash": self._task_payload(
                    row=row,
                    definition=definition,
                    model=model,
                    document_set=document_set,
                )["risk_submission_hash"],
                "updated_at": self._timestamp(row.updated_at).isoformat(),
            }
            for row in rows
        ]
        input_signature = content_hash(
            {
                "risk_items": risk_item_signatures,
                "risk_model_version_id": model.id,
                "risk_model_content_hash": model.content_hash,
                "document_set_revision_id": document_set.id,
                "document_set_manifest_hash": document_set.manifest_hash,
                "reserve_cost_component": reserve_reference,
            }
        )
        output_hash = content_hash(
            {
                "calculation": calculation,
                "independent_validation": independent,
            }
        )
        previous = self.session.scalar(
            select(RiskCalculationRow)
            .where(
                RiskCalculationRow.project_id == project.id,
                RiskCalculationRow.is_current.is_(True),
            )
            .with_for_update()
        )
        if previous is not None:
            previous.is_current = False
        now = utc_now()
        status = "VALIDATED" if calculation.passed else "BLOCKED"
        calculation_id = f"risk-calculation-{uuid4()}"
        calculation_row = RiskCalculationRow(
            id=calculation_id,
            project_id=project.id,
            policy_version_id=model.id,
            status=status,
            expected_reserve=calculation.expected_reserve,
            currency=calculation.currency,
            unit=definition.reserve_unit,
            supersedes_calculation_id=previous.id if previous is not None else None,
            is_current=True,
            payload={
                "calculation": calculation.model_dump(mode="json"),
                "independent_validation": independent,
                "input_signature": input_signature,
                "output_hash": output_hash,
                "risk_item_ids": [item.id for item in rows],
                "risk_item_signatures": risk_item_signatures,
                "risk_model_version_id": model.id,
                "risk_model_content_hash": model.content_hash,
                "document_set_revision_id": document_set.id,
                "document_set_manifest_hash": document_set.manifest_hash,
                "reserve_cost_component": reserve_reference,
                "basis_type": "RISK_RESERVE",
                "unit_rate": str(calculation.expected_reserve),
                "currency": calculation.currency,
                "unit": definition.reserve_unit,
                "calculated_by": actor.actor_id,
            },
            created_at=now,
        )
        self.session.add(calculation_row)
        self._replace_findings(project.id, calculation.findings, calculation_id)
        project_service.record_event(
            aggregate_type="project",
            aggregate_id=project.id,
            event_type="risk_reserve_calculated",
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload={
                "risk_calculation_id": calculation_id,
                "status": status,
                "expected_reserve": calculation.expected_reserve,
                "currency": calculation.currency,
                "input_signature": input_signature,
                "output_hash": output_hash,
                "risk_item_ids": [item.id for item in rows],
                "risk_model_version_id": model.id,
                "risk_model_content_hash": model.content_hash,
                "document_set_revision_id": document_set.id,
                "independent_validation_passed": independent["passed"],
            },
        )
        return self._calculation_view(calculation_row)

    def _risk_model(
        self,
        project_id: str,
        organization_id: str,
        *,
        expected_version_id: str | None = None,
    ) -> tuple[RiskModelDefinition, ControlledVersionRow]:
        row = require_bound_controlled_version(
            session=self.session,
            settings=self.settings,
            project_id=project_id,
            organization_id=organization_id,
            purpose="risk_model",
            kind="risk_model",
            expected_version_id=expected_version_id,
        )
        return (
            RiskModelDefinition.model_validate(
                {
                    key: value
                    for key, value in row.payload.items()
                    if key != "_governance"
                }
            ),
            row,
        )

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

    def _reserve_cost_component(
        self,
        definition: RiskModelDefinition,
        project_id: str,
    ) -> dict[str, str]:
        reference = definition.reserve_cost_component
        line = self.session.scalar(
            select(BoqLineRow).where(
                BoqLineRow.id == reference.line_id,
                BoqLineRow.project_id == project_id,
                BoqLineRow.is_current.is_(True),
                BoqLineRow.status == VerificationStatus.VERIFIED.value,
            )
        )
        if line is None or line.unit != definition.reserve_unit:
            raise ValueError("Risk reserve BoQ line or unit does not match the risk model")
        components = line.payload.get("cost_components")
        component = (
            next(
                (
                    item
                    for item in components
                    if isinstance(item, dict)
                    and item.get("semantic_key") == reference.semantic_key
                ),
                None,
            )
            if isinstance(components, list)
            else None
        )
        if (
            not isinstance(component, dict)
            or component.get("category") != CostCategory.RISK.value
            or component.get("basis_kind") != CostBasisKind.RISK_MODEL.value
        ):
            raise ValueError(
                "Risk model must reference a planned RISK/RISK_MODEL component"
            )
        return {
            "line_id": reference.line_id,
            "semantic_key": reference.semantic_key,
        }

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
            raise ValueError("One or more risk evidence observations are missing")
        ordered = tuple(by_id[item] for item in observation_ids)
        if any(row.document_revision_id not in document_revision_ids for row in ordered):
            raise ValueError("Risk evidence must belong to the confirmed document set")
        return ordered

    @classmethod
    def _validate_observation_values(
        cls,
        draft: RiskItemDraft,
        observations: tuple[ObservationRow, ...],
        *,
        expected_field_name: str,
        require_eligible_status: bool = True,
    ) -> None:
        expected_hash = content_hash(draft.evidence_value())
        for row in observations:
            observation = cls._observation(row)
            if require_eligible_status and observation.status not in {
                VerificationStatus.UNVERIFIED,
                VerificationStatus.VERIFIED,
            }:
                raise ValueError("Risk evidence status is not eligible")
            if (
                require_eligible_status
                and observation.method is EvidenceMethod.MANUAL
                and observation.status is not VerificationStatus.VERIFIED
            ):
                raise ValueError("Manual risk evidence requires dedicated review")
            if observation.field_name != expected_field_name:
                raise ValueError("Risk evidence belongs to another governed field")
            if content_hash(observation.value) != expected_hash:
                raise ValueError("Risk evidence does not reproduce the risk item")
            if observation.unit is not None:
                raise ValueError("Structured risk evidence must not declare a scalar unit")

    @staticmethod
    def _observation(row: ObservationRow) -> Observation:
        observation = Observation.model_validate(row.payload.get("observation"))
        if (
            row.id != observation.observation_id
            or row.document_revision_id
            != observation.location.document_revision_id
            or row.field_name != observation.field_name
            or row.method != observation.method.value
            or row.method_version != observation.method_version
            or row.status != observation.status.value
        ):
            raise ValueError("Risk evidence row does not reproduce its payload")
        return observation

    def _candidate_view(
        self,
        *,
        row: ObservationRow,
        risk_key: str,
        definition: RiskModelDefinition,
        organization_id: str,
        document_revision_ids: frozenset[str],
        independent: bool,
    ) -> RiskEvidenceCandidateView:
        blockers: list[str] = []
        try:
            observation = self._observation(row)
        except (TypeError, ValueError):
            observation = Observation.model_validate(row.payload.get("observation"))
            blockers.append("EVIDENCE_INTEGRITY_FAILED")
        draft: RiskItemDraft | None = None
        try:
            raw_value = observation.value
            if not isinstance(raw_value, dict):
                raise ValueError("Risk evidence value is not structured")
            draft = RiskItemDraft.model_validate(
                {**raw_value, "observation_ids": [observation.observation_id]}
            )
            if draft.risk_key != risk_key:
                blockers.append("RISK_KEY_MISMATCH")
            if draft.correlated:
                blockers.append("CORRELATION_ENGINE_NOT_QUALIFIED")
        except (TypeError, ValueError):
            blockers.append("RISK_VALUE_INVALID")
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
        if observation.field_name != definition.evidence_field_names[risk_key]:
            blockers.append("EVIDENCE_FIELD_MISMATCH")
        qualification_id = row.payload.get("adapter_qualification_id")
        qualification = (
            self.session.get(AdapterQualificationRow, qualification_id)
            if isinstance(qualification_id, str)
            else None
        )
        domain = (
            qualification.payload.get("independence_domain")
            if qualification is not None
            else None
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
                or qualification.payload.get("service_actor_id")
                != observation.actor_id
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
        return RiskEvidenceCandidateView(
            observation=observation,
            draft=draft,
            adapter_qualification_id=(
                str(qualification_id) if isinstance(qualification_id, str) else None
            ),
            adapter_status=qualification.status if qualification is not None else None,
            adapter_valid_until=(
                qualification.valid_until if qualification is not None else None
            ),
            independence_domain=domain if isinstance(domain, str) else None,
            eligible=not blockers,
            blockers=tuple(dict.fromkeys(blockers)),
        )

    def _review_view(
        self,
        *,
        actor: Actor,
        project_state: ApprovalState,
        row: RiskItemRow,
        definition: RiskModelDefinition,
        model: ControlledVersionRow,
        document_set: DocumentSetRevisionRow,
    ) -> RiskItemReviewView:
        task = self._task_for_item(row)
        if task is None:
            return RiskItemReviewView(
                item=self._view(row),
                task_status="MISSING",
                task_updated_at=self._timestamp(row.updated_at),
                assigned_role=definition.review_role,
                decision_allowed=False,
                decision_blockers=("TASK_MISSING",),
            )
        blockers = self._review_blockers(
            actor=actor,
            project_state=project_state,
            row=row,
            task=task,
            definition=definition,
            model=model,
            document_set=document_set,
        )
        return RiskItemReviewView(
            item=self._view(row),
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
        row: RiskItemRow,
        task: ApprovalTaskRow,
        definition: RiskModelDefinition,
        model: ControlledVersionRow,
        document_set: DocumentSetRevisionRow,
    ) -> list[str]:
        blockers: list[str] = []
        if project_state not in _EDITABLE_STATES:
            blockers.append("PROJECT_STATE_NOT_ALLOWED")
        if not row.is_current:
            blockers.append("RISK_SUPERSEDED")
        if row.status != VerificationStatus.IN_REVIEW.value:
            blockers.append("RISK_NOT_IN_REVIEW")
        if row.payload.get("created_by") == actor.actor_id:
            blockers.append("FOUR_EYES_RISK_AUTHOR")
        if definition.review_role not in actor.roles:
            blockers.append("REVIEW_ROLE_REQUIRED")
        if row.payload.get("risk_model_version_id") != model.id:
            blockers.append("RISK_MODEL_VERSION_CHANGED")
        if row.payload.get("risk_model_content_hash") != model.content_hash:
            blockers.append("RISK_MODEL_HASH_MISMATCH")
        if row.payload.get("document_set_revision_id") != document_set.id:
            blockers.append("DOCUMENT_SET_CHANGED")
        expected_task_payload = self._task_payload(
            row=row,
            definition=definition,
            model=model,
            document_set=document_set,
        )
        if (
            task.project_id != row.project_id
            or task.task_type != "RISK_ITEM_REVIEW"
            or task.entity_type != "risk_item"
            or task.entity_id != row.id
            or task.assigned_role != definition.review_role.value
            or not task.required
            or task.payload != expected_task_payload
        ):
            blockers.append("TASK_INTEGRITY_FAILED")
        if task.status != "PENDING":
            blockers.append("TASK_NOT_PENDING")
        if task.payload.get("created_by") == actor.actor_id:
            blockers.append("FOUR_EYES_TASK_CREATOR")
        field_name = definition.evidence_field_names.get(row.risk_key)
        if field_name is None:
            blockers.append("RISK_KEY_NOT_DECLARED")
        elif self._unresolved_conflicts(row.project_id, field_name):
            blockers.append("UNRESOLVED_EVIDENCE_CONFLICT")
        try:
            draft = self._draft(row)
            observations = self._observations(
                row.project_id,
                draft.observation_ids,
                document_revision_ids=frozenset(document_set.revision_ids),
            )
            self._validate_observation_values(
                draft,
                observations,
                expected_field_name=str(field_name),
            )
            leaves = resolve_observation_leaves(
                self.session,
                project_id=row.project_id,
                observations=observations,
            )
            self._validate_observation_values(
                draft,
                leaves,
                expected_field_name=str(field_name),
                require_eligible_status=False,
            )
            leaf_ids = tuple(item.id for item in leaves)
            if tuple(row.payload.get("independence_source_ids", [])) != leaf_ids:
                blockers.append("INDEPENDENCE_EVIDENCE_CHANGED")
            if row.risk_key in definition.independently_verified_risk_keys:
                qualified = require_distinct_qualified_independence(
                    self.session,
                    project_id=row.project_id,
                    observations=observations,
                )
                if qualified != leaf_ids:
                    blockers.append("INDEPENDENCE_EVIDENCE_CHANGED")
                self._validate_observation_values(
                    draft,
                    leaves,
                    expected_field_name=str(field_name),
                )
        except (KeyError, LookupError, TypeError, ValueError):
            blockers.append("EVIDENCE_INTEGRITY_FAILED")
        return list(dict.fromkeys(blockers))

    def _calculation_blockers(
        self,
        *,
        project_id: str,
        rows: tuple[RiskItemRow, ...],
        definition: RiskModelDefinition,
        model: ControlledVersionRow,
        document_set: DocumentSetRevisionRow,
    ) -> tuple[str, ...]:
        by_key = {row.risk_key: row for row in rows}
        blockers: list[str] = []
        for key in definition.required_risk_keys:
            row = by_key.get(key)
            if row is None:
                blockers.append(f"RISK_REQUIRED_MISSING:{key}")
            elif row.status != VerificationStatus.VERIFIED.value:
                blockers.append(f"RISK_REQUIRED_UNVERIFIED:{key}")
        if len(rows) < definition.minimum_risk_items:
            blockers.append("RISK_REGISTER_BELOW_APPROVED_MINIMUM")
        if len(by_key) != len(rows):
            blockers.append("RISK_KEY_AMBIGUOUS")
        if any(row.risk_key not in definition.risk_keys for row in rows):
            blockers.append("RISK_KEY_NOT_DECLARED")
        if any(
            row.payload.get("risk_model_version_id") != model.id
            or row.payload.get("risk_model_content_hash") != model.content_hash
            or row.payload.get("document_set_revision_id") != document_set.id
            for row in rows
        ):
            blockers.append("RISK_CONTEXT_STALE")
        try:
            self._reserve_cost_component(definition, project_id)
        except (LookupError, TypeError, ValueError):
            blockers.append("RISK_RESERVE_COMPONENT_INVALID")
        return tuple(dict.fromkeys(blockers))

    def _require_verified_item_integrity(
        self,
        *,
        row: RiskItemRow,
        definition: RiskModelDefinition,
        model: ControlledVersionRow,
        document_set: DocumentSetRevisionRow,
    ) -> None:
        created_by = row.payload.get("created_by")
        verified_by = row.payload.get("verified_by")
        task_id = row.payload.get("approval_task_id")
        if (
            row.status != VerificationStatus.VERIFIED.value
            or not row.is_current
            or not isinstance(created_by, str)
            or not created_by
            or not isinstance(verified_by, str)
            or not verified_by
            or created_by == verified_by
            or row.payload.get("reviewed_by") != verified_by
            or row.payload.get("review_decision") != "APPROVED"
            or row.payload.get("risk_model_version_id") != model.id
            or row.payload.get("risk_model_content_hash") != model.content_hash
            or row.payload.get("document_set_revision_id") != document_set.id
            or not isinstance(task_id, str)
            or not task_id
        ):
            raise ValueError("Verified risk item provenance is invalid")
        draft = self._draft(row)
        field_name = definition.evidence_field_names[row.risk_key]
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
        leaves = resolve_observation_leaves(
            self.session,
            project_id=row.project_id,
            observations=observations,
        )
        self._validate_observation_values(
            draft,
            leaves,
            expected_field_name=field_name,
            require_eligible_status=False,
        )
        leaf_ids = tuple(item.id for item in leaves)
        if tuple(row.payload.get("independence_source_ids", [])) != leaf_ids:
            raise ValueError("Risk evidence leaves changed")
        if (
            row.risk_key in definition.independently_verified_risk_keys
            and require_distinct_qualified_independence(
                self.session,
                project_id=row.project_id,
                observations=observations,
            )
            != leaf_ids
        ):
            raise ValueError("Risk independent evidence changed")
        task = self.session.get(ApprovalTaskRow, task_id)
        expected_task_payload = self._task_payload(
            row=row,
            definition=definition,
            model=model,
            document_set=document_set,
        )
        if (
            task is None
            or task.project_id != row.project_id
            or task.task_type != "RISK_ITEM_REVIEW"
            or task.entity_type != "risk_item"
            or task.entity_id != row.id
            or task.assigned_role != definition.review_role.value
            or not task.required
            or task.status != "APPROVED"
            or task.payload != expected_task_payload
        ):
            raise ValueError("Risk approval task integrity failed")
        approval = self.session.scalar(
            select(ApprovalRecordRow).where(ApprovalRecordRow.task_id == task.id)
        )
        if (
            approval is None
            or approval.decision != "APPROVED"
            or approval.decided_by != verified_by
            or approval.payload.get("risk_item_id") != row.id
            or approval.payload.get("risk_key") != row.risk_key
            or approval.payload.get("evidence_ids")
            != list(draft.observation_ids)
            or approval.payload.get("independence_source_ids") != list(leaf_ids)
            or approval.payload.get("risk_model_version_id") != model.id
            or approval.payload.get("risk_model_content_hash") != model.content_hash
            or approval.payload.get("document_set_revision_id") != document_set.id
            or approval.payload.get("risk_submission_hash")
            != expected_task_payload["risk_submission_hash"]
        ):
            raise ValueError("Risk approval record integrity failed")
        event = next(
            (
                candidate
                for candidate in self.session.scalars(
                    select(AuditEventRow)
                    .where(
                        AuditEventRow.aggregate_type == "project",
                        AuditEventRow.aggregate_id == row.project_id,
                        AuditEventRow.event_type == "risk_item_review_decided",
                    )
                    .order_by(AuditEventRow.sequence.desc())
                )
                if candidate.payload.get("risk_item_id") == row.id
            ),
            None,
        )
        if (
            event is None
            or event.actor_id != verified_by
            or event.payload.get("approval_id") != approval.id
            or event.payload.get("approval_task_id") != task.id
            or event.payload.get("decision") != "APPROVED"
            or event.payload.get("risk_key") != row.risk_key
            or event.payload.get("evidence_ids") != list(draft.observation_ids)
            or event.payload.get("risk_submission_hash")
            != expected_task_payload["risk_submission_hash"]
        ):
            raise ValueError("Risk approval audit event integrity failed")

    def _ensure_review_task(
        self,
        *,
        row: RiskItemRow,
        definition: RiskModelDefinition,
        model: ControlledVersionRow,
        document_set: DocumentSetRevisionRow,
    ) -> ApprovalTaskRow:
        task_id = (
            "approval-task-risk-"
            + content_hash(
                {
                    "risk_item_id": row.id,
                    "risk_model_version_id": model.id,
                }
            )[:24]
        )
        if self.session.get(ApprovalTaskRow, task_id) is not None:
            raise RuntimeError("Risk review task identifier collision")
        task = ApprovalTaskRow(
            id=task_id,
            project_id=row.project_id,
            task_type="RISK_ITEM_REVIEW",
            entity_type="risk_item",
            entity_id=row.id,
            assigned_role=definition.review_role.value,
            status="PENDING",
            required=True,
            payload=self._task_payload(
                row=row,
                definition=definition,
                model=model,
                document_set=document_set,
            ),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        self.session.add(task)
        return task

    @staticmethod
    def _task_payload(
        *,
        row: RiskItemRow,
        definition: RiskModelDefinition,
        model: ControlledVersionRow,
        document_set: DocumentSetRevisionRow,
    ) -> dict[str, Any]:
        submission = {
            "risk_item_id": row.id,
            "risk_key": row.risk_key,
            "evidence_value": row.payload.get("evidence_value"),
            "observation_ids": row.payload.get("observation_ids", []),
            "independence_source_ids": row.payload.get(
                "independence_source_ids",
                [],
            ),
            "created_by": row.payload.get("created_by"),
            "risk_model_version_id": model.id,
            "risk_model_content_hash": model.content_hash,
            "document_set_revision_id": document_set.id,
            "review_role": definition.review_role.value,
        }
        return {
            "created_by": row.payload.get("created_by"),
            "risk_item_id": row.id,
            "risk_key": row.risk_key,
            "risk_submission_hash": content_hash(submission),
            "observation_ids": list(row.payload.get("observation_ids", [])),
            "independence_source_ids": list(
                row.payload.get("independence_source_ids", [])
            ),
            "risk_model_version_id": model.id,
            "risk_model_content_hash": model.content_hash,
            "document_set_revision_id": document_set.id,
            "review_role": definition.review_role.value,
        }

    def _task_for_item(
        self,
        row: RiskItemRow,
        *,
        lock: bool = False,
    ) -> ApprovalTaskRow | None:
        task_id = row.payload.get("approval_task_id")
        if not isinstance(task_id, str) or not task_id:
            return None
        statement = select(ApprovalTaskRow).where(
            ApprovalTaskRow.id == task_id,
            ApprovalTaskRow.project_id == row.project_id,
            ApprovalTaskRow.entity_type == "risk_item",
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

    def _current_risk(self, project_id: str, risk_item_id: str) -> RiskItemRow:
        row = self.session.scalar(
            select(RiskItemRow)
            .where(
                RiskItemRow.id == risk_item_id,
                RiskItemRow.project_id == project_id,
                RiskItemRow.is_current.is_(True),
            )
            .with_for_update()
        )
        if row is None:
            raise LookupError(risk_item_id)
        return row

    @staticmethod
    def _draft(row: RiskItemRow) -> RiskItemDraft:
        return RiskItemDraft.model_validate(
            {
                **row.payload.get("evidence_value", {}),
                "risk_key": row.risk_key,
                "observation_ids": row.payload.get("observation_ids", []),
            }
        )

    @staticmethod
    def _independent_recalculation(
        risks: tuple[RiskItem, ...],
        definition: RiskModelDefinition,
        policy_version: str,
    ) -> dict[str, Any]:
        policy = definition.calculation_policy(policy_version)
        rounding = {
            "ROUND_HALF_UP": ROUND_HALF_UP,
            "ROUND_HALF_EVEN": ROUND_HALF_EVEN,
        }[policy.rounding_mode]
        quantum = Decimal(1).scaleb(-policy.rounding_scale)
        expected: dict[str, Decimal] = {}
        for risk in risks:
            if (
                risk.status is not VerificationStatus.VERIFIED
                or risk.currency != policy.currency
                or risk.correlated
            ):
                continue
            three_point_sum = (
                risk.impact_min
                + risk.impact_most_likely
                + risk.impact_max
            )
            expected[risk.risk_id] = (
                risk.probability * three_point_sum / Decimal(3)
            ).quantize(quantum, rounding=rounding)
        reserve = sum(expected.values(), start=Decimal(0)).quantize(
            quantum,
            rounding=rounding,
        )
        return {
            "validator_version": f"independent:{policy_version}",
            "expected_reserve": str(reserve),
            "currency": policy.currency,
            "per_risk_expected_impact": {
                key: str(expected[key]) for key in sorted(expected)
            },
            "passed": len(expected) == len(risks),
        }

    def _replace_findings(
        self,
        project_id: str,
        findings: tuple[ValidationFinding, ...],
        calculation_id: str,
    ) -> None:
        now = utc_now()
        for old_finding in self.session.scalars(
            select(VerificationFindingRow).where(
                VerificationFindingRow.project_id == project_id,
                VerificationFindingRow.contour == "RISK",
                VerificationFindingRow.resolved.is_(False),
            )
        ):
            old_finding.resolved = True
            old_finding.updated_at = now
            old_finding.payload = {
                **old_finding.payload,
                "resolved_by_risk_calculation_id": calculation_id,
                "resolved_at": now.isoformat(),
            }
        for finding in findings:
            identity = {
                "project_id": project_id,
                "contour": "RISK",
                "finding": finding,
            }
            finding_id = f"finding-{content_hash(identity)[:24]}"
            payload = {
                **finding.model_dump(mode="json"),
                "risk_calculation_id": calculation_id,
            }
            existing = self.session.get(VerificationFindingRow, finding_id)
            if existing is None:
                self.session.add(
                    VerificationFindingRow(
                        id=finding_id,
                        project_id=project_id,
                        contour="RISK",
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

    @staticmethod
    def _calculation_view(row: RiskCalculationRow) -> RiskCalculationView:
        calculation = RiskCalculation.model_validate(row.payload.get("calculation"))
        independent = row.payload.get("independent_validation")
        input_signature = row.payload.get("input_signature")
        output_hash = row.payload.get("output_hash")
        risk_item_ids = row.payload.get("risk_item_ids")
        document_set_revision_id = row.payload.get("document_set_revision_id")
        if (
            not isinstance(independent, dict)
            or independent.get("passed") is not True
            or not isinstance(input_signature, str)
            or len(input_signature) != 64
            or not isinstance(output_hash, str)
            or len(output_hash) != 64
            or not isinstance(risk_item_ids, list)
            or not risk_item_ids
            or len(risk_item_ids) != len(set(risk_item_ids))
            or not isinstance(document_set_revision_id, str)
            or not document_set_revision_id
        ):
            raise ValueError("Risk calculation provenance is incomplete")
        if content_hash(
            {
                "calculation": calculation,
                "independent_validation": independent,
            }
        ) != output_hash:
            raise ValueError("Risk calculation output hash differs")
        return RiskCalculationView(
            calculation_id=row.id,
            calculation=calculation,
            status=row.status,
            input_signature=input_signature,
            output_hash=output_hash,
            independent_validation_passed=True,
            risk_item_ids=tuple(str(item) for item in risk_item_ids),
            document_set_revision_id=document_set_revision_id,
            supersedes_calculation_id=row.supersedes_calculation_id,
            created_at=RiskService._timestamp(row.created_at),
        )

    @staticmethod
    def _require_editable_state(project: ProjectRow) -> None:
        if ApprovalState(project.state) not in _EDITABLE_STATES:
            raise ValueError("Risk register must be fixed before calculation")

    @staticmethod
    def _reason(reason: str) -> str:
        normalized = reason.strip()
        if not normalized or len(normalized) > 2000:
            raise ValueError("Risk workflow reason must contain 1 to 2000 characters")
        return normalized

    @staticmethod
    def _timestamp(value: datetime) -> datetime:
        normalized = ensure_utc(value)
        if normalized is None:
            raise ValueError("Risk workflow timestamp is missing")
        return normalized

    def _project_service(self) -> ProjectService:
        return ProjectService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        )

    @staticmethod
    def _view(row: RiskItemRow) -> RiskItemView:
        created_by = row.payload.get("created_by")
        model_id = row.payload.get("risk_model_version_id")
        model_hash = row.payload.get("risk_model_content_hash")
        document_set_id = row.payload.get("document_set_revision_id")
        task_id = row.payload.get("approval_task_id")
        if not all(
            isinstance(item, str) and item
            for item in (
                created_by,
                model_id,
                model_hash,
                document_set_id,
                task_id,
            )
        ):
            raise ValueError("Risk item provenance is incomplete")
        return RiskItemView(
            row_id=row.id,
            risk_key=row.risk_key,
            risk=RiskItem.model_validate(row.payload.get("risk")),
            independence_source_ids=tuple(
                row.payload.get("independence_source_ids", [])
            ),
            supersedes_risk_id=row.supersedes_risk_id,
            is_current=row.is_current,
            created_by=str(created_by),
            verified_by=row.payload.get("verified_by"),
            risk_model_version_id=str(model_id),
            risk_model_content_hash=str(model_hash),
            document_set_revision_id=str(document_set_id),
            approval_task_id=str(task_id),
            updated_at=RiskService._timestamp(row.updated_at),
        )
