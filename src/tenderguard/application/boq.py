from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
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
    require_observation_in_document_set,
)
from tenderguard.application.evidence import EvidenceService, ObservationDraft
from tenderguard.application.evidence_independence import (
    require_distinct_qualified_independence,
)
from tenderguard.application.projects import OptimisticLockError, ProjectService
from tenderguard.application.stage_gates import scope_input_signature
from tenderguard.config import Settings
from tenderguard.domain.boq_spreadsheet import BoqXlsxProfile, ImportedBoqRowValue
from tenderguard.domain.common import content_hash, ensure_utc, utc_now
from tenderguard.domain.enums import (
    ActorRole,
    ApprovalState,
    CostBasisKind,
    CostCategory,
    EvidenceMethod,
    FindingCode,
    Severity,
    VerificationStatus,
)
from tenderguard.domain.models import (
    DomainModel,
    Observation,
    QuantityRecord,
    ValidationFinding,
)
from tenderguard.domain.quantities import (
    QuantityFormulaDefinition,
    QuantityValidationPolicy,
    QuantityValidationResult,
    validate_quantity,
)
from tenderguard.domain.scope import ScopeEvaluation, ScopeRule, evaluate_scope
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.object_store import ObjectStore
from tenderguard.infrastructure.orm import (
    AdapterQualificationRow,
    ApprovalRecordRow,
    ApprovalTaskRow,
    BoqLineRow,
    ControlledVersionRow,
    ExtractionRunRow,
    ManualChangeRow,
    ObservationRow,
    ProjectControlledVersionRow,
    ProjectPassportFactRow,
    ProjectRow,
    QuantityManualChangeApplicationRow,
    QuantityRow,
    ScopeEvaluationRow,
    ScopeFindingRow,
    VerificationFindingRow,
)


class CostComponentDraft(DomainModel):
    semantic_key: str = Field(min_length=1, max_length=128)
    category: CostCategory
    basis_kind: CostBasisKind
    sign: Literal[-1, 1] = 1
    factor_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def controlled_factor_ids_are_valid(self) -> CostComponentDraft:
        if self.semantic_key != self.semantic_key.strip():
            raise ValueError("Cost component semantic key must be normalized")
        if len(self.factor_ids) != len(set(self.factor_ids)):
            raise ValueError("Cost component factor IDs must be unique")
        if any(
            not factor_id or factor_id != factor_id.strip() or len(factor_id) > 200
            for factor_id in self.factor_ids
        ):
            raise ValueError("Cost component factor ID is invalid")
        return self


class BoqLineDraft(DomainModel):
    line_key: str = Field(min_length=1, max_length=128)
    wbs_node_id: str = Field(min_length=1, max_length=128)
    work_code: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=2000)
    unit: str = Field(min_length=1, max_length=100)
    evidence_observation_ids: tuple[str, ...] = Field(min_length=1)
    cost_components: tuple[CostComponentDraft, ...] = Field(min_length=1)
    critical_quantity: bool = False

    @model_validator(mode="after")
    def component_keys_are_unique(self) -> BoqLineDraft:
        keys = [item.semantic_key for item in self.cost_components]
        if len(keys) != len(set(keys)):
            raise ValueError("BoQ line cost component semantic keys must be unique")
        if len(self.evidence_observation_ids) != len(set(self.evidence_observation_ids)):
            raise ValueError("BoQ line evidence observation IDs must be unique")
        bounded_values = (
            self.line_key,
            self.wbs_node_id,
            self.work_code,
            self.description,
            self.unit,
        )
        if any(value != value.strip() for value in bounded_values):
            raise ValueError("BoQ line text fields must be normalized")
        return self


class BoqLineView(DomainModel):
    line_id: str
    line_key: str
    wbs_node_id: str
    work_code: str
    description: str
    unit: str
    status: VerificationStatus
    critical_quantity: bool
    cost_components: tuple[CostComponentDraft, ...]
    supersedes_line_id: str | None
    is_current: bool
    updated_at: datetime


class BoqEvidenceCandidateView(DomainModel):
    observation: Observation
    work_code: str
    unit: str
    description: str | None = None


class BoqAuthoringContextView(DomainModel):
    project_id: str
    project_state: ApprovalState
    document_set_revision_id: str
    evidence_field_name: str
    evidence_candidates: tuple[BoqEvidenceCandidateView, ...]
    candidates_truncated: bool


class BoqSpreadsheetMappingCommand(DomainModel):
    work_code: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=2000)
    unit: str = Field(min_length=1, max_length=100)
    expected_source_observation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposed_at: datetime

    @field_validator("proposed_at")
    @classmethod
    def proposal_timestamp_is_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("BoQ spreadsheet mapping timestamp must include a timezone")
        return value

    @model_validator(mode="after")
    def text_is_normalized(self) -> BoqSpreadsheetMappingCommand:
        if any(value != value.strip() for value in (self.work_code, self.description, self.unit)):
            raise ValueError("BoQ spreadsheet mapping text must be normalized")
        return self


class BoqSpreadsheetQuantityCommand(DomainModel):
    expected_source_observation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposed_at: datetime

    @field_validator("proposed_at")
    @classmethod
    def proposal_timestamp_is_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("BoQ spreadsheet quantity timestamp must include a timezone")
        return value


class BoqSpreadsheetCandidateView(DomainModel):
    source_observation: Observation
    source_observation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_item_id: str
    source_position_id: str
    description: str
    specification: str | None
    source_reference: str | None
    unit: str
    quantity: Decimal
    worksheet_name: str
    row_number: int
    proposal_observation_id: str | None = None
    proposal_task_status: str | None = None
    verified_observation_id: str | None = None
    proposal_allowed: bool
    proposal_blockers: tuple[str, ...] = ()
    quantity_proposal_observation_id: str | None = None
    quantity_proposal_task_status: str | None = None
    verified_quantity_observation_id: str | None = None
    quantity_proposal_allowed: bool
    quantity_proposal_blockers: tuple[str, ...] = ()


class BoqSpreadsheetCandidateContextView(DomainModel):
    project_id: str
    project_state: ApprovalState
    document_set_revision_id: str
    candidates: tuple[BoqSpreadsheetCandidateView, ...]
    candidates_truncated: bool


BOQ_LINE_MAPPING_SCHEMA = "boq-line-mapping/v1"


class BoqLineReviewView(DomainModel):
    line: BoqLineView
    created_by: str
    document_set_revision_id: str
    evidence_observations: tuple[Observation, ...]
    verification_allowed: bool
    verification_blockers: tuple[str, ...]


class InitialQuantityEvidenceCandidateView(DomainModel):
    observation: Observation
    observation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_item_id: str
    value: Decimal
    unit: str


class InitialQuantityContextView(DomainModel):
    project_id: str
    project_state: ApprovalState
    document_set_revision_id: str
    line: BoqLineView
    source_item_id: str | None
    quantity_policy_version_id: str | None
    evidence_candidates: tuple[InitialQuantityEvidenceCandidateView, ...]
    current_quantity_id: str | None = None
    current_quantity_status: VerificationStatus | None = None
    recording_allowed: bool
    recording_blockers: tuple[str, ...] = ()


class AttachImportedQuantityCommand(DomainModel):
    source_observation_id: str = Field(min_length=1, max_length=64)
    expected_source_observation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_line_updated_at: datetime

    @field_validator("expected_line_updated_at")
    @classmethod
    def line_timestamp_is_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Expected BoQ line timestamp must include a timezone")
        return value


class QuantityDraft(DomainModel):
    value: Decimal
    unit: str
    source_observation_ids: tuple[str, ...] = Field(min_length=1)
    source_priority: int = Field(ge=0)
    rounding_scale: int = Field(ge=0, le=12)
    waste_factor: Decimal = Field(ge=0)
    alternative_quantity_ids: tuple[str, ...] = ()
    manual_change_id: str | None = None


class QuantitySubmission(DomainModel):
    draft: QuantityDraft
    formula: QuantityFormulaDefinition | None = None
    formula_input_observation_ids: dict[str, str] = Field(default_factory=dict)


class QuantityExecutionResult(DomainModel):
    quantity: QuantityRecord
    validation: QuantityValidationResult
    supersedes_quantity_id: str | None = None


class ManualChangePolicyRule(DomainModel):
    entity_type: str = Field(min_length=1, max_length=100)
    field_name: str = Field(min_length=1, max_length=200)
    critical: bool
    assigned_role: ActorRole | None = None

    @model_validator(mode="after")
    def criticality_and_review_role_are_consistent(self) -> ManualChangePolicyRule:
        if self.critical and self.assigned_role is None:
            raise ValueError("Critical manual-change rules require an assigned role")
        if not self.critical and self.assigned_role is not None:
            raise ValueError("Non-critical manual-change rules cannot assign an approval role")
        if self.assigned_role is ActorRole.SYSTEM:
            raise ValueError("SYSTEM cannot approve a manual change")
        return self


