from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from pydantic import Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from tenderguard.application.evidence_independence import (
    require_distinct_qualified_independence,
)
from tenderguard.application.projects import ProjectService
from tenderguard.application.stage_gates import scope_input_signature
from tenderguard.config import Settings
from tenderguard.domain.common import content_hash, utc_now
from tenderguard.domain.enums import (
    ActorRole,
    ApprovalState,
    CostBasisKind,
    CostCategory,
    FindingCode,
    Severity,
    VerificationStatus,
    VersionStatus,
)
from tenderguard.domain.models import (
    DomainModel,
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
    BoqLineRow,
    ControlledVersionRow,
    ManualChangeRow,
    ObservationRow,
    ProjectControlledVersionRow,
    ProjectPassportFactRow,
    QuantityRow,
    ScopeEvaluationRow,
    ScopeFindingRow,
    VerificationFindingRow,
)


class CostComponentDraft(DomainModel):
    semantic_key: str = Field(min_length=1, max_length=200)
    category: CostCategory
    basis_kind: CostBasisKind


class BoqLineDraft(DomainModel):
    line_key: str = Field(min_length=1, max_length=128)
    wbs_node_id: str
    work_code: str
    description: str
    unit: str
    evidence_observation_ids: tuple[str, ...] = Field(min_length=1)
    cost_components: tuple[CostComponentDraft, ...] = Field(min_length=1)
    critical_quantity: bool = False

    @model_validator(mode="after")
    def component_keys_are_unique(self) -> BoqLineDraft:
        keys = [item.semantic_key for item in self.cost_components]
        if len(keys) != len(set(keys)):
            raise ValueError("BoQ line cost component semantic keys must be unique")
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

    def create_line(
        self,
        *,
        actor: Actor,
        project_id: str,
        draft: BoqLineDraft,
        request_id: str,
        reason: str,
    ) -> BoqLineView:
        project_service = self._project_service()
        project = project_service.get_project(
            actor=actor,
            project_id=project_id,
            lock=True,
            required_roles=(ActorRole.ESTIMATOR, ActorRole.TECHNICAL_EXPERT),
        )
        if ApprovalState(project.state) is not ApprovalState.BOQ_IN_PROGRESS:
            raise ValueError("BoQ lines may be created only in BOQ_IN_PROGRESS")
        self._verified_observations(project_id, draft.evidence_observation_ids)
        identity = {
            "project_id": project_id,
            "document_set_revision_id": project.current_document_set_revision_id,
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
                "document_set_revision_id": project.current_document_set_revision_id,
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
        request_id: str,
        reason: str,
    ) -> BoqLineView:
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
        if row.status == VerificationStatus.VERIFIED.value:
            return self._line_view(row)
        if row.payload.get("document_set_revision_id") != project.current_document_set_revision_id:
            raise ValueError("BoQ line belongs to a superseded document-set revision")
        if row.payload.get("created_by") == actor.actor_id:
            raise ValueError("BoQ line requires independent four-eyes verification")
        observations = self._verified_observations(
            project_id,
            tuple(row.payload.get("evidence_observation_ids", [])),
        )
        if not any(self._observation_supports_line(item, row) for item in observations.values()):
            raise ValueError("No verified evidence reproduces the BoQ work code and unit")
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
        line = self.session.scalar(
            select(BoqLineRow)
            .where(
                BoqLineRow.id == line_id,
                BoqLineRow.project_id == project_id,
                BoqLineRow.is_current.is_(True),
            )
            .with_for_update()
        )
        if line is None:
            raise LookupError(line_id)
        if line.status != VerificationStatus.VERIFIED.value:
            raise ValueError("Quantity cannot be attached to an unverified BoQ line")
        if submission.draft.unit != line.unit:
            raise ValueError("Quantity unit differs from the verified BoQ line unit")
        observations = self._verified_observations(
            project_id,
            submission.draft.source_observation_ids,
        )
        if line.payload.get("critical_quantity"):
            self._require_independent_coverage(observations.values())
        self._validate_quantity_evidence(submission, observations)
        if submission.draft.manual_change_id:
            change = self.session.scalar(
                select(ManualChangeRow).where(
                    ManualChangeRow.id == submission.draft.manual_change_id,
                    ManualChangeRow.project_id == project_id,
                )
            )
            if change is None:
                raise ValueError("Quantity manual change does not exist in this project")
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

        quantity_id = f"quantity-{uuid4()}"
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
        validation = validate_quantity(
            candidate,
            formula=submission.formula,
            policy=policy,
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
        self.session.add(
            QuantityRow(
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

    def _bound_version(
        self,
        project_id: str,
        *,
        purpose: str,
        kind: str,
    ) -> ControlledVersionRow:
        row = self.session.scalar(
            select(ControlledVersionRow)
            .join(
                ProjectControlledVersionRow,
                ProjectControlledVersionRow.controlled_version_id == ControlledVersionRow.id,
            )
            .where(
                ProjectControlledVersionRow.project_id == project_id,
                ProjectControlledVersionRow.purpose == purpose,
                ControlledVersionRow.kind == kind,
                ControlledVersionRow.status == VersionStatus.APPROVED.value,
            )
        )
        if row is None:
            raise ValueError(f"A bound approved {kind} version is required")
        return row

    def _verified_observations(
        self,
        project_id: str,
        observation_ids: tuple[str, ...],
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
        return {row.id: row for row in rows}

    @staticmethod
    def _observation_supports_line(observation: ObservationRow, line: BoqLineRow) -> bool:
        raw = observation.payload.get("observation", {}).get("value")
        return (
            isinstance(raw, dict)
            and raw.get("work_code") == line.work_code
            and raw.get("unit") == line.unit
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
        )
