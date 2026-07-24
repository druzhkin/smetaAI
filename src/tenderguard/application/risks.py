from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from tenderguard.application.evidence_independence import (
    require_distinct_qualified_independence,
)
from tenderguard.application.projects import ProjectService
from tenderguard.config import Settings
from tenderguard.domain.common import content_hash, utc_now
from tenderguard.domain.enums import (
    ActorRole,
    ApprovalState,
    CostBasisKind,
    CostCategory,
    Severity,
    VerificationStatus,
    VersionStatus,
)
from tenderguard.domain.models import DomainModel, ValidationFinding
from tenderguard.domain.risk import (
    RiskCalculation,
    RiskItem,
    RiskPolicy,
    calculate_risk_reserve,
)
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.object_store import ObjectStore
from tenderguard.infrastructure.orm import (
    BoqLineRow,
    ControlledVersionRow,
    ObservationRow,
    ProjectControlledVersionRow,
    RiskCalculationRow,
    RiskItemRow,
    VerificationFindingRow,
)


class RiskItemDraft(DomainModel):
    risk_key: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1)
    probability: Decimal = Field(ge=0, le=1)
    impact_min: Decimal = Field(ge=0)
    impact_most_likely: Decimal = Field(ge=0)
    impact_max: Decimal = Field(ge=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    observation_ids: tuple[str, ...] = Field(min_length=1)
    correlated: bool = False
    correlation_group: str | None = None
    mitigation_cost_input_id: str | None = None

    def evidence_value(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"observation_ids"})


class RiskItemView(DomainModel):
    row_id: str
    risk: RiskItem
    supersedes_risk_id: str | None
    is_current: bool