class ManualChangePolicy(DomainModel):
    rules: tuple[ManualChangePolicyRule, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def rule_targets_are_unique(self) -> ManualChangePolicy:
        targets = [(rule.entity_type, rule.field_name) for rule in self.rules]
        if len(targets) != len(set(targets)):
            raise ValueError("Manual-change policy contains duplicate target rules")
        return self


class QuantityManualChangeView(DomainModel):
    change_id: str
    project_id: str
    line_id: str
    previous_quantity_id: str
    critical: bool
    changed_by: str
    reason: str
    changed_at: datetime
    policy_version_id: str
    document_set_revision_id: str
    before: dict[str, Any]
    after: dict[str, Any]
    approval_task_id: str | None = None
    approval_task_status: str | None = None
    approval_task_updated_at: datetime | None = None
    status: str
    applied_quantity_id: str | None = None
    applied_by: str | None = None
    applied_at: datetime | None = None


class QuantityChangeContextView(DomainModel):
    project_id: str
    line_id: str
    line_key: str
    description: str
    unit: str
    current_quantity_id: str
    current_quantity_status: VerificationStatus
    current_submission: QuantitySubmission
    document_set_revision_id: str
    quantity_policy_version_id: str
    quantity_formula_rules_version_id: str | None = None
    manual_change_policy_version_id: str
    critical: bool
    approval_role: ActorRole | None = None


class ScopeRunResult(DomainModel):
    evaluation: ScopeEvaluation | None = None
    validation_findings: tuple[ValidationFinding, ...] = ()


class BoqService:
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

    def authoring_context(
        self,
        *,
        actor: Actor,
        project_id: str,
        evidence_field_name: str,
        limit: int,
    ) -> BoqAuthoringContextView:
        if limit < 1 or limit > 100:
            raise ValueError("BoQ authoring context limit must be between 1 and 100")
        evidence_field_name = evidence_field_name.strip()
        if not evidence_field_name or len(evidence_field_name) > 300:
            raise ValueError("BoQ evidence field name must contain 1 to 300 characters")
        project = self._project_service().get_project(
            actor=actor,
            project_id=project_id,
            required_roles=(
                ActorRole.ESTIMATOR,
                ActorRole.TECHNICAL_EXPERT,
                ActorRole.REVIEWER,
            ),
        )
        if ApprovalState(project.state) is not ApprovalState.BOQ_IN_PROGRESS:
            raise ValueError("BoQ authoring requires BOQ_IN_PROGRESS")
        document_set = require_confirmed_document_set_integrity(
            session=self.session,
            settings=self.settings,
            project_id=project_id,
            document_set_revision_id=project.current_document_set_revision_id,
        )
        rows = list(
            self.session.scalars(
                select(ObservationRow)
                .where(
                    ObservationRow.project_id == project_id,
                    ObservationRow.document_revision_id.in_(tuple(document_set.revision_ids)),
                    ObservationRow.field_name == evidence_field_name,
                    ObservationRow.status == VerificationStatus.VERIFIED.value,
                )
                .order_by(ObservationRow.created_at.desc(), ObservationRow.id)
                .limit(limit + 1)
            )
        )
        candidates: list[BoqEvidenceCandidateView] = []
        for row in rows[:limit]:
            observation = self._validated_observation(row)
            value = observation.value
            if (
                isinstance(value, dict)
                and isinstance(value.get("work_code"), str)
                and value["work_code"]
                and isinstance(value.get("unit"), str)
                and value["unit"]
            ):
                candidates.append(
                    BoqEvidenceCandidateView(
                        observation=observation,
                        work_code=value["work_code"],
                        unit=value["unit"],
                        description=(
                            value.get("description")
                            if isinstance(value.get("description"), str)
                            else None
                        ),
                    )
                )
        return BoqAuthoringContextView(
            project_id=project_id,
            project_state=ApprovalState(project.state),
            document_set_revision_id=document_set.id,
            evidence_field_name=evidence_field_name,
            evidence_candidates=tuple(candidates),
            candidates_truncated=len(rows) > limit,
        )

    def spreadsheet_candidate_context(
        self,
        *,
        actor: Actor,
        project_id: str,
        limit: int,
    ) -> BoqSpreadsheetCandidateContextView:
        if limit < 1 or limit > 100:
            raise ValueError("BoQ spreadsheet candidate limit must be between 1 and 100")
        project = self._project_service().get_project(
            actor=actor,
            project_id=project_id,
            required_roles=(
                ActorRole.ESTIMATOR,
                ActorRole.TECHNICAL_EXPERT,
                ActorRole.REVIEWER,
            ),
        )
        if ApprovalState(project.state) is not ApprovalState.BOQ_IN_PROGRESS:
            raise ValueError("BoQ spreadsheet review requires BOQ_IN_PROGRESS")
        document_set = require_confirmed_document_set_integrity(
            session=self.session,
            settings=self.settings,
            project_id=project_id,
            document_set_revision_id=project.current_document_set_revision_id,
        )
        rows = list(
            self.session.scalars(
                select(ObservationRow)
                .where(
                    ObservationRow.project_id == project_id,
                    ObservationRow.document_revision_id.in_(tuple(document_set.revision_ids)),
                    ObservationRow.field_name.like("boq_row_candidate:%"),
                    ObservationRow.method == EvidenceMethod.TABLE_PARSER.value,
                    ObservationRow.status == VerificationStatus.UNVERIFIED.value,
                )
                .order_by(ObservationRow.created_at, ObservationRow.id)
                .limit(limit + 1)
            )
        )
        base_blockers: list[str] = []
        if not actor.roles.intersection({ActorRole.TECHNICAL_EXPERT, ActorRole.REVIEWER}):
            base_blockers.append("MAPPING_ROLE_REQUIRED")
        else:
            try:
                manual_context = EvidenceService(
                    session=self.session,
                    settings=self.settings,
                    object_store=self.object_store,
                ).manual_evidence_context(actor=actor, project_id=project_id)
                if ApprovalState(project.state) not in manual_context.allowed_project_states:
                    base_blockers.append("MANUAL_EVIDENCE_STATE_NOT_ALLOWED")
            except (RuntimeError, ValueError):
                base_blockers.append("MANUAL_EVIDENCE_POLICY_UNAVAILABLE")

        candidates: list[BoqSpreadsheetCandidateView] = []
        for row in rows[:limit]:
            observation, value = self._validated_spreadsheet_candidate(
                row=row,
                organization_id=actor.organization_id,
            )
            proposal, task, verified_observation_id = self._latest_mapping_proposal(
                project_id=project_id,
                document_revision_ids=tuple(document_set.revision_ids),
                source_observation_id=row.id,
            )
            blockers = list(base_blockers)
            if task is not None and task.status in {"PENDING", "APPROVED"}:
                blockers.append("ACTIVE_MAPPING_PROPOSAL")
            quantity_proposal, quantity_task, verified_quantity_id = self._latest_quantity_proposal(
                project_id=project_id,
                document_revision_ids=tuple(document_set.revision_ids),
                source_observation_id=row.id,
                source_item_id=value.source_item_id,
                expected_quantity=value.quantity,
                expected_unit=value.unit,
            )
            quantity_blockers = list(base_blockers)
            if quantity_task is not None and quantity_task.status in {"PENDING", "APPROVED"}:
                quantity_blockers.append("ACTIVE_QUANTITY_PROPOSAL")
            candidates.append(
                BoqSpreadsheetCandidateView(
                    source_observation=observation,
                    source_observation_hash=content_hash(row.payload),
                    source_item_id=value.source_item_id,
                    source_position_id=value.source_position_id,
                    description=value.description,
                    specification=value.specification,
                    source_reference=value.source_reference,
                    unit=value.unit,
                    quantity=value.quantity,
                    worksheet_name=value.worksheet_name,
                    row_number=value.row_number,
                    proposal_observation_id=proposal.id if proposal is not None else None,
                    proposal_task_status=task.status if task is not None else None,
                    verified_observation_id=verified_observation_id,
                    proposal_allowed=not blockers,
                    proposal_blockers=tuple(blockers),
                    quantity_proposal_observation_id=(
                        quantity_proposal.id if quantity_proposal is not None else None
                    ),
                    quantity_proposal_task_status=(
                        quantity_task.status if quantity_task is not None else None
                    ),
                    verified_quantity_observation_id=verified_quantity_id,
                    quantity_proposal_allowed=not quantity_blockers,
                    quantity_proposal_blockers=tuple(quantity_blockers),
                )
            )
        return BoqSpreadsheetCandidateContextView(
            project_id=project_id,
            project_state=ApprovalState(project.state),
            document_set_revision_id=document_set.id,
            candidates=tuple(candidates),
            candidates_truncated=len(rows) > limit,
        )

    def propose_spreadsheet_mapping(
        self,
        *,
        actor: Actor,
        project_id: str,
        source_observation_id: str,
        command: BoqSpreadsheetMappingCommand,
        request_id: str,
        reason: str,
    ) -> Observation:
        project = self._project_service().get_project(
            actor=actor,
            project_id=project_id,
            lock=True,
            required_roles=(ActorRole.TECHNICAL_EXPERT, ActorRole.REVIEWER),
        )
        if ApprovalState(project.state) is not ApprovalState.BOQ_IN_PROGRESS:
            raise ValueError("BoQ spreadsheet review requires BOQ_IN_PROGRESS")
        document_set = require_confirmed_document_set_integrity(
            session=self.session,
            settings=self.settings,
            project_id=project_id,
            document_set_revision_id=project.current_document_set_revision_id,
        )
        source = self.session.scalar(
            select(ObservationRow).where(
                ObservationRow.id == source_observation_id,
                ObservationRow.project_id == project_id,
                ObservationRow.document_revision_id.in_(tuple(document_set.revision_ids)),
            )
        )
        if source is None:
            raise LookupError(source_observation_id)
        source_observation, source_value = self._validated_spreadsheet_candidate(
            row=source,
            organization_id=actor.organization_id,
        )
        if content_hash(source.payload) != command.expected_source_observation_hash:
            raise OptimisticLockError(
                "Imported BoQ row changed after it was loaded; reload before mapping"
            )
        _proposal, active_task, _verified_id = self._latest_mapping_proposal(
            project_id=project_id,
            document_revision_ids=tuple(document_set.revision_ids),
            source_observation_id=source.id,
        )
        if active_task is not None and active_task.status in {"PENDING", "APPROVED"}:
            raise ValueError("Imported BoQ row already has an active mapping proposal")
        evidence_service = EvidenceService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        )
        manual_context = evidence_service.manual_evidence_context(
            actor=actor,
            project_id=project_id,
        )
        if ApprovalState(project.state) not in manual_context.allowed_project_states:
            raise ValueError("Manual evidence policy does not allow mapping in this project state")
        mapping_value = {
            "schema_version": BOQ_LINE_MAPPING_SCHEMA,
            "source_item_id": source_value.source_item_id,
            "source_position_id": source_value.source_position_id,
            "work_code": command.work_code,
            "description": command.description,
            "unit": command.unit,
            "mapping_scope": "IDENTITY_DESCRIPTION_UNIT_ONLY",
            "quantity_promoted": False,
        }
        return evidence_service.record_observation(
            actor=actor,
            project_id=project_id,
            draft=ObservationDraft(
                field_name="boq_line",
                value=mapping_value,
                unit=command.unit,
                method=EvidenceMethod.MANUAL,
                method_version=manual_context.policy_version_id,
                source_priority=source_observation.source_priority,
                location=source_observation.location,
                observed_at=command.proposed_at,
                confidence=None,
                adapter_qualification_id=None,
                basis_metadata={},
            ),
            request_id=request_id,
            reason=reason,
            upstream_observation_ids=(source.id,),
        )

    def propose_spreadsheet_quantity(
        self,
        *,
        actor: Actor,
        project_id: str,
        source_observation_id: str,
        command: BoqSpreadsheetQuantityCommand,
        request_id: str,
        reason: str,
    ) -> Observation:
        project = self._project_service().get_project(
            actor=actor,
            project_id=project_id,
            lock=True,
            required_roles=(ActorRole.TECHNICAL_EXPERT, ActorRole.REVIEWER),
        )
        if ApprovalState(project.state) is not ApprovalState.BOQ_IN_PROGRESS:
            raise ValueError("BoQ spreadsheet review requires BOQ_IN_PROGRESS")
        document_set = require_confirmed_document_set_integrity(
            session=self.session,
            settings=self.settings,
            project_id=project_id,
            document_set_revision_id=project.current_document_set_revision_id,
        )
        source = self.session.scalar(
            select(ObservationRow).where(
                ObservationRow.id == source_observation_id,
                ObservationRow.project_id == project_id,
                ObservationRow.document_revision_id.in_(tuple(document_set.revision_ids)),
            )
        )
        if source is None:
            raise LookupError(source_observation_id)
        source_observation, source_value = self._validated_spreadsheet_candidate(
            row=source,
            organization_id=actor.organization_id,
        )
        if content_hash(source.payload) != command.expected_source_observation_hash:
            raise OptimisticLockError(
                "Imported BoQ row changed after it was loaded; reload before quantity review"
            )
        _proposal, active_task, _verified_id = self._latest_quantity_proposal(
            project_id=project_id,
            document_revision_ids=tuple(document_set.revision_ids),
            source_observation_id=source.id,
            source_item_id=source_value.source_item_id,
            expected_quantity=source_value.quantity,
            expected_unit=source_value.unit,
        )
        if active_task is not None and active_task.status in {"PENDING", "APPROVED"}:
            raise ValueError("Imported BoQ row already has an active quantity proposal")
        evidence_service = EvidenceService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        )
        manual_context = evidence_service.manual_evidence_context(
            actor=actor,
            project_id=project_id,
        )
        if ApprovalState(project.state) not in manual_context.allowed_project_states:
            raise ValueError("Manual evidence policy does not allow quantity review in this state")
        return evidence_service.record_observation(
            actor=actor,
            project_id=project_id,
            draft=ObservationDraft(
                field_name=f"boq_quantity:{source_value.source_item_id}",
                value=source_value.quantity,
                unit=source_value.unit,
                method=EvidenceMethod.MANUAL,
                method_version=manual_context.policy_version_id,
                source_priority=source_observation.source_priority,
                location=source_observation.location,
                observed_at=command.proposed_at,
                confidence=None,
                adapter_qualification_id=None,
                basis_metadata={},
            ),
            request_id=request_id,
            reason=reason,
            upstream_observation_ids=(source.id,),
        )

    def line_review(
        self,
        *,
        actor: Actor,
        project_id: str,
        line_id: str,
    ) -> BoqLineReviewView:
        project = self._project_service().get_project(
            actor=actor,
            project_id=project_id,
            required_roles=(ActorRole.TECHNICAL_EXPERT, ActorRole.REVIEWER),
        )
        row = self.session.scalar(
            select(BoqLineRow).where(
                BoqLineRow.id == line_id,
                BoqLineRow.project_id == project_id,
            )
        )
        if row is None:
            raise LookupError(line_id)
        document_set = require_confirmed_document_set_integrity(
            session=self.session,
            settings=self.settings,
            project_id=project_id,
            document_set_revision_id=project.current_document_set_revision_id,
        )
        evidence_ids = tuple(row.payload.get("evidence_observation_ids", []))
        observations = self._verified_observations(
            project_id,
            evidence_ids,
            document_revision_ids=frozenset(document_set.revision_ids),
        )
        blockers: list[str] = []
        if ApprovalState(project.state) not in {
            ApprovalState.BOQ_IN_PROGRESS,
            ApprovalState.BOQ_REVIEW,
        }:
            blockers.append("PROJECT_STATE_NOT_ALLOWED")
        if not row.is_current:
            blockers.append("LINE_SUPERSEDED")
        if row.status != VerificationStatus.IN_REVIEW.value:
            blockers.append("LINE_NOT_IN_REVIEW")
        created_by = row.payload.get("created_by")
        if not isinstance(created_by, str) or not created_by:
            blockers.append("LINE_CREATOR_MISSING")
            created_by = ""
        elif created_by == actor.actor_id:
            blockers.append("FOUR_EYES_LINE_AUTHOR")
        document_set_revision_id = row.payload.get("document_set_revision_id")
        if (
            not isinstance(document_set_revision_id, str)
            or not document_set_revision_id
            or document_set_revision_id != project.current_document_set_revision_id
        ):
            blockers.append("DOCUMENT_SET_MISMATCH")
            document_set_revision_id = (
                document_set_revision_id if isinstance(document_set_revision_id, str) else ""
            )
        if not any(self._observation_supports_line(item, row) for item in observations.values()):
            blockers.append("SUPPORTING_EVIDENCE_MISMATCH")
        return BoqLineReviewView(
            line=self._line_view(row),
            created_by=created_by,
            document_set_revision_id=document_set_revision_id,
            evidence_observations=tuple(
                self._validated_observation(observations[item_id]) for item_id in evidence_ids
            ),
            verification_allowed=not blockers,
            verification_blockers=tuple(dict.fromkeys(blockers)),
        )

    def initial_quantity_context(
        self,
        *,
        actor: Actor,
        project_id: str,
        line_id: str,
    ) -> InitialQuantityContextView:
        project = self._project_service().get_project(
            actor=actor,
            project_id=project_id,
            required_roles=(
                ActorRole.ESTIMATOR,
                ActorRole.TECHNICAL_EXPERT,
                ActorRole.REVIEWER,
            ),
        )
        line = self.session.scalar(
            select(BoqLineRow).where(
                BoqLineRow.id == line_id,
                BoqLineRow.project_id == project_id,
            )
        )
        if line is None:
            raise LookupError(line_id)
        document_set = require_confirmed_document_set_integrity(
            session=self.session,
            settings=self.settings,
            project_id=project_id,
            document_set_revision_id=project.current_document_set_revision_id,
        )
        blockers: list[str] = []
        if ApprovalState(project.state) not in {
            ApprovalState.BOQ_IN_PROGRESS,
            ApprovalState.BOQ_REVIEW,
        }:
            blockers.append("PROJECT_STATE_NOT_ALLOWED")
        if not line.is_current:
            blockers.append("LINE_SUPERSEDED")
        if line.status != VerificationStatus.VERIFIED.value:
            blockers.append("LINE_NOT_VERIFIED")
        if line.payload.get("document_set_revision_id") != document_set.id:
            blockers.append("DOCUMENT_SET_MISMATCH")
        if not actor.roles.intersection({ActorRole.ESTIMATOR, ActorRole.TECHNICAL_EXPERT}):
            blockers.append("QUANTITY_RECORDING_ROLE_REQUIRED")
        source_item_id = self._spreadsheet_source_item_for_line(
            project_id=project_id,
            line=line,
            document_revision_ids=frozenset(document_set.revision_ids),
        )
        if source_item_id is None:
            blockers.append("SPREADSHEET_SOURCE_MAPPING_REQUIRED")
        evidence_candidates: list[InitialQuantityEvidenceCandidateView] = []
        if source_item_id is not None:
            rows = list(
                self.session.scalars(
                    select(ObservationRow)
                    .where(
                        ObservationRow.project_id == project_id,
                        ObservationRow.document_revision_id.in_(tuple(document_set.revision_ids)),
                        ObservationRow.field_name == f"boq_quantity:{source_item_id}",
                        ObservationRow.status == VerificationStatus.VERIFIED.value,
                    )
                    .order_by(ObservationRow.created_at.desc(), ObservationRow.id.desc())
                )
            )
            for row in rows:
                observation, source_value = self._validated_quantity_evidence_for_source(
                    row=row,
                    source_item_id=source_item_id,
                    organization_id=actor.organization_id,
                )
                quantity = Decimal(str(observation.value))
                evidence_candidates.append(
                    InitialQuantityEvidenceCandidateView(
                        observation=observation,
                        observation_hash=content_hash(row.payload),
                        source_item_id=source_item_id,
                        value=quantity,
                        unit=source_value.unit,
                    )
                )
        if not evidence_candidates:
            blockers.append("VERIFIED_SPREADSHEET_QUANTITY_REQUIRED")
        if len(evidence_candidates) > 1:
            blockers.append("MULTIPLE_VERIFIED_SPREADSHEET_QUANTITIES")
        current_quantity = self.session.scalar(
            select(QuantityRow).where(
                QuantityRow.boq_line_id == line.id,
                QuantityRow.is_current.is_(True),
            )
        )
        if current_quantity is not None:
            blockers.append("CURRENT_QUANTITY_ALREADY_EXISTS")
        if bool(line.payload.get("critical_quantity")):
            blockers.append("INDEPENDENT_QUANTITY_EVIDENCE_REQUIRED")
        quantity_policy_version_id: str | None = None
        try:
            policy_row = self._bound_version(
                project_id,
                purpose="quantity_policy",
                kind="quantity_policy",
            )
            policy_payload = policy_row.payload.get("policy")
            if not isinstance(policy_payload, dict):
                raise ValueError("Approved quantity policy payload is missing 'policy'")
            policy = QuantityValidationPolicy.model_validate(
                {"policy_version": policy_row.id, **policy_payload}
            )
            quantity_policy_version_id = policy_row.id
            if policy.absolute_tolerance is None or policy.relative_tolerance is None:
                blockers.append("QUANTITY_POLICY_THRESHOLDS_UNCONFIGURED")
        except (RuntimeError, ValueError):
            blockers.append("QUANTITY_POLICY_UNAVAILABLE")
        blockers = list(dict.fromkeys(blockers))
        return InitialQuantityContextView(
            project_id=project_id,
            project_state=ApprovalState(project.state),
            document_set_revision_id=document_set.id,
            line=self._line_view(line),
            source_item_id=source_item_id,
            quantity_policy_version_id=quantity_policy_version_id,
            evidence_candidates=tuple(evidence_candidates),
            current_quantity_id=current_quantity.id if current_quantity is not None else None,
            current_quantity_status=(
                VerificationStatus(current_quantity.status)
                if current_quantity is not None
                else None
            ),
            recording_allowed=not blockers,
            recording_blockers=tuple(blockers),
        )

    def attach_imported_quantity(
        self,
        *,
        actor: Actor,
        project_id: str,
        line_id: str,
        command: AttachImportedQuantityCommand,
        request_id: str,
        reason: str,
    ) -> QuantityExecutionResult:
        reason = reason.strip()
        if not reason or len(reason) > 2000:
            raise ValueError("Quantity attachment reason must contain 1 to 2000 characters")
        project = self._project_service().get_project(
            actor=actor,
            project_id=project_id,
            lock=True,
            required_roles=(ActorRole.ESTIMATOR, ActorRole.TECHNICAL_EXPERT),
        )
        context = self.initial_quantity_context(
            actor=actor,
            project_id=project_id,
            line_id=line_id,
        )
        expected_updated_at = ensure_utc(command.expected_line_updated_at)
        if ensure_utc(context.line.updated_at) != expected_updated_at:
            raise OptimisticLockError(
                "BoQ line changed after quantity context was loaded; reload before attaching"
            )
        if context.recording_blockers:
            raise ValueError(
                "Initial spreadsheet quantity is BLOCKED: " + ", ".join(context.recording_blockers)
            )
        candidate = next(
            (
                item
                for item in context.evidence_candidates
                if item.observation.observation_id == command.source_observation_id
            ),
            None,
        )
        if candidate is None:
            raise ValueError("Selected quantity evidence is not in the current controlled context")
        row = self.session.get(ObservationRow, command.source_observation_id)
        if row is None or content_hash(row.payload) != command.expected_source_observation_hash:
            raise OptimisticLockError(
                "Quantity evidence changed after it was loaded; reload before attaching"
            )
        exponent = candidate.value.as_tuple().exponent
        if not isinstance(exponent, int):
            raise RuntimeError("Verified spreadsheet quantity is not finite")
        result = self.record_quantity(
            actor=actor,
            project_id=project.id,
            line_id=line_id,
            submission=QuantitySubmission(
                draft=QuantityDraft(
                    value=candidate.value,
                    unit=candidate.unit,
                    source_observation_ids=(candidate.observation.observation_id,),
                    source_priority=candidate.observation.source_priority,
                    rounding_scale=max(0, -exponent),
                    waste_factor=Decimal("0"),
                    alternative_quantity_ids=(),
                    manual_change_id=None,
                ),
                formula=None,
                formula_input_observation_ids={},
            ),
            request_id=request_id,
            reason=reason,
        )
        if (
            not result.validation.passed
            or result.quantity.status is not VerificationStatus.VERIFIED
        ):
            validation_blockers = ", ".join(item.code.value for item in result.validation.findings)
            raise ValueError(
                "Initial spreadsheet quantity validation is BLOCKED: " + validation_blockers
            )
        return result

    def create_line(
        self,
        *,
        actor: Actor,
        project_id: str,
        draft: BoqLineDraft,
        request_id: str,
        reason: str,
    ) -> BoqLineView:
        reason = reason.strip()
        if not reason or len(reason) > 2000:
            raise ValueError("BoQ line reason must contain 1 to 2000 characters")
        project_service = self._project_service()
        project = project_service.get_project(
            actor=actor,
            project_id=project_id,
            lock=True,
            required_roles=(ActorRole.ESTIMATOR, ActorRole.TECHNICAL_EXPERT),
        )
        if ApprovalState(project.state) is not ApprovalState.BOQ_IN_PROGRESS:
            raise ValueError("BoQ lines may be created only in BOQ_IN_PROGRESS")
        document_set = require_confirmed_document_set_integrity(
            session=self.session,
            settings=self.settings,
            project_id=project_id,
            document_set_revision_id=project.current_document_set_revision_id,
        )
        observations = self._verified_observations(
            project_id,
            draft.evidence_observation_ids,
            document_revision_ids=frozenset(document_set.revision_ids),
        )
        if not any(
            isinstance((observation := self._validated_observation(row)).value, dict)
            and observation.value.get("work_code") == draft.work_code
            and observation.value.get("unit") == draft.unit
            and (
                "description" not in observation.value
                or observation.value.get("description") == draft.description
            )
            for row in observations.values()
        ):
            raise ValueError(
                "No verified evidence reproduces the BoQ work code, description and unit"
            )
        identity = {
            "project_id": project_id,
            "document_set_revision_id": document_set.id,
            "draft": draft,
        }
        line_id = f"boq-line-{content_hash(identity)[:24]}"
        existing = self.session.get(BoqLineRow, line_id)
        if existing is not None:
            if not existing.is_current:
                raise ValueError("An identical superseded BoQ revision cannot become current again")
            return self._line_view(existing)
        previous = self.session.scalar(
            select(BoqLineRow)
            .where(
                BoqLineRow.project_id == project_id,
                BoqLineRow.line_key == draft.line_key,
                BoqLineRow.is_current.is_(True),
            )
            .with_for_update()
        )
        now = utc_now()
        if previous is not None:
            previous.is_current = False
            previous.updated_at = now
        row = BoqLineRow(
            id=line_id,
            project_id=project_id,
            line_key=draft.line_key,
            wbs_node_id=draft.wbs_node_id,
            work_code=draft.work_code,
            description=draft.description,
            unit=draft.unit,
            status=VerificationStatus.IN_REVIEW.value,
            supersedes_line_id=previous.id if previous else None,
            is_current=True,
            payload={
                "evidence_observation_ids": list(draft.evidence_observation_ids),
                "critical_quantity": draft.critical_quantity,
                "cost_components": [item.model_dump(mode="json") for item in draft.cost_components],
                "created_by": actor.actor_id,
                "document_set_revision_id": document_set.id,
            },
            created_at=now,
            updated_at=now,
        )
        self.session.add(row)
        project_service.record_event(
            aggregate_type="project",
            aggregate_id=project_id,
            event_type="boq_line_created",
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload={
                "line_id": line_id,
                "supersedes_line_id": row.supersedes_line_id,
                **draft.model_dump(mode="json"),
            },
        )
        return self._line_view(row)

    def verify_line(
        self,
        *,
        actor: Actor,
        project_id: str,
        line_id: str,
        expected_line_updated_at: datetime,
        request_id: str,
        reason: str,
    ) -> BoqLineView:
        reason = reason.strip()
        if not reason or len(reason) > 2000:
            raise ValueError("BoQ verification reason must contain 1 to 2000 characters")
        expected_updated_at = ensure_utc(expected_line_updated_at)
        if expected_updated_at is None or expected_line_updated_at.tzinfo is None:
            raise ValueError("Expected BoQ line timestamp must include a timezone")
        project_service = self._project_service()
        project = project_service.get_project(
            actor=actor,
            project_id=project_id,
            lock=True,
            required_roles=(ActorRole.TECHNICAL_EXPERT, ActorRole.REVIEWER),
        )
        if ApprovalState(project.state) not in {
            ApprovalState.BOQ_IN_PROGRESS,
            ApprovalState.BOQ_REVIEW,
        }:
            raise ValueError("BoQ verification requires BOQ_IN_PROGRESS or BOQ_REVIEW")
        row = self.session.scalar(
            select(BoqLineRow)
            .where(
                BoqLineRow.id == line_id,
                BoqLineRow.project_id == project_id,
                BoqLineRow.is_current.is_(True),
            )
            .with_for_update()
        )
        if row is None:
            raise LookupError(line_id)
        if ensure_utc(row.updated_at) != expected_updated_at:
            raise OptimisticLockError(
                "BoQ line changed after it was loaded; reload before verification"
            )
        if row.status == VerificationStatus.VERIFIED.value:
            return self._line_view(row)
        if row.payload.get("document_set_revision_id") != project.current_document_set_revision_id:
            raise ValueError("BoQ line belongs to a superseded document-set revision")
        document_set = require_confirmed_document_set_integrity(
            session=self.session,
            settings=self.settings,
            project_id=project_id,
            document_set_revision_id=project.current_document_set_revision_id,
        )
        if row.payload.get("created_by") == actor.actor_id:
            raise ValueError("BoQ line requires independent four-eyes verification")
        observations = self._verified_observations(
            project_id,
            tuple(row.payload.get("evidence_observation_ids", [])),
            document_revision_ids=frozenset(document_set.revision_ids),
        )
        if not any(self._observation_supports_line(item, row) for item in observations.values()):
            raise ValueError(
                "No verified evidence reproduces the BoQ work code, description and unit"
            )
        row.status = VerificationStatus.VERIFIED.value
        row.updated_at = utc_now()
        row.payload = {
            **row.payload,
            "verified_by": actor.actor_id,
            "verified_at": row.updated_at.isoformat(),
        }
        project_service.record_event(
            aggregate_type="project",
            aggregate_id=project_id,
            event_type="boq_line_verified",
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload={"line_id": row.id, "evidence_ids": list(observations)},
        )
        return self._line_view(row)

    def propose_quantity_manual_change(
        self,
        *,
        actor: Actor,
        project_id: str,
        line_id: str,
        submission: QuantitySubmission,
        request_id: str,
        reason: str,
    ) -> QuantityManualChangeView:
        reason = reason.strip()
        if not reason:
            raise ValueError("Manual-change reason is required")
        if len(reason) > 2000:
            raise ValueError("Manual-change reason exceeds 2000 characters")
        if submission.draft.manual_change_id is not None:
            raise ValueError("A manual-change proposal cannot reference another change")
        project_service = self._project_service()
        project = project_service.get_project(
            actor=actor,
            project_id=project_id,
            lock=True,
            required_roles=(ActorRole.ESTIMATOR, ActorRole.TECHNICAL_EXPERT),
        )
        if ApprovalState(project.state) not in {
            ApprovalState.BOQ_IN_PROGRESS,
            ApprovalState.BOQ_REVIEW,
        }:
            raise ValueError("Quantity manual changes require BOQ_IN_PROGRESS or BOQ_REVIEW")
        line = self._current_quantity_line(project_id, line_id, lock=True)
        candidate, validation, _ = self._prepare_quantity_candidate(
            project_id=project_id,
            line=line,
            submission=submission,
            quantity_id=f"quantity-change-candidate-{uuid4()}",
        )
        if not validation.passed:
            raise ValueError("A manual-change proposal cannot bypass quantity validation")
        previous = self.session.scalar(
            select(QuantityRow)
            .where(
                QuantityRow.boq_line_id == line.id,
                QuantityRow.is_current.is_(True),
            )
            .with_for_update()
        )
        if previous is None:
            raise ValueError("An initial quantity is not a manual change")
        before = self._quantity_state_from_row(previous)
        after = self._quantity_state_from_submission(submission)
        if before == after:
            raise ValueError("A manual-change proposal must change the quantity record")
        policy_row, rule = self._manual_change_rule(project_id)
        document_set_revision_id = project.current_document_set_revision_id
        if document_set_revision_id is None:
            raise ValueError("A confirmed current document set is required")
        identity = {
            "project_id": project_id,
            "line_id": line.id,
            "previous_quantity_id": previous.id,
            "before": before,
            "after": after,
            "policy_version_id": policy_row.id,
            "document_set_revision_id": document_set_revision_id,
            "changed_by": actor.actor_id,
            "reason": reason,
        }
        change_id = f"manual-change-{content_hash(identity)[:24]}"
        existing = self.session.get(ManualChangeRow, change_id)
        if existing is not None:
            return self._quantity_manual_change_view(existing)
        now = utc_now()
        approval_task_id = (
            f"approval-task-{content_hash({'manual_change_id': change_id})[:24]}"
            if rule.critical
            else None
        )
        source_observation_ids = sorted(candidate.source_observation_ids)
        change = ManualChangeRow(
            id=change_id,
            project_id=project_id,
            entity_type="quantity",
            entity_id=line.id,
            field_name="record",
            critical=rule.critical,
            changed_by=actor.actor_id,
            reason=reason,
            payload={
                "lifecycle_version": "quantity-manual-change-v1",
                "previous_quantity_id": previous.id,
                "before": before,
                "before_hash": content_hash(before),
                "after": after,
                "after_hash": content_hash(after),
                "policy_version_id": policy_row.id,
                "document_set_revision_id": document_set_revision_id,
                "approval_task_id": approval_task_id,
                "source_observation_ids": source_observation_ids,
            },
            changed_at=now,
        )
        self.session.add(change)
        if rule.critical:
            if approval_task_id is None or rule.assigned_role is None:
                raise ValueError("Critical manual-change policy is incomplete")
            self.session.add(
                ApprovalTaskRow(
                    id=approval_task_id,
                    project_id=project_id,
                    task_type="MANUAL_CHANGE",
                    entity_type="manual_change",
                    entity_id=change_id,
                    assigned_role=rule.assigned_role.value,
                    status="PENDING",
                    required=True,
                    payload={
                        "created_by": actor.actor_id,
                        "policy_version_id": policy_row.id,
                        "observation_ids": source_observation_ids,
                        "before_hash": content_hash(before),
                        "after_hash": content_hash(after),
                    },
                    created_at=now,
                    updated_at=now,
                )
            )
        project_service.record_event(
            aggregate_type="project",
            aggregate_id=project_id,
            event_type="quantity_manual_change_proposed",
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload={
                "manual_change_id": change_id,
                "line_id": line.id,
                "previous_quantity_id": previous.id,
                "critical": rule.critical,
                "policy_version_id": policy_row.id,
                "approval_task_id": approval_task_id,
                "before_hash": content_hash(before),
                "after_hash": content_hash(after),
                "source_observation_ids": source_observation_ids,
            },
        )
        return self._quantity_manual_change_view(change)

    def quantity_change_context(
        self,
        *,
        actor: Actor,
        project_id: str,
        line_id: str,
    ) -> QuantityChangeContextView:
        project = self._project_service().get_project(
            actor=actor,
            project_id=project_id,
            required_roles=(ActorRole.ESTIMATOR, ActorRole.TECHNICAL_EXPERT),
        )
        if ApprovalState(project.state) not in {
            ApprovalState.BOQ_IN_PROGRESS,
            ApprovalState.BOQ_REVIEW,
        }:
            raise ValueError("Quantity manual changes require BOQ_IN_PROGRESS or BOQ_REVIEW")
        line = self._current_quantity_line(project_id, line_id, lock=False)
        current = self.session.scalar(
            select(QuantityRow).where(
                QuantityRow.boq_line_id == line.id,
                QuantityRow.is_current.is_(True),
            )
        )
        if current is None:
            raise ValueError("A current quantity is required before proposing a manual change")
        document_set_revision_id = project.current_document_set_revision_id
        if document_set_revision_id is None:
            raise ValueError("A confirmed current document set is required")
        quantity_policy = self._bound_version(
            project_id,
            purpose="quantity_policy",
            kind="quantity_policy",
        )
        manual_change_policy, rule = self._manual_change_rule(project_id)
        formula_rules_binding = self.session.scalar(
            select(ProjectControlledVersionRow).where(
                ProjectControlledVersionRow.project_id == project_id,
                ProjectControlledVersionRow.purpose == "quantity_formula_rules",
            )
        )
        formula_rules = (
            self._bound_version(
                project_id,
                purpose="quantity_formula_rules",
                kind="quantity_formula_rules",
            )
            if formula_rules_binding is not None
            else None
        )
        submission = QuantitySubmission.model_validate(self._quantity_state_from_row(current))
        if submission.formula is not None and (
            formula_rules is None or submission.formula.formula_version != formula_rules.id
        ):
            raise ValueError("Current quantity formula is no longer governed by the bound rules")
        return QuantityChangeContextView(
            project_id=project_id,
            line_id=line.id,
            line_key=line.line_key,
            description=line.description,
            unit=line.unit,
            current_quantity_id=current.id,
            current_quantity_status=VerificationStatus(current.status),
            current_submission=submission,
            document_set_revision_id=document_set_revision_id,
            quantity_policy_version_id=quantity_policy.id,
            quantity_formula_rules_version_id=(
                formula_rules.id if formula_rules is not None else None
            ),
            manual_change_policy_version_id=manual_change_policy.id,
            critical=rule.critical,
            approval_role=rule.assigned_role,
        )

    def quantity_manual_change_review(
        self,
        *,
        actor: Actor,
        project_id: str,
        change_id: str,
    ) -> QuantityManualChangeView:
        self._project_service().get_project(
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
        change = self.session.scalar(
            select(ManualChangeRow).where(
                ManualChangeRow.id == change_id,
                ManualChangeRow.project_id == project_id,
                ManualChangeRow.entity_type == "quantity",
                ManualChangeRow.field_name == "record",
            )
        )
        if change is None:
            raise LookupError(change_id)
        return self._quantity_manual_change_view(change)

    def apply_quantity_manual_change(
        self,
        *,
        actor: Actor,
        project_id: str,
        change_id: str,
        request_id: str,
        reason: str,
    ) -> QuantityExecutionResult:
        reason = reason.strip()
        if not reason:
            raise ValueError("Manual-change application reason is required")
        if len(reason) > 2000:
            raise ValueError("Manual-change application reason exceeds 2000 characters")
        change = self.session.scalar(
            select(ManualChangeRow).where(
                ManualChangeRow.id == change_id,
                ManualChangeRow.project_id == project_id,
                ManualChangeRow.entity_type == "quantity",
                ManualChangeRow.field_name == "record",
            )
        )
        if change is None:
            raise LookupError(change_id)
        if change.changed_by != actor.actor_id:
            raise ValueError("Only the registered change author may apply the exact revision")
        raw_after = change.payload.get("after")
        if not isinstance(raw_after, dict):
            raise ValueError("Quantity manual change has no reproducible after-state")
        submission = QuantitySubmission.model_validate(raw_after)
        if submission.draft.manual_change_id is not None:
            raise ValueError("Stored manual-change after-state contains a nested change identity")
        submission = submission.model_copy(
            update={"draft": submission.draft.model_copy(update={"manual_change_id": change.id})}
        )
        return self.record_quantity(
            actor=actor,
            project_id=project_id,
            line_id=change.entity_id,
            submission=submission,
            request_id=request_id,
            reason=reason,
        )

    def record_quantity(
        self,
        *,
        actor: Actor,
        project_id: str,
        line_id: str,
        submission: QuantitySubmission,
        request_id: str,
        reason: str,
    ) -> QuantityExecutionResult:
        project_service = self._project_service()
        project = project_service.get_project(
            actor=actor,
            project_id=project_id,
            lock=True,
            required_roles=(ActorRole.ESTIMATOR, ActorRole.TECHNICAL_EXPERT),
        )
        if ApprovalState(project.state) not in {
            ApprovalState.BOQ_IN_PROGRESS,
            ApprovalState.BOQ_REVIEW,
        }:
            raise ValueError("Quantity recording requires BOQ_IN_PROGRESS or BOQ_REVIEW")
        line = self._current_quantity_line(project_id, line_id, lock=True)
        quantity_id = f"quantity-{uuid4()}"
        candidate, validation, policy_row = self._prepare_quantity_candidate(
            project_id=project_id,
            line=line,
            submission=submission,
            quantity_id=quantity_id,
        )
        status = VerificationStatus.VERIFIED if validation.passed else VerificationStatus.CONFLICT
        quantity = candidate.model_copy(update={"status": status})
        previous = self.session.scalar(
            select(QuantityRow)
            .where(
                QuantityRow.boq_line_id == line.id,
                QuantityRow.is_current.is_(True),
            )
            .with_for_update()
        )
        manual_change: ManualChangeRow | None = None
        manual_change_approval_id: str | None = None
        if previous is None:
            if submission.draft.manual_change_id is not None:
                raise ValueError("An initial quantity cannot consume a manual change")
        else:
            if not validation.passed:
                raise ValueError("A quantity revision cannot bypass current quantity validation")
            manual_change, manual_change_approval_id = self._validated_quantity_manual_change(
                actor=actor,
                project_id=project_id,
                document_set_revision_id=project.current_document_set_revision_id,
                line=line,
                previous=previous,
                submission=submission,
            )
        if previous is not None:
            previous.is_current = False
            previous.updated_at = utc_now()
        now = utc_now()
        if validation.passed and previous is not None:
            old_findings = list(
                self.session.scalars(
                    select(VerificationFindingRow).where(
                        VerificationFindingRow.project_id == project_id,
                        VerificationFindingRow.contour == "QUANTITY",
                        VerificationFindingRow.resolved.is_(False),
                    )
                )
            )
            for old_finding in old_findings:
                if previous.id in old_finding.payload.get("entity_ids", []):
                    old_finding.resolved = True
                    old_finding.updated_at = now
                    old_finding.payload = {
                        **old_finding.payload,
                        "resolved_by_quantity_id": quantity.quantity_id,
                        "resolved_at": now.isoformat(),
                    }
        quantity_row = QuantityRow(
            id=quantity.quantity_id,
            boq_line_id=line.id,
            value=quantity.value,
            unit=quantity.unit,
            status=quantity.status.value,
            supersedes_quantity_id=previous.id if previous else None,
            is_current=True,
            payload={
                "record": quantity.model_dump(mode="json"),
                "formula": (
                    submission.formula.model_dump(mode="json") if submission.formula else None
                ),
                "formula_input_observation_ids": (submission.formula_input_observation_ids),
                "validation": validation.model_dump(mode="json"),
                "quantity_policy_version_id": policy_row.id,
                "recorded_by": actor.actor_id,
            },
            created_at=now,
            updated_at=now,
        )
        self.session.add(quantity_row)
        manual_change_application_id: str | None = None
        if manual_change is not None:
            manual_change_application_id = (
                f"manual-change-application-"
                f"{content_hash((manual_change.id, quantity.quantity_id))[:24]}"
            )
            self.session.add(
                QuantityManualChangeApplicationRow(
                    id=manual_change_application_id,
                    project_id=project_id,
                    manual_change_id=manual_change.id,
                    quantity_id=quantity.quantity_id,
                    applied_by=actor.actor_id,
                    payload={
                        "manual_change_hash": content_hash(manual_change.payload),
                        "before_hash": manual_change.payload["before_hash"],
                        "after_hash": manual_change.payload["after_hash"],
                        "policy_version_id": manual_change.payload["policy_version_id"],
                        "approval_id": manual_change_approval_id,
                    },
                    applied_at=now,
                )
            )
        for finding in validation.findings:
            self._persist_finding(project_id, "QUANTITY", finding, now)
        project_service.record_event(
            aggregate_type="project",
            aggregate_id=project_id,
            event_type="quantity_recorded",
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload={
                "line_id": line.id,
                "quantity": quantity.model_dump(mode="json"),
                "validation": validation.model_dump(mode="json"),
                "supersedes_quantity_id": previous.id if previous else None,
                "policy_version_id": policy_row.id,
                "manual_change_id": manual_change.id if manual_change else None,
                "manual_change_application_id": manual_change_application_id,
                "manual_change_approval_id": manual_change_approval_id,
            },
        )
        return QuantityExecutionResult(
            quantity=quantity,
            validation=validation,
            supersedes_quantity_id=previous.id if previous else None,
        )

    def run_scope_completeness(
        self,
        *,
        actor: Actor,
        project_id: str,
        wbs_node_id: str,
        request_id: str,
        reason: str,
    ) -> ScopeRunResult:
        normalized_wbs_node_id = wbs_node_id.strip()
        reason = reason.strip()
        if (
            not normalized_wbs_node_id
            or normalized_wbs_node_id != wbs_node_id
            or len(wbs_node_id) > 128
        ):
            raise ValueError(
                "Scope completeness WBS node must be normalized and contain 1 to 128 characters"
            )
        if not reason or len(reason) > 2000:
            raise ValueError("Scope completeness reason must contain 1 to 2000 characters")
        project_service = self._project_service()
        project = project_service.get_project(
            actor=actor,
            project_id=project_id,
            lock=True,
            required_roles=(ActorRole.TECHNICAL_EXPERT, ActorRole.REVIEWER),
        )
        if ApprovalState(project.state) is not ApprovalState.BOQ_REVIEW:
            raise ValueError("Scope completeness runs only in BOQ_REVIEW")
        rule_row = self._bound_version(
            project_id,
            purpose="scope_rules",
            kind="scope_rules",
        )
        raw_rules = rule_row.payload.get("rules")
        if not isinstance(raw_rules, list) or not raw_rules:
            raise ValueError("Approved scope rule pack contains no rules")
        rules = tuple(
            ScopeRule.model_validate({**item, "rule_pack_version_id": rule_row.id})
            for item in raw_rules
        )
        lines = list(
            self.session.scalars(
                select(BoqLineRow).where(
                    BoqLineRow.project_id == project_id,
                    BoqLineRow.wbs_node_id == wbs_node_id,
                    BoqLineRow.status == VerificationStatus.VERIFIED.value,
                    BoqLineRow.is_current.is_(True),
                )
            )
        )
        if not lines:
            raise ValueError(
                "Scope completeness requires at least one current verified BoQ line "
                "for the exact WBS node"
            )
        project_tags, tag_findings = self._verified_project_tags(project_id, rules)
        now = utc_now()
        input_signature = scope_input_signature(
            self.session,
            project_id,
            wbs_node_id,
        )
        if tag_findings:
            for finding in tag_findings:
                self._persist_finding(project_id, "SCOPE", finding, now)
            scope_evaluation_id = self._record_scope_evaluation(
                project_id=project_id,
                wbs_node_id=wbs_node_id,
                rule_pack_version_id=rule_row.id,
                input_signature=input_signature,
                status="BLOCKED",
                payload={
                    "validation_findings": [item.model_dump(mode="json") for item in tag_findings],
                    "evaluated_by": actor.actor_id,
                },
                now=now,
            )
            project_service.record_event(
                aggregate_type="project",
                aggregate_id=project_id,
                event_type="scope_evaluation_blocked",
                actor=actor,
                request_id=request_id,
                reason=reason,
                payload={
                    "wbs_node_id": wbs_node_id,
                    "rule_pack_version_id": rule_row.id,
                    "scope_evaluation_id": scope_evaluation_id,
                    "findings": [item.model_dump(mode="json") for item in tag_findings],
                },
            )
            return ScopeRunResult(validation_findings=tag_findings)
        evaluation = evaluate_scope(
            wbs_node_id=wbs_node_id,
            present_work_codes=frozenset(line.work_code for line in lines),
            project_tags=project_tags,
            rules=rules,
        )
        emitted_ids = {scope_finding.finding_id for scope_finding in evaluation.findings}
        prior = list(
            self.session.scalars(
                select(ScopeFindingRow).where(
                    ScopeFindingRow.project_id == project_id,
                    ScopeFindingRow.resolved.is_(False),
                )
            )
        )
        for row in prior:
            if (
                row.payload.get("rule_pack_version_id") == rule_row.id
                and row.payload.get("wbs_node_id") == wbs_node_id
                and row.id not in emitted_ids
            ):
                row.resolved = True
                row.updated_at = now
                row.payload = {
                    **row.payload,
                    "resolved_by_recalculation": actor.actor_id,
                    "resolved_at": now.isoformat(),
                }
        for scope_finding in evaluation.findings:
            if self.session.get(ScopeFindingRow, scope_finding.finding_id) is not None:
                continue
            self.session.add(
                ScopeFindingRow(
                    id=scope_finding.finding_id,
                    project_id=project_id,
                    rule_id=scope_finding.rule_id,
                    severity=scope_finding.severity.value,
                    resolved=False,
                    payload={
                        **scope_finding.model_dump(mode="json"),
                        "rule_pack_version_id": rule_row.id,
                        "wbs_node_id": wbs_node_id,
                    },
                    created_at=now,
                    updated_at=now,
                )
            )
        scope_evaluation_id = self._record_scope_evaluation(
            project_id=project_id,
            wbs_node_id=wbs_node_id,
            rule_pack_version_id=rule_row.id,
            input_signature=input_signature,
            status="PASSED" if not evaluation.findings else "BLOCKED",
            payload={
                "evaluation": evaluation.model_dump(mode="json"),
                "evaluated_by": actor.actor_id,
            },
            now=now,
        )
        project_service.record_event(
            aggregate_type="project",
            aggregate_id=project_id,
            event_type="scope_completeness_evaluated",
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload={
                **evaluation.model_dump(mode="json"),
                "scope_evaluation_id": scope_evaluation_id,
                "input_signature": input_signature,
            },
        )
        return ScopeRunResult(evaluation=evaluation)

    def _record_scope_evaluation(
        self,
        *,
        project_id: str,
        wbs_node_id: str,
        rule_pack_version_id: str,
        input_signature: str,
        status: str,
        payload: dict[str, Any],
        now: Any,
    ) -> str:
        previous = self.session.scalar(
            select(ScopeEvaluationRow)
            .where(
                ScopeEvaluationRow.project_id == project_id,
                ScopeEvaluationRow.wbs_node_id == wbs_node_id,
                ScopeEvaluationRow.is_current.is_(True),
            )
            .with_for_update()
        )
        if previous is not None:
            previous.is_current = False
        evaluation_id = f"scope-evaluation-{uuid4()}"
        self.session.add(
            ScopeEvaluationRow(
                id=evaluation_id,
                project_id=project_id,
                wbs_node_id=wbs_node_id,
                rule_pack_version_id=rule_pack_version_id,
                status=status,
                input_signature=input_signature,
                supersedes_evaluation_id=previous.id if previous else None,
                is_current=True,
                payload=payload,
                created_at=now,
            )
        )
        return evaluation_id

    def _current_quantity_line(
        self,
        project_id: str,
        line_id: str,
        *,
        lock: bool,
    ) -> BoqLineRow:
        statement = select(BoqLineRow).where(
            BoqLineRow.id == line_id,
            BoqLineRow.project_id == project_id,
            BoqLineRow.is_current.is_(True),
        )
        if lock:
            statement = statement.with_for_update()
        line = self.session.scalar(statement)
        if line is None:
            raise LookupError(line_id)
        if line.status != VerificationStatus.VERIFIED.value:
            raise ValueError("Quantity cannot be attached to an unverified BoQ line")
        return line

    def _prepare_quantity_candidate(
        self,
        *,
        project_id: str,
        line: BoqLineRow,
        submission: QuantitySubmission,
        quantity_id: str,
    ) -> tuple[QuantityRecord, QuantityValidationResult, ControlledVersionRow]:
        if submission.draft.unit != line.unit:
            raise ValueError("Quantity unit differs from the verified BoQ line unit")
        observations = self._verified_observations(
            project_id,
            submission.draft.source_observation_ids,
        )
        if line.payload.get("critical_quantity"):
            self._require_independent_coverage(observations.values())
        self._validate_quantity_evidence(submission, observations)
        policy_row = self._bound_version(
            project_id,
            purpose="quantity_policy",
            kind="quantity_policy",
        )
        policy_payload = policy_row.payload.get("policy")
        if not isinstance(policy_payload, dict):
            raise ValueError("Approved quantity policy payload is missing 'policy'")
        policy = QuantityValidationPolicy.model_validate(
            {"policy_version": policy_row.id, **policy_payload}
        )
        if submission.formula is not None:
            formula_rules = self._bound_version(
                project_id,
                purpose="quantity_formula_rules",
                kind="quantity_formula_rules",
            )
            if submission.formula.formula_version != formula_rules.id:
                raise ValueError("Quantity formula does not match the bound controlled version")
            allowed = formula_rules.payload.get("allowed_operations", [])
            if submission.formula.operation.value not in allowed:
                raise ValueError("Quantity formula operation is not approved")
        candidate = QuantityRecord(
            quantity_id=quantity_id,
            boq_line_id=line.id,
            value=submission.draft.value,
            unit=submission.draft.unit,
            source_observation_ids=submission.draft.source_observation_ids,
            source_priority=submission.draft.source_priority,
            formula=submission.formula.display_formula if submission.formula else None,
            formula_inputs=submission.formula.inputs if submission.formula else {},
            rounding_mode="ROUND_HALF_UP",
            rounding_scale=submission.draft.rounding_scale,
            waste_factor=submission.draft.waste_factor,
            alternative_quantity_ids=submission.draft.alternative_quantity_ids,
            manual_change_id=submission.draft.manual_change_id,
            status=VerificationStatus.IN_REVIEW,
        )
        return (
            candidate,
            validate_quantity(
                candidate,
                formula=submission.formula,
                policy=policy,
            ),
            policy_row,
        )

    def _manual_change_rule(
        self,
        project_id: str,
    ) -> tuple[ControlledVersionRow, ManualChangePolicyRule]:
        policy_row = self._bound_version(
            project_id,
            purpose="manual_change_policy",
            kind="manual_change_policy",
        )
        raw_policy = policy_row.payload.get("policy")
        if not isinstance(raw_policy, dict):
            raise ValueError("Approved manual-change policy payload is missing 'policy'")
        policy = ManualChangePolicy.model_validate(raw_policy)
        rule = next(
            (
                item
                for item in policy.rules
                if item.entity_type == "quantity" and item.field_name == "record"
            ),
            None,
        )
        if rule is None:
            raise ValueError("Manual-change policy has no exact quantity record rule")
        return policy_row, rule

    @staticmethod
    def _quantity_state_from_submission(
        submission: QuantitySubmission,
    ) -> dict[str, Any]:
        draft = submission.draft.model_dump(
            mode="json",
            exclude={"manual_change_id"},
        )
        draft["source_observation_ids"] = sorted(submission.draft.source_observation_ids)
        draft["alternative_quantity_ids"] = sorted(submission.draft.alternative_quantity_ids)
        return {
            "draft": draft,
            "formula": (
                submission.formula.model_dump(mode="json")
                if submission.formula is not None
                else None
            ),
            "formula_input_observation_ids": {
                key: submission.formula_input_observation_ids[key]
                for key in sorted(submission.formula_input_observation_ids)
            },
        }

    @staticmethod
    def _quantity_state_from_row(row: QuantityRow) -> dict[str, Any]:
        raw_record = row.payload.get("record")
        if not isinstance(raw_record, dict):
            raise ValueError("Current quantity lacks a governed record payload")
        record = QuantityRecord.model_validate(raw_record)
        raw_formula = row.payload.get("formula")
        formula = (
            QuantityFormulaDefinition.model_validate(raw_formula)
            if raw_formula is not None
            else None
        )
        raw_formula_evidence = row.payload.get("formula_input_observation_ids")
        if not isinstance(raw_formula_evidence, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in raw_formula_evidence.items()
        ):
            raise ValueError("Current quantity formula evidence payload is invalid")
        return {
            "draft": {
                "value": str(record.value),
                "unit": record.unit,
                "source_observation_ids": sorted(record.source_observation_ids),
                "source_priority": record.source_priority,
                "rounding_scale": record.rounding_scale,
                "waste_factor": str(record.waste_factor),
                "alternative_quantity_ids": sorted(record.alternative_quantity_ids),
            },
            "formula": (formula.model_dump(mode="json") if formula is not None else None),
            "formula_input_observation_ids": {
                key: raw_formula_evidence[key] for key in sorted(raw_formula_evidence)
            },
        }

    def _validated_quantity_manual_change(
        self,
        *,
        actor: Actor,
        project_id: str,
        document_set_revision_id: str | None,
        line: BoqLineRow,
        previous: QuantityRow,
        submission: QuantitySubmission,
    ) -> tuple[ManualChangeRow, str | None]:
        change_id = submission.draft.manual_change_id
        if change_id is None:
            raise ValueError("Every quantity revision requires a registered manual change")
        change = self.session.scalar(
            select(ManualChangeRow)
            .where(
                ManualChangeRow.id == change_id,
                ManualChangeRow.project_id == project_id,
            )
            .with_for_update()
        )
        if change is None:
            raise ValueError("Quantity manual change does not exist in this project")
        if (
            change.entity_type != "quantity"
            or change.entity_id != line.id
            or change.field_name != "record"
            or change.changed_by != actor.actor_id
            or not change.reason.strip()
        ):
            raise ValueError("Quantity manual change identity does not match the revision")
        if change.payload.get("lifecycle_version") != "quantity-manual-change-v1":
            raise ValueError("Quantity manual change lifecycle is unsupported")
        application = self.session.scalar(
            select(QuantityManualChangeApplicationRow).where(
                QuantityManualChangeApplicationRow.manual_change_id == change.id
            )
        )
        if application is not None:
            raise ValueError("Quantity manual change has already been applied")
        policy_row, rule = self._manual_change_rule(project_id)
        if (
            change.critical != rule.critical
            or change.payload.get("policy_version_id") != policy_row.id
            or change.payload.get("document_set_revision_id") != document_set_revision_id
            or change.payload.get("previous_quantity_id") != previous.id
        ):
            raise ValueError("Quantity manual change no longer matches current controlled context")
        before = self._quantity_state_from_row(previous)
        after = self._quantity_state_from_submission(submission)
        if (
            change.payload.get("before") != before
            or change.payload.get("before_hash") != content_hash(before)
            or change.payload.get("after") != after
            or change.payload.get("after_hash") != content_hash(after)
            or change.payload.get("source_observation_ids")
            != sorted(submission.draft.source_observation_ids)
        ):
            raise ValueError("Quantity manual change does not reproduce the exact revision")
        approval_task_id = change.payload.get("approval_task_id")
        if not rule.critical:
            if approval_task_id is not None:
                raise ValueError("Non-critical quantity change has an unexpected approval task")
            return change, None
        if not isinstance(approval_task_id, str) or rule.assigned_role is None:
            raise ValueError("Critical quantity change lacks its approval task identity")
        task = self.session.scalar(
            select(ApprovalTaskRow)
            .where(
                ApprovalTaskRow.id == approval_task_id,
                ApprovalTaskRow.project_id == project_id,
                ApprovalTaskRow.entity_type == "manual_change",
                ApprovalTaskRow.entity_id == change.id,
                ApprovalTaskRow.task_type == "MANUAL_CHANGE",
            )
            .with_for_update()
        )
        if (
            task is None
            or not task.required
            or task.assigned_role != rule.assigned_role.value
            or task.status != "APPROVED"
            or task.payload.get("created_by") != change.changed_by
            or task.payload.get("policy_version_id") != policy_row.id
            or task.payload.get("before_hash") != change.payload.get("before_hash")
            or task.payload.get("after_hash") != change.payload.get("after_hash")
            or task.payload.get("observation_ids") != change.payload.get("source_observation_ids")
        ):
            raise ValueError("Critical quantity change approval task is incomplete")
        approval = self.session.scalar(
            select(ApprovalRecordRow).where(
                ApprovalRecordRow.task_id == task.id,
                ApprovalRecordRow.decision == "APPROVED",
            )
        )
        raw_approval_evidence_ids = (
            approval.payload.get("evidence_ids", []) if approval is not None else []
        )
        expected_evidence_ids = change.payload.get("source_observation_ids", [])
        if (
            approval is None
            or approval.decided_by == change.changed_by
            or approval.payload.get("related_change_ids") != [change.id]
            or not isinstance(expected_evidence_ids, list)
            or not all(isinstance(value, str) for value in expected_evidence_ids)
            or len(expected_evidence_ids) != len(set(expected_evidence_ids))
            or not isinstance(raw_approval_evidence_ids, list)
            or not all(isinstance(value, str) for value in raw_approval_evidence_ids)
            or len(raw_approval_evidence_ids) != len(set(raw_approval_evidence_ids))
            or sorted(raw_approval_evidence_ids) != sorted(expected_evidence_ids)
        ):
            raise ValueError("Critical quantity change approval evidence is incomplete")
        approval_evidence_ids = tuple(raw_approval_evidence_ids)
        self._verified_observations(project_id, approval_evidence_ids)
        return change, approval.id

    def _quantity_manual_change_view(
        self,
        change: ManualChangeRow,
    ) -> QuantityManualChangeView:
        if change.payload.get("lifecycle_version") != "quantity-manual-change-v1":
            raise ValueError("Quantity manual change lifecycle is unsupported")
        approval_task_id = change.payload.get("approval_task_id")
        task = None
        if isinstance(approval_task_id, str):
            task = self.session.scalar(
                select(ApprovalTaskRow).where(
                    ApprovalTaskRow.id == approval_task_id,
                    ApprovalTaskRow.project_id == change.project_id,
                    ApprovalTaskRow.task_type == "MANUAL_CHANGE",
                    ApprovalTaskRow.entity_type == "manual_change",
                    ApprovalTaskRow.entity_id == change.id,
                )
            )
        application = self.session.scalar(
            select(QuantityManualChangeApplicationRow).where(
                QuantityManualChangeApplicationRow.manual_change_id == change.id,
                QuantityManualChangeApplicationRow.project_id == change.project_id,
            )
        )
        before = change.payload.get("before")
        after = change.payload.get("after")
        policy_version_id = change.payload.get("policy_version_id")
        document_set_revision_id = change.payload.get("document_set_revision_id")
        previous_quantity_id = change.payload.get("previous_quantity_id")
        source_observation_ids = change.payload.get("source_observation_ids")
        after_draft = after.get("draft") if isinstance(after, dict) else None
        if (
            not isinstance(before, dict)
            or not isinstance(after, dict)
            or not isinstance(policy_version_id, str)
            or not isinstance(document_set_revision_id, str)
            or not isinstance(previous_quantity_id, str)
            or not isinstance(source_observation_ids, list)
            or not source_observation_ids
            or not all(isinstance(item, str) for item in source_observation_ids)
            or len(source_observation_ids) != len(set(source_observation_ids))
            or not isinstance(after_draft, dict)
            or after_draft.get("source_observation_ids") != source_observation_ids
            or change.payload.get("before_hash") != content_hash(before)
            or change.payload.get("after_hash") != content_hash(after)
        ):
            raise ValueError("Quantity manual change payload is incomplete")
        policy = self.session.get(ControlledVersionRow, policy_version_id)
        policy_rule: ManualChangePolicyRule | None = None
        project = self.session.get(ProjectRow, change.project_id)
        if policy is not None and project is not None:
            try:
                require_controlled_version_integrity(
                    session=self.session,
                    settings=self.settings,
                    row=policy,
                    expected_organization_id=project.organization_id,
                    expected_kind="manual_change_policy",
                )
            except (ArithmeticError, KeyError, LookupError, TypeError, ValueError):
                policy = None
        if policy is not None:
            raw_policy = policy.payload.get("policy")
            if isinstance(raw_policy, dict):
                try:
                    parsed_policy = ManualChangePolicy.model_validate(raw_policy)
                except ValueError:
                    parsed_policy = None
                if parsed_policy is not None:
                    policy_rule = next(
                        (
                            item
                            for item in parsed_policy.rules
                            if item.entity_type == "quantity" and item.field_name == "record"
                        ),
                        None,
                    )
        integrity_ok = (
            change.critical
            and isinstance(approval_task_id, str)
            and task is not None
            and task.required
            and policy_rule is not None
            and policy_rule.critical
            and policy_rule.assigned_role is not None
            and task.assigned_role == policy_rule.assigned_role.value
            and task.payload.get("created_by") == change.changed_by
            and task.payload.get("policy_version_id") == policy_version_id
            and task.payload.get("before_hash") == change.payload.get("before_hash")
            and task.payload.get("after_hash") == change.payload.get("after_hash")
            and task.payload.get("observation_ids") == source_observation_ids
        ) or (
            not change.critical
            and approval_task_id is None
            and policy_rule is not None
            and not policy_rule.critical
            and policy_rule.assigned_role is None
        )
        approval: ApprovalRecordRow | None = None
        if task is not None and task.status == "APPROVED":
            approval = self.session.scalar(
                select(ApprovalRecordRow).where(
                    ApprovalRecordRow.task_id == task.id,
                    ApprovalRecordRow.decision == "APPROVED",
                )
            )
            raw_approval_evidence_ids = (
                approval.payload.get("evidence_ids") if approval is not None else None
            )
            approval_evidence_ids = (
                tuple(raw_approval_evidence_ids)
                if isinstance(raw_approval_evidence_ids, list)
                and all(isinstance(item, str) for item in raw_approval_evidence_ids)
                else None
            )
            integrity_ok = (
                integrity_ok
                and approval is not None
                and approval.decided_by != change.changed_by
                and approval.payload.get("related_change_ids") == [change.id]
                and approval_evidence_ids is not None
                and len(approval_evidence_ids) == len(set(approval_evidence_ids))
                and sorted(approval_evidence_ids) == sorted(source_observation_ids)
            )
            if integrity_ok and approval_evidence_ids is not None:
                try:
                    self._verified_observations(
                        change.project_id,
                        approval_evidence_ids,
                    )
                except ValueError:
                    integrity_ok = False
        if application is not None:
            applied_quantity = self.session.get(QuantityRow, application.quantity_id)
            applied_record = (
                applied_quantity.payload.get("record")
                if applied_quantity is not None
                and applied_quantity.boq_line_id == change.entity_id
                and applied_quantity.supersedes_quantity_id == previous_quantity_id
                else None
            )
            try:
                applied_state = (
                    self._quantity_state_from_row(applied_quantity)
                    if applied_quantity is not None
                    else None
                )
            except ValueError:
                applied_state = None
            integrity_ok = (
                integrity_ok
                and application.applied_by == change.changed_by
                and application.payload.get("manual_change_hash") == content_hash(change.payload)
                and application.payload.get("before_hash") == change.payload.get("before_hash")
                and application.payload.get("after_hash") == change.payload.get("after_hash")
                and application.payload.get("policy_version_id") == policy_version_id
                and isinstance(applied_record, dict)
                and applied_record.get("manual_change_id") == change.id
                and applied_state == after
                and (
                    application.payload.get("approval_id") == approval.id
                    if change.critical and approval is not None
                    else application.payload.get("approval_id") is None
                )
            )
        if not integrity_ok:
            status = "BLOCKED_INTEGRITY"
        elif application is not None:
            status = "APPLIED"
        elif change.critical:
            if task is None:
                raise ValueError("Critical quantity change approval task is missing")
            status = "PENDING_APPROVAL" if task.status == "PENDING" else task.status
        else:
            status = "APPROVED_BY_POLICY"
        changed_at = ensure_utc(change.changed_at)
        task_updated_at = ensure_utc(task.updated_at) if task is not None else None
        applied_at = ensure_utc(application.applied_at) if application is not None else None
        if changed_at is None:
            raise ValueError("Quantity manual change timestamp is missing")
        return QuantityManualChangeView(
            change_id=change.id,
            project_id=change.project_id,
            line_id=change.entity_id,
            previous_quantity_id=previous_quantity_id,
            critical=change.critical,
            changed_by=change.changed_by,
            reason=change.reason,
            changed_at=changed_at,
            policy_version_id=policy_version_id,
            document_set_revision_id=document_set_revision_id,
            before=before,
            after=after,
            approval_task_id=(approval_task_id if isinstance(approval_task_id, str) else None),
            approval_task_status=task.status if task is not None else None,
            approval_task_updated_at=task_updated_at,
            status=status,
            applied_quantity_id=application.quantity_id if application is not None else None,
            applied_by=application.applied_by if application is not None else None,
            applied_at=applied_at,
        )

    def _spreadsheet_source_item_for_line(
        self,
        *,
        project_id: str,
        line: BoqLineRow,
        document_revision_ids: frozenset[str],
    ) -> str | None:
        raw_ids = line.payload.get("evidence_observation_ids")
        if not (
            isinstance(raw_ids, list)
            and raw_ids
            and all(isinstance(item, str) for item in raw_ids)
            and len(raw_ids) == len(set(raw_ids))
        ):
            raise RuntimeError("BoQ line evidence identity is invalid")
        rows = self._verified_observations(
            project_id,
            tuple(raw_ids),
            document_revision_ids=document_revision_ids,
        )
        source_item_ids: set[str] = set()
        for row in rows.values():
            observation = self._validated_observation(row)
            value = observation.value
            if not isinstance(value, dict) or value.get("schema_version") != (
                BOQ_LINE_MAPPING_SCHEMA
            ):
                continue
            source_item_id = value.get("source_item_id")
            if not isinstance(source_item_id, str) or not source_item_id:
                raise RuntimeError("Verified BoQ mapping lacks its source item identity")
            source_item_ids.add(source_item_id)
        if len(source_item_ids) > 1:
            raise RuntimeError("BoQ line is linked to multiple spreadsheet source items")
        return next(iter(source_item_ids), None)

    def _validated_quantity_evidence_for_source(
        self,
        *,
        row: ObservationRow,
        source_item_id: str,
        organization_id: str,
    ) -> tuple[Observation, ImportedBoqRowValue]:
        observation = self._validated_observation(row)
        upstream = row.payload.get("upstream_observations")
        if not (
            row.field_name == f"boq_quantity:{source_item_id}"
            and observation.field_name == row.field_name
            and isinstance(upstream, list)
            and len(upstream) == 1
            and isinstance(upstream[0], dict)
            and isinstance(upstream[0].get("observation_id"), str)
        ):
            raise RuntimeError("Verified spreadsheet quantity lineage is invalid")
        source = self.session.get(ObservationRow, upstream[0]["observation_id"])
        if source is None or source.project_id != row.project_id:
            raise RuntimeError("Verified spreadsheet quantity source is unavailable")
        _source_observation, source_value = self._validated_spreadsheet_candidate(
            row=source,
            organization_id=organization_id,
        )
        try:
            quantity = Decimal(str(observation.value))
        except (InvalidOperation, TypeError, ValueError) as error:
            raise RuntimeError("Verified spreadsheet quantity is not an exact decimal") from error
        if (
            source_value.source_item_id != source_item_id
            or not quantity.is_finite()
            or quantity != source_value.quantity
            or observation.unit != source_value.unit
        ):
            raise RuntimeError("Verified spreadsheet quantity differs from its imported source")
        return observation, source_value

    def _validated_spreadsheet_candidate(
        self,
        *,
        row: ObservationRow,
        organization_id: str,
    ) -> tuple[Observation, ImportedBoqRowValue]:
        observation = Observation.model_validate(row.payload.get("observation"))
        value = ImportedBoqRowValue.model_validate(observation.value)
        if (
            row.id != observation.observation_id
            or row.document_revision_id != observation.location.document_revision_id
            or row.field_name != observation.field_name
            or row.field_name != f"boq_row_candidate:{value.source_item_id}"
            or row.method != EvidenceMethod.TABLE_PARSER.value
            or observation.method is not EvidenceMethod.TABLE_PARSER
            or row.method_version != observation.method_version
            or row.status != VerificationStatus.UNVERIFIED.value
            or observation.status is not VerificationStatus.UNVERIFIED
            or observation.unit != value.unit
        ):
            raise RuntimeError("Imported BoQ row identity does not reproduce")
        qualification_id = row.payload.get("adapter_qualification_id")
        if not isinstance(qualification_id, str) or not qualification_id:
            raise RuntimeError("Imported BoQ row lacks adapter qualification lineage")
        qualification = self.session.get(AdapterQualificationRow, qualification_id)
        if (
            qualification is None
            or qualification.status != "APPROVED"
            or qualification.adapter_version != row.method_version
            or qualification.payload.get("organization_id") != organization_id
            or qualification.payload.get("service_actor_id") != observation.actor_id
            or EvidenceMethod.TABLE_PARSER.value
            not in qualification.payload.get("supported_methods", [])
            or (
                qualification.valid_until is not None
                and qualification.valid_until < utc_now().date()
            )
        ):
            raise ValueError("Imported BoQ row no longer matches its qualified adapter")
        extraction_runs = list(
            self.session.scalars(
                select(ExtractionRunRow).where(
                    ExtractionRunRow.project_id == row.project_id,
                    ExtractionRunRow.document_revision_id == row.document_revision_id,
                    ExtractionRunRow.adapter_qualification_id == qualification.id,
                    ExtractionRunRow.method == EvidenceMethod.TABLE_PARSER.value,
                    ExtractionRunRow.status == "COMPLETED",
                )
            )
        )
        profile_row = self._bound_version(
            row.project_id,
            purpose="boq_xlsx_profile",
            kind="boq_xlsx_profile",
            expected_version_id=value.workbook_profile_version_id,
        )
        profile = BoqXlsxProfile.model_validate(profile_row.payload.get("profile"))
        if content_hash(profile) != value.workbook_profile_content_hash:
            raise RuntimeError("Imported BoQ row profile content does not reproduce")
        supporting_runs = [
            run
            for run in extraction_runs
            if isinstance(run.payload.get("observation_ids"), list)
            and row.id in run.payload["observation_ids"]
            and run.payload.get("workbook_object_sha256") == value.workbook_object_sha256
            and run.payload.get("profile_version_id") == value.workbook_profile_version_id
            and run.payload.get("profile_content_hash") == profile_row.content_hash
        ]
        if len(supporting_runs) != 1:
            raise RuntimeError("Imported BoQ row lacks one exact completed extraction run")
        run = supporting_runs[0]
        observation_ids = run.payload["observation_ids"]
        if len(observation_ids) != len(set(observation_ids)) or run.payload.get("row_count") != len(
            observation_ids
        ):
            raise RuntimeError("Imported BoQ extraction run identity does not reproduce")
        return observation, value

    def _latest_mapping_proposal(
        self,
        *,
        project_id: str,
        document_revision_ids: tuple[str, ...],
        source_observation_id: str,
    ) -> tuple[ObservationRow | None, ApprovalTaskRow | None, str | None]:
        proposal_rows = list(
            self.session.scalars(
                select(ObservationRow)
                .where(
                    ObservationRow.project_id == project_id,
                    ObservationRow.document_revision_id.in_(document_revision_ids),
                    ObservationRow.field_name == "boq_line",
                    ObservationRow.method == EvidenceMethod.MANUAL.value,
                    ObservationRow.status == VerificationStatus.UNVERIFIED.value,
                )
                .order_by(ObservationRow.created_at.desc(), ObservationRow.id.desc())
            )
        )
        matching: list[ObservationRow] = []
        for proposal in proposal_rows:
            upstream = proposal.payload.get("upstream_observations")
            if not (
                isinstance(upstream, list)
                and len(upstream) == 1
                and isinstance(upstream[0], dict)
                and upstream[0].get("observation_id") == source_observation_id
            ):
                continue
            observation = Observation.model_validate(proposal.payload.get("observation"))
            value = observation.value
            if (
                proposal.id != observation.observation_id
                or proposal.document_revision_id != observation.location.document_revision_id
                or proposal.field_name != observation.field_name
                or proposal.method != observation.method.value
                or proposal.method_version != observation.method_version
                or proposal.status != observation.status.value
                or observation.method is not EvidenceMethod.MANUAL
                or observation.status is not VerificationStatus.UNVERIFIED
                or not isinstance(value, dict)
                or value.get("schema_version") != BOQ_LINE_MAPPING_SCHEMA
                or value.get("mapping_scope") != "IDENTITY_DESCRIPTION_UNIT_ONLY"
                or value.get("quantity_promoted") is not False
                or not isinstance(value.get("work_code"), str)
                or not isinstance(value.get("description"), str)
                or not isinstance(value.get("unit"), str)
                or observation.unit != value.get("unit")
            ):
                raise RuntimeError("BoQ spreadsheet mapping proposal identity does not reproduce")
            matching.append(proposal)
        return self._reviewed_candidate_proposal(
            project_id=project_id,
            matching=matching,
            proposal_kind="mapping",
        )

    def _latest_quantity_proposal(
        self,
        *,
        project_id: str,
        document_revision_ids: tuple[str, ...],
        source_observation_id: str,
        source_item_id: str,
        expected_quantity: Decimal,
        expected_unit: str,
    ) -> tuple[ObservationRow | None, ApprovalTaskRow | None, str | None]:
        field_name = f"boq_quantity:{source_item_id}"
        proposal_rows = list(
            self.session.scalars(
                select(ObservationRow)
                .where(
                    ObservationRow.project_id == project_id,
                    ObservationRow.document_revision_id.in_(document_revision_ids),
                    ObservationRow.field_name == field_name,
                    ObservationRow.method == EvidenceMethod.MANUAL.value,
                    ObservationRow.status == VerificationStatus.UNVERIFIED.value,
                )
                .order_by(ObservationRow.created_at.desc(), ObservationRow.id.desc())
            )
        )
        matching: list[ObservationRow] = []
        for proposal in proposal_rows:
            upstream = proposal.payload.get("upstream_observations")
            if not (
                isinstance(upstream, list)
                and len(upstream) == 1
                and isinstance(upstream[0], dict)
                and upstream[0].get("observation_id") == source_observation_id
            ):
                continue
            observation = Observation.model_validate(proposal.payload.get("observation"))
            try:
                quantity = Decimal(str(observation.value))
            except (InvalidOperation, TypeError, ValueError) as error:
                raise RuntimeError(
                    "BoQ spreadsheet quantity proposal is not an exact decimal"
                ) from error
            if (
                proposal.id != observation.observation_id
                or proposal.document_revision_id != observation.location.document_revision_id
                or proposal.field_name != observation.field_name
                or proposal.method != observation.method.value
                or proposal.method_version != observation.method_version
                or proposal.status != observation.status.value
                or observation.method is not EvidenceMethod.MANUAL
                or observation.status is not VerificationStatus.UNVERIFIED
                or not quantity.is_finite()
                or quantity != expected_quantity
                or observation.unit != expected_unit
            ):
                raise RuntimeError("BoQ spreadsheet quantity proposal identity does not reproduce")
            matching.append(proposal)
        return self._reviewed_candidate_proposal(
            project_id=project_id,
            matching=matching,
            proposal_kind="quantity",
        )

    def _reviewed_candidate_proposal(
        self,
        *,
        project_id: str,
        matching: list[ObservationRow],
        proposal_kind: str,
    ) -> tuple[ObservationRow | None, ApprovalTaskRow | None, str | None]:
        if not matching:
            return None, None, None
        tasks = list(
            self.session.scalars(
                select(ApprovalTaskRow).where(
                    ApprovalTaskRow.project_id == project_id,
                    ApprovalTaskRow.task_type == "MANUAL_EVIDENCE_REVIEW",
                    ApprovalTaskRow.entity_type == "evidence_observation",
                    ApprovalTaskRow.entity_id.in_(tuple(row.id for row in matching)),
                )
            )
        )
        tasks_by_proposal = {task.entity_id: task for task in tasks}
        if len(tasks_by_proposal) != len(matching):
            raise RuntimeError(f"BoQ spreadsheet {proposal_kind} proposal lacks its review task")
        active = [
            row for row in matching if tasks_by_proposal[row.id].status in {"PENDING", "APPROVED"}
        ]
        if len(active) > 1:
            raise RuntimeError(f"Imported BoQ row has multiple active {proposal_kind} proposals")
        selected = active[0] if active else matching[0]
        task = tasks_by_proposal[selected.id]
        verified_observation_id: str | None = None
        decisions = list(
            self.session.scalars(
                select(ApprovalRecordRow).where(ApprovalRecordRow.task_id == task.id)
            )
        )
        if task.status == "PENDING" and decisions:
            raise RuntimeError(f"Pending BoQ {proposal_kind} task already has a decision record")
        if task.status != "PENDING":
            if len(decisions) != 1 or decisions[0].decision != task.status:
                raise RuntimeError(f"BoQ {proposal_kind} task decision does not reproduce")
            candidate = decisions[0].payload.get("verified_observation_id")
            if task.status == "APPROVED":
                if not isinstance(candidate, str) or not candidate:
                    raise RuntimeError(f"Approved BoQ {proposal_kind} lacks verified evidence")
                verified = self.session.get(ObservationRow, candidate)
                if (
                    verified is None
                    or verified.project_id != project_id
                    or verified.status != VerificationStatus.VERIFIED.value
                    or verified.field_name != selected.field_name
                ):
                    raise RuntimeError(f"Approved BoQ {proposal_kind} evidence is unavailable")
                EvidenceService(
                    session=self.session,
                    settings=self.settings,
                    object_store=self.object_store,
                ).require_verified_manual_derivation(
                    project_id=project_id,
                    observation_id=candidate,
                )
                verified_observation_id = candidate
            elif candidate is not None:
                raise RuntimeError(
                    f"Rejected BoQ {proposal_kind} unexpectedly produced verified evidence"
                )
        return selected, task, verified_observation_id

    def _bound_version(
        self,
        project_id: str,
        *,
        purpose: str,
        kind: str,
        expected_version_id: str | None = None,
    ) -> ControlledVersionRow:
        project = self.session.get(ProjectRow, project_id)
        if project is None:
            raise LookupError(project_id)
        return require_bound_controlled_version(
            session=self.session,
            settings=self.settings,
            project_id=project_id,
            organization_id=project.organization_id,
            purpose=purpose,
            kind=kind,
            expected_version_id=expected_version_id,
        )

    def _verified_observations(
        self,
        project_id: str,
        observation_ids: tuple[str, ...],
        *,
        document_revision_ids: frozenset[str] | None = None,
    ) -> dict[str, ObservationRow]:
        rows = list(
            self.session.scalars(
                select(ObservationRow).where(
                    ObservationRow.project_id == project_id,
                    ObservationRow.id.in_(observation_ids),
                    ObservationRow.status == VerificationStatus.VERIFIED.value,
                )
            )
        )
        if len(rows) != len(set(observation_ids)):
            raise ValueError("One or more evidence observations are absent or unverified")
        for row in rows:
            self._validated_observation(row)
            if document_revision_ids is not None:
                require_observation_in_document_set(
                    document_revision_ids=document_revision_ids,
                    document_revision_id=row.document_revision_id,
                )
        return {row.id: row for row in rows}

    def _validated_observation(self, row: ObservationRow) -> Observation:
        observation = Observation.model_validate(row.payload.get("observation"))
        if (
            row.id != observation.observation_id
            or row.document_revision_id != observation.location.document_revision_id
            or row.field_name != observation.field_name
            or row.method != observation.method.value
            or row.method_version != observation.method_version
            or row.status != observation.status.value
            or observation.status is not VerificationStatus.VERIFIED
        ):
            raise RuntimeError("Verified BoQ evidence identity does not reproduce")
        if row.payload.get("derivation_type") == "MANUAL_EVIDENCE_REVIEW":
            return EvidenceService(
                session=self.session,
                settings=self.settings,
                object_store=self.object_store,
            ).require_verified_manual_derivation(
                project_id=row.project_id,
                observation_id=row.id,
            )
        return observation

    def _observation_supports_line(
        self,
        observation: ObservationRow,
        line: BoqLineRow,
    ) -> bool:
        raw = self._validated_observation(observation).value
        return (
            isinstance(raw, dict)
            and raw.get("work_code") == line.work_code
            and raw.get("unit") == line.unit
            and ("description" not in raw or raw.get("description") == line.description)
        )

    def _validate_quantity_evidence(
        self,
        submission: QuantitySubmission,
        observations: dict[str, ObservationRow],
    ) -> None:
        if submission.formula is None:
            if submission.formula_input_observation_ids:
                raise ValueError("Direct quantity cannot contain formula input evidence")
            matches = [
                row
                for row in observations.values()
                if self._decimal_observation_value(row) == submission.draft.value
                and row.payload.get("observation", {}).get("unit") == submission.draft.unit
            ]
            if not matches:
                raise ValueError("No verified observation reproduces the direct quantity")
            return
        expected_inputs = set(submission.formula.inputs)
        if set(submission.formula_input_observation_ids) != expected_inputs:
            raise ValueError("Every formula input requires exactly one evidence observation")
        for input_name, expected_value in submission.formula.inputs.items():
            observation_id = submission.formula_input_observation_ids[input_name]
            observation = observations.get(observation_id)
            if observation is None:
                raise ValueError(f"Formula input evidence is missing: {input_name}")
            if self._decimal_observation_value(observation) != expected_value:
                raise ValueError(f"Formula input evidence differs: {input_name}")

    @staticmethod
    def _decimal_observation_value(observation: ObservationRow) -> Decimal | None:
        raw = observation.payload.get("observation", {}).get("value")
        try:
            return Decimal(str(raw))
        except (InvalidOperation, TypeError, ValueError):
            return None

    def _require_independent_coverage(self, observations: Any) -> None:
        require_distinct_qualified_independence(
            self.session,
            project_id=next(iter(observations)).project_id,
            observations=observations,
        )

    def _verified_project_tags(
        self,
        project_id: str,
        rules: tuple[ScopeRule, ...],
    ) -> tuple[frozenset[str], tuple[ValidationFinding, ...]]:
        tags_required = any(rule.required_project_tags for rule in rules)
        row = self.session.scalar(
            select(ProjectPassportFactRow).where(
                ProjectPassportFactRow.project_id == project_id,
                ProjectPassportFactRow.field_name == "project_tags",
                ProjectPassportFactRow.status == VerificationStatus.VERIFIED.value,
                ProjectPassportFactRow.is_current.is_(True),
            )
        )
        if row is None:
            if not tags_required:
                return frozenset(), ()
            finding = ValidationFinding(
                code=FindingCode.PROJECT_PASSPORT_INCOMPLETE,
                severity=Severity.BLOCKER,
                message="Scope applicability requires verified project tags",
                entity_ids=("project_tags",),
            )
            return frozenset(), (finding,)
        value = row.payload.get("value")
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            finding = ValidationFinding(
                code=FindingCode.PROJECT_PASSPORT_INCOMPLETE,
                severity=Severity.BLOCKER,
                message="Verified project_tags fact has an invalid value",
                entity_ids=(row.id,),
            )
            return frozenset(), (finding,)
        return frozenset(value), ()

    def _persist_finding(
        self,
        project_id: str,
        contour: str,
        finding: ValidationFinding,
        now: Any,
    ) -> None:
        identity = {"project_id": project_id, "contour": contour, "finding": finding}
        finding_id = f"finding-{content_hash(identity)[:24]}"
        if self.session.get(VerificationFindingRow, finding_id) is not None:
            return
        self.session.add(
            VerificationFindingRow(
                id=finding_id,
                project_id=project_id,
                contour=contour,
                code=finding.code.value,
                severity=finding.severity.value,
                resolved=False,
                payload=finding.model_dump(mode="json"),
                created_at=now,
                updated_at=now,
            )
        )

    def _project_service(self) -> ProjectService:
        return ProjectService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        )

    @staticmethod
    def _line_view(row: BoqLineRow) -> BoqLineView:
        updated_at = ensure_utc(row.updated_at)
        if updated_at is None:
            raise RuntimeError("BoQ line update timestamp is missing")
        return BoqLineView(
            line_id=row.id,
            line_key=row.line_key,
            wbs_node_id=row.wbs_node_id,
            work_code=row.work_code,
            description=row.description,
            unit=row.unit,
            status=VerificationStatus(row.status),
            critical_quantity=bool(row.payload.get("critical_quantity")),
            cost_components=tuple(
                CostComponentDraft.model_validate(item)
                for item in row.payload.get("cost_components", [])
            ),
            supersedes_line_id=row.supersedes_line_id,
            is_current=row.is_current,
            updated_at=updated_at,
        )