class RiskCalculationView(DomainModel):
    calculation_id: str
    calculation: RiskCalculation
    status: str
    input_signature: str
    supersedes_calculation_id: str | None


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

    def submit_risk(
        self,
        *,
        actor: Actor,
        project_id: str,
        draft: RiskItemDraft,
        request_id: str,
        reason: str,
    ) -> RiskItemView:
        project = self._require_editable_state(
            actor,
            project_id,
            required_roles=(ActorRole.ESTIMATOR, ActorRole.TECHNICAL_EXPERT),
        )
        observations = self._observations(project.id, draft.observation_ids)
        self._validate_observation_values(draft, observations)
        model = self._risk_model(project.id)
        independent = model.payload.get("independently_verified_risk_keys")
        if not isinstance(independent, list) or not all(
            isinstance(item, str) for item in independent
        ):
            raise ValueError("Risk model independence requirements are invalid")
        if draft.risk_key in independent:
            require_distinct_qualified_independence(
                self.session,
                project_id=project.id,
                observations=observations,
            )
        candidate = RiskItem(
            risk_id=f"risk-item-{uuid4()}",
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
        if previous is not None:
            previous.is_current = False
            previous.updated_at = now
        row = RiskItemRow(
            id=candidate.risk_id,
            project_id=project.id,
            risk_key=draft.risk_key,
            status=candidate.status.value,
            currency=candidate.currency,
            expected_impact=None,
            supersedes_risk_id=previous.id if previous else None,
            is_current=True,
            payload={
                "risk": candidate.model_dump(mode="json"),
                "risk_key": draft.risk_key,
                "evidence_value": draft.evidence_value(),
                "created_by": actor.actor_id,
                "risk_model_version_id": model.id,
                "document_set_revision_id": project.current_document_set_revision_id,
            },
            created_at=now,
            updated_at=now,
        )
        self.session.add(row)
        self._audit(
            project.id,
            actor,
            request_id,
            reason,
            "risk_item_submitted",
            {
                "risk_item_id": row.id,
                "risk_key": row.risk_key,
                "observation_ids": list(draft.observation_ids),
                "risk_model_version_id": model.id,
                "supersedes_risk_id": row.supersedes_risk_id,
            },
        )
        return self._view(row)

    def verify_risk(
        self,
        *,
        actor: Actor,
        project_id: str,
        risk_item_id: str,
        request_id: str,
        reason: str,
    ) -> RiskItemView:
        project = self._require_editable_state(
            actor,
            project_id,
            required_roles=(ActorRole.TECHNICAL_EXPERT, ActorRole.REVIEWER),
        )
        row = self._current_risk(project.id, risk_item_id)
        if row.status != VerificationStatus.IN_REVIEW.value:
            raise ValueError("Only an IN_REVIEW risk item can be verified")
        if row.payload.get("document_set_revision_id") != project.current_document_set_revision_id:
            raise ValueError("Risk item belongs to a superseded document-set revision")
        if row.payload.get("created_by") == actor.actor_id:
            raise ValueError("Risk verification requires a different actor")
        risk = RiskItem.model_validate(row.payload["risk"])
        observations = self._observations(project.id, risk.observation_ids)
        draft = RiskItemDraft.model_validate(
            {
                "risk_key": row.risk_key,
                **row.payload["evidence_value"],
                "observation_ids": risk.observation_ids,
            }
        )
        self._validate_observation_values(draft, observations)
        model = self._risk_model(project.id)
        if row.payload.get("risk_model_version_id") != model.id:
            raise ValueError("Risk item was prepared under a superseded risk model")
        independent = model.payload.get("independently_verified_risk_keys", [])
        if row.risk_key in independent:
            require_distinct_qualified_independence(
                self.session,
                project_id=project.id,
                observations=observations,
            )
        verified = risk.model_copy(update={"status": VerificationStatus.VERIFIED})
        now = utc_now()
        row.status = verified.status.value
        row.payload = {
            **row.payload,
            "risk": verified.model_dump(mode="json"),
            "verified_by": actor.actor_id,
            "verified_at": now.isoformat(),
        }
        row.updated_at = now
        self._audit(
            project.id,
            actor,
            request_id,
            reason,
            "risk_item_verified",
            {"risk_item_id": row.id, "risk_key": row.risk_key},
        )
        return self._view(row)

    def calculate_reserve(
        self,
        *,
        actor: Actor,
        project_id: str,
        request_id: str,
        reason: str,
    ) -> RiskCalculationView:
        project = self._require_editable_state(
            actor,
            project_id,
            required_roles=(
                ActorRole.ESTIMATOR,
                ActorRole.TECHNICAL_EXPERT,
                ActorRole.REVIEWER,
            ),
        )
        model = self._risk_model(project.id)
        raw_policy = model.payload.get("policy")
        if not isinstance(raw_policy, dict):
            raise ValueError("Approved risk model has no policy")
        policy = RiskPolicy.model_validate(
            {
                **raw_policy,
                "policy_version": model.id,
            }
        )
        rows = list(
            self.session.scalars(
                select(RiskItemRow)
                .where(
                    RiskItemRow.project_id == project.id,
                    RiskItemRow.is_current.is_(True),
                )
                .order_by(RiskItemRow.risk_key)
            )
        )
        minimum = model.payload.get("minimum_risk_items")
        if not isinstance(minimum, int) or minimum < 1:
            raise ValueError("Risk model minimum_risk_items must be a positive integer")
        if len(rows) < minimum:
            raise ValueError("Risk register has fewer items than the approved minimum")
        risks = tuple(RiskItem.model_validate(row.payload["risk"]) for row in rows)
        calculation = calculate_risk_reserve(risks, policy)
        reserve_reference = self._reserve_cost_component(model, project.id)
        input_signature = content_hash(
            {
                "risk_item_ids": [row.id for row in rows],
                "risk_model_version_id": model.id,
                "reserve_cost_component": reserve_reference,
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
        row = RiskCalculationRow(
            id=calculation_id,
            project_id=project.id,
            policy_version_id=model.id,
            status=status,
            expected_reserve=calculation.expected_reserve,
            currency=calculation.currency,
            unit=str(model.payload.get("reserve_unit")),
            supersedes_calculation_id=previous.id if previous else None,
            is_current=True,
            payload={
                "calculation": calculation.model_dump(mode="json"),
                "input_signature": input_signature,
                "reserve_cost_component": reserve_reference,
                "basis_type": "RISK_RESERVE",
                "unit_rate": str(calculation.expected_reserve),
                "currency": calculation.currency,
                "unit": str(model.payload.get("reserve_unit")),
                "calculated_by": actor.actor_id,
            },
            created_at=now,
        )
        self.session.add(row)
        for risk_row in rows:
            risk_row.expected_impact = calculation.per_risk_expected_impact.get(risk_row.id)
        self._replace_findings(project.id, calculation.findings, calculation_id)
        self._audit(
            project.id,
            actor,
            request_id,
            reason,
            "risk_reserve_calculated",
            {
                "risk_calculation_id": calculation_id,
                "status": status,
                "expected_reserve": calculation.expected_reserve,
                "currency": calculation.currency,
                "input_signature": input_signature,
                "risk_model_version_id": model.id,
            },
        )
        return RiskCalculationView(
            calculation_id=calculation_id,
            calculation=calculation,
            status=status,
            input_signature=input_signature,
            supersedes_calculation_id=previous.id if previous else None,
        )

    def _risk_model(self, project_id: str) -> ControlledVersionRow:
        row = self.session.scalar(
            select(ControlledVersionRow)
            .join(
                ProjectControlledVersionRow,
                ProjectControlledVersionRow.controlled_version_id == ControlledVersionRow.id,
            )
            .where(
                ProjectControlledVersionRow.project_id == project_id,
                ProjectControlledVersionRow.purpose == "risk_model",
                ControlledVersionRow.kind == "risk_model",
                ControlledVersionRow.status == VersionStatus.APPROVED.value,
            )
        )
        if row is None:
            raise ValueError("A bound approved risk_model version is required")
        return row

    def _reserve_cost_component(
        self,
        model: ControlledVersionRow,
        project_id: str,
    ) -> dict[str, str]:
        reference = model.payload.get("reserve_cost_component")
        if not isinstance(reference, dict):
            raise ValueError("Risk model has no reserve cost component")
        line_id = reference.get("line_id")
        semantic_key = reference.get("semantic_key")
        reserve_unit = model.payload.get("reserve_unit")
        if not all(
            isinstance(item, str) and item for item in (line_id, semantic_key, reserve_unit)
        ):
            raise ValueError("Risk reserve cost component reference is invalid")
        line = self.session.scalar(
            select(BoqLineRow).where(
                BoqLineRow.id == line_id,
                BoqLineRow.project_id == project_id,
                BoqLineRow.is_current.is_(True),
                BoqLineRow.status == VerificationStatus.VERIFIED.value,
            )
        )
        if line is None or line.unit != reserve_unit:
            raise ValueError("Risk reserve BoQ line or unit does not match the risk model")
        components = line.payload.get("cost_components")
        component = (
            next(
                (
                    item
                    for item in components
                    if isinstance(item, dict) and item.get("semantic_key") == semantic_key
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
            raise ValueError("Risk model must reference a planned RISK/RISK_MODEL component")
        return {"line_id": str(line_id), "semantic_key": str(semantic_key)}

    def _observations(
        self,
        project_id: str,
        observation_ids: tuple[str, ...],
    ) -> tuple[ObservationRow, ...]:
        rows = tuple(
            self.session.scalars(
                select(ObservationRow).where(
                    ObservationRow.project_id == project_id,
                    ObservationRow.id.in_(observation_ids),
                )
            )
        )
        if len(rows) != len(set(observation_ids)):
            raise ValueError("One or more risk evidence observations are missing")
        return rows

    @staticmethod
    def _validate_observation_values(
        draft: RiskItemDraft,
        observations: tuple[ObservationRow, ...],
    ) -> None:
        expected_hash = content_hash(draft.evidence_value())
        for row in observations:
            if content_hash(row.payload.get("observation", {}).get("value")) != expected_hash:
                raise ValueError("Risk evidence observations do not reproduce the risk item")

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

    def _replace_findings(
        self,
        project_id: str,
        findings: tuple[ValidationFinding, ...],
        calculation_id: str,
    ) -> None:
        now = utc_now()
        prior = list(
            self.session.scalars(
                select(VerificationFindingRow).where(
                    VerificationFindingRow.project_id == project_id,
                    VerificationFindingRow.contour == "RISK",
                    VerificationFindingRow.resolved.is_(False),
                )
            )
        )
        for old_finding in prior:
            old_finding.resolved = True
            old_finding.updated_at = now
            old_finding.payload = {
                **old_finding.payload,
                "resolved_by_risk_calculation_id": calculation_id,
                "resolved_at": now.isoformat(),
            }
        for finding in findings:
            identity = {"project_id": project_id, "contour": "RISK", "finding": finding}
            finding_id = f"finding-{content_hash(identity)[:24]}"
            existing = self.session.get(VerificationFindingRow, finding_id)
            payload = {
                **finding.model_dump(mode="json"),
                "risk_calculation_id": calculation_id,
            }
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
        if ApprovalState(project.state) not in {
            ApprovalState.BOQ_IN_PROGRESS,
            ApprovalState.BOQ_REVIEW,
            ApprovalState.PRICING_IN_PROGRESS,
            ApprovalState.RFQ_REQUIRED,
        }:
            raise ValueError("Risk register must be fixed before calculation")
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

    def _project_service(self) -> ProjectService:
        return ProjectService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        )

    @staticmethod
    def _view(row: RiskItemRow) -> RiskItemView:
        return RiskItemView(
            row_id=row.id,
            risk=RiskItem.model_validate(row.payload["risk"]),
            supersedes_risk_id=row.supersedes_risk_id,
            is_current=row.is_current,
        )
