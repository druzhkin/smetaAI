from __future__ import annotations

from decimal import Decimal, InvalidOperation
from io import BytesIO
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from tenderguard.application.approvals import ApprovalService
from tenderguard.application.pricing import PricingService
from tenderguard.application.projects import ProjectService, ProjectView
from tenderguard.config import Settings
from tenderguard.domain.approvals import ApprovalSubject
from tenderguard.domain.calculation import (
    AtomicCostInput,
    CalculationPolicy,
    calculate_primary,
    create_snapshot,
    validate_independently,
)
from tenderguard.domain.common import canonical_json, content_hash, ensure_utc, utc_now
from tenderguard.domain.enums import (
    ActorRole,
    ApprovalReason,
    ApprovalState,
    CostBasisKind,
    PriceStatus,
    VerificationStatus,
    VersionStatus,
)
from tenderguard.domain.models import (
    CalculationResult,
    CalculationSnapshot,
    ControlledVersion,
    DomainModel,
    IndependentValidationResult,
)
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.object_store import ObjectStore
from tenderguard.infrastructure.orm import (
    ApprovalRecordRow,
    ApprovalTaskRow,
    BoqLineRow,
    CalculationRunRow,
    CalculationSnapshotRow,
    CommercialCostModelRow,
    ControlledVersionRow,
    CostInputRow,
    NormativeCalculationRow,
    ObservationRow,
    PriceDecisionRow,
    ProjectControlledVersionRow,
    QuantityRow,
    RiskCalculationRow,
)


class CalculationExecutionResult(DomainModel):
    project: ProjectView
    primary: CalculationResult
    independent: IndependentValidationResult
    snapshot: CalculationSnapshot


class CalculationService:
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

    def execute(
        self,
        *,
        actor: Actor,
        project_id: str,
        expected_row_version: int,
        inputs: tuple[AtomicCostInput, ...],
        policy: CalculationPolicy,
        request_id: str,
        reason: str,
    ) -> CalculationExecutionResult:
        project_service = ProjectService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        )
        project = project_service.get_project(
            actor=actor,
            project_id=project_id,
            lock=True,
            required_roles=(ActorRole.ESTIMATOR,),
        )
        if project.row_version != expected_row_version:
            from tenderguard.application.projects import OptimisticLockError

            raise OptimisticLockError(
                f"Expected row version {expected_row_version}, found {project.row_version}"
            )
        if ApprovalState(project.state) is not ApprovalState.CALCULATION_IN_PROGRESS:
            raise ValueError("Project must be in CALCULATION_IN_PROGRESS")
        if not project.current_document_set_revision_id:
            raise ValueError("Current document-set revision is not confirmed")
        if not inputs:
            raise ValueError("Calculation requires atomic cost inputs")

        version_rows = list(
            self.session.scalars(
                select(ControlledVersionRow)
                .join(
                    ProjectControlledVersionRow,
                    ProjectControlledVersionRow.controlled_version_id == ControlledVersionRow.id,
                )
                .where(ProjectControlledVersionRow.project_id == project.id)
            )
        )
        versions = tuple(
            ControlledVersion(
                kind=row.kind,
                version_id=row.id,
                content_hash=row.content_hash,
                status=VersionStatus(row.status),
                approved_by=row.approved_by,
                approved_at=ensure_utc(row.approved_at),
            )
            for row in version_rows
        )
        calculation_model = next((row for row in versions if row.kind == "calculation_model"), None)
        if calculation_model is None or calculation_model.status is not VersionStatus.APPROVED:
            raise ValueError("An approved calculation_model version must be bound")
        if policy.policy_version != calculation_model.version_id:
            raise ValueError("Calculation policy does not match the bound controlled version")
        self._validate_input_lineage(
            project.id,
            project.current_document_set_revision_id,
            inputs,
            version_rows,
        )
        canonical_inputs = tuple(sorted(inputs, key=lambda item: item.cost_input_id))
        canonical_versions = tuple(sorted(versions, key=lambda item: (item.kind, item.version_id)))

        now = utc_now()
        run_id = f"calculation-run-{uuid4()}"
        primary = calculate_primary(
            canonical_inputs,
            policy,
            engine_version=calculation_model.version_id,
            calculated_at=now,
        )
        independent = validate_independently(
            canonical_inputs,
            primary,
            policy,
            validator_version=f"independent:{calculation_model.version_id}",
            validated_at=now,
        )
        risk_total = sum(
            (line.amount for line in primary.lines if line.category.value == "RISK"),
            start=Decimal("0"),
        )
        if risk_total > 0:
            reserve_share = (
                risk_total / primary.grand_total if primary.grand_total > 0 else Decimal("1")
            )
            ApprovalService(
                session=self.session,
                settings=self.settings,
                object_store=self.object_store,
            ).plan(
                actor=actor,
                project_id=project.id,
                subjects=(
                    ApprovalSubject(
                        entity_type="calculation_run",
                        entity_id=run_id,
                        reasons=frozenset({ApprovalReason.LARGE_RESERVE}),
                        reserve_share=reserve_share,
                    ),
                ),
                request_id=request_id,
                reason="Evaluate methodology-owned risk reserve approval threshold",
            )
        snapshot = create_snapshot(
            project_id=project.id,
            document_set_revision_id=project.current_document_set_revision_id,
            inputs=canonical_inputs,
            policy=policy,
            controlled_versions=canonical_versions,
            primary=primary,
            independent=independent,
            created_by=actor.actor_id,
            created_at=now,
        )
        snapshot_payload = canonical_json(
            {
                "snapshot": snapshot,
                "inputs": canonical_inputs,
                "policy": policy,
                "controlled_versions": canonical_versions,
                "primary": primary,
                "independent": independent,
            }
        )
        stored = self.object_store.put(BytesIO(snapshot_payload))
        self.session.add(
            CalculationRunRow(
                id=run_id,
                project_id=project.id,
                engine_version=calculation_model.version_id,
                status="VALIDATED" if independent.passed else "FAILED_VALIDATION",
                currency=primary.currency,
                grand_total=primary.grand_total,
                payload={
                    "primary": primary.model_dump(mode="json"),
                    "independent_validation": independent.model_dump(mode="json"),
                    "policy": policy.model_dump(mode="json"),
                },
                created_at=now,
            )
        )
        self.session.flush()
        for item in canonical_inputs:
            basis = (
                item.source_observation_id
                or item.approved_assumption_id
                or item.normative_rate_id
                or item.risk_reserve_id
                or item.derived_cost_model_id
            )
            self.session.add(
                CostInputRow(
                    id=(
                        "cost-input-"
                        + content_hash({"run_id": run_id, "cost_input_id": item.cost_input_id})[:24]
                    ),
                    project_id=project.id,
                    calculation_run_id=run_id,
                    semantic_key=item.semantic_key,
                    category=item.category.value,
                    amount_basis_id=basis,
                    payload=item.model_dump(mode="json"),
                    created_at=now,
                )
            )
        self.session.add(
            CalculationSnapshotRow(
                id=snapshot.snapshot_id,
                project_id=project.id,
                calculation_run_id=run_id,
                document_set_revision_id=snapshot.document_set_revision_id,
                input_hash=snapshot.input_hash,
                output_hash=snapshot.output_hash,
                snapshot_hash=snapshot.snapshot_hash,
                fixed=True,
                object_key=stored.object_key,
                created_by=actor.actor_id,
                created_at=now,
            )
        )
        project_service.record_event(
            aggregate_type="project",
            aggregate_id=project.id,
            event_type="calculation_snapshot_created",
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload={
                "calculation_run_id": run_id,
                "snapshot_id": snapshot.snapshot_id,
                "snapshot_hash": snapshot.snapshot_hash,
                "object_hash": stored.object_hash,
                "independent_validation_passed": independent.passed,
                "grand_total": primary.grand_total,
                "currency": primary.currency,
            },
        )
        view = project_service.transition(
            actor=actor,
            project_id=project.id,
            to_state=ApprovalState.INDEPENDENT_VALIDATION,
            expected_row_version=project.row_version,
            request_id=request_id,
            reason="Primary calculation and independent recalculation completed",
        )
        return CalculationExecutionResult(
            project=view,
            primary=primary,
            independent=independent,
            snapshot=snapshot,
        )

    def _validate_input_lineage(
        self,
        project_id: str,
        document_set_revision_id: str,
        inputs: tuple[AtomicCostInput, ...],
        version_rows: list[ControlledVersionRow],
    ) -> None:
        versions = {
            row.id: row for row in version_rows if row.status == VersionStatus.APPROVED.value
        }
        line_ids = {item.line_id for item in inputs}
        lines = list(
            self.session.scalars(
                select(BoqLineRow).where(
                    BoqLineRow.project_id == project_id,
                    BoqLineRow.id.in_(line_ids),
                    BoqLineRow.status == VerificationStatus.VERIFIED.value,
                    BoqLineRow.is_current.is_(True),
                )
            )
        )
        if len(lines) != len(line_ids):
            raise ValueError("One or more cost inputs reference no verified project BoQ line")
        component_definitions: dict[tuple[str, str], dict[str, object]] = {}
        for line in lines:
            raw_components = line.payload.get("cost_components")
            if not isinstance(raw_components, list) or not raw_components:
                raise ValueError(f"BoQ line {line.id} has no verified cost component plan")
            for component in raw_components:
                if not isinstance(component, dict):
                    raise ValueError(f"BoQ line {line.id} has an invalid cost component")
                semantic_key = component.get("semantic_key")
                if not isinstance(semantic_key, str):
                    raise ValueError(f"BoQ line {line.id} has an invalid semantic key")
                component_definitions[(line.id, semantic_key)] = component
        input_keys = [(item.line_id, item.semantic_key) for item in inputs]
        if len(input_keys) != len(set(input_keys)):
            raise ValueError("Calculation contains duplicate BoQ cost components")
        if set(input_keys) != set(component_definitions):
            raise ValueError(
                "Calculation inputs do not exactly cover the verified BoQ cost component plan"
            )
        quantities = {
            row.boq_line_id: row
            for row in self.session.scalars(
                select(QuantityRow).where(
                    QuantityRow.boq_line_id.in_(line_ids),
                    QuantityRow.is_current.is_(True),
                    QuantityRow.status == VerificationStatus.VERIFIED.value,
                )
            )
        }
        line_by_id = {line.id: line for line in lines}
        for item in inputs:
            line = line_by_id[item.line_id]
            quantity = quantities.get(item.line_id)
            if quantity is None or quantity.value != item.quantity or quantity.unit != item.unit:
                raise ValueError(
                    f"Cost input {item.cost_input_id} does not reproduce the current "
                    "verified BoQ quantity"
                )
            if line.wbs_node_id != item.wbs_node_id:
                raise ValueError(f"Cost input {item.cost_input_id} differs from the BoQ WBS node")
            component = component_definitions[(item.line_id, item.semantic_key)]
            if component.get("category") != item.category.value:
                raise ValueError(
                    f"Cost input {item.cost_input_id} differs from its planned cost category"
                )
            basis_kind = component.get("basis_kind")
            if basis_kind == CostBasisKind.MARKET.value and item.source_observation_id is None:
                raise ValueError(
                    f"Market cost component {item.cost_input_id} requires a price observation"
                )
            if basis_kind == CostBasisKind.NORMATIVE.value and item.normative_rate_id is None:
                raise ValueError(
                    f"Normative cost component {item.cost_input_id} requires a normative rate"
                )
            if (
                basis_kind == CostBasisKind.APPROVED_ASSUMPTION.value
                and item.approved_assumption_id is None
            ):
                raise ValueError(
                    f"Assumption cost component {item.cost_input_id} requires an approval"
                )
            if basis_kind == CostBasisKind.RISK_MODEL.value and item.risk_reserve_id is None:
                raise ValueError(
                    f"Risk cost component {item.cost_input_id} requires a risk calculation"
                )
            if (
                basis_kind == CostBasisKind.DERIVED_MODEL.value
                and item.derived_cost_model_id is None
            ):
                raise ValueError(
                    f"Derived cost component {item.cost_input_id} requires a validated model"
                )
            if item.source_observation_id:
                observation = self.session.scalar(
                    select(ObservationRow).where(
                        ObservationRow.id == item.source_observation_id,
                        ObservationRow.project_id == project_id,
                        ObservationRow.status == "VERIFIED",
                    )
                )
                if observation is None:
                    raise ValueError(
                        f"Cost input {item.cost_input_id} references no verified observation"
                    )
                price_decision = self.session.scalar(
                    select(PriceDecisionRow).where(
                        PriceDecisionRow.project_id == project_id,
                        PriceDecisionRow.item_id == item.semantic_key,
                        PriceDecisionRow.is_current.is_(True),
                        PriceDecisionRow.status == PriceStatus.VERIFIED.value,
                        PriceDecisionRow.derived_observation_id == observation.id,
                    )
                )
                if price_decision is None:
                    raise ValueError(
                        f"Cost input {item.cost_input_id} does not reference the current "
                        "verified price decision"
                    )
                decision = PricingService(
                    session=self.session,
                    settings=self.settings,
                    object_store=self.object_store,
                ).require_price_decision_integrity(price_decision)
                if (
                    decision.status is not PriceStatus.VERIFIED
                    or decision.decision_id != price_decision.id
                ):
                    raise ValueError(
                        f"Cost input {item.cost_input_id} references a price decision "
                        "that failed deterministic integrity validation"
                    )
                self._match_rate_basis(
                    item,
                    observation.payload,
                    expected_basis_type="NORMALIZED_PRICE",
                )
            elif item.approved_assumption_id:
                approval = self.session.scalar(
                    select(ApprovalRecordRow)
                    .join(ApprovalTaskRow, ApprovalTaskRow.id == ApprovalRecordRow.task_id)
                    .where(
                        ApprovalRecordRow.id == item.approved_assumption_id,
                        ApprovalRecordRow.decision == "APPROVED",
                        ApprovalTaskRow.project_id == project_id,
                        ApprovalTaskRow.entity_type == "cost_assumption",
                        ApprovalTaskRow.entity_id == f"{item.line_id}:{item.semantic_key}",
                    )
                )
                if approval is None:
                    raise ValueError(
                        f"Cost input {item.cost_input_id} references no approved assumption"
                    )
                self._match_rate_basis(
                    item,
                    approval.payload,
                    expected_basis_type="APPROVED_ASSUMPTION",
                )
            elif item.normative_rate_id:
                normative = self.session.scalar(
                    select(NormativeCalculationRow).where(
                        NormativeCalculationRow.id == item.normative_rate_id,
                        NormativeCalculationRow.project_id == project_id,
                        NormativeCalculationRow.status == "VALIDATED",
                        NormativeCalculationRow.artifact_hash.is_not(None),
                    )
                )
                if normative is None:
                    raise ValueError(
                        f"Cost input {item.cost_input_id} references no validated normative rate"
                    )
                self._match_rate_basis(
                    item,
                    normative.payload,
                    expected_basis_type="NORMATIVE_RATE",
                )
            elif item.risk_reserve_id:
                risk = self.session.scalar(
                    select(RiskCalculationRow).where(
                        RiskCalculationRow.id == item.risk_reserve_id,
                        RiskCalculationRow.project_id == project_id,
                        RiskCalculationRow.status == "VALIDATED",
                        RiskCalculationRow.is_current.is_(True),
                    )
                )
                if risk is None:
                    raise ValueError(
                        f"Cost input {item.cost_input_id} references no current validated "
                        "risk calculation"
                    )
                reference = risk.payload.get("reserve_cost_component")
                if (
                    not isinstance(reference, dict)
                    or reference.get("line_id") != item.line_id
                    or reference.get("semantic_key") != item.semantic_key
                ):
                    raise ValueError(
                        f"Risk calculation does not belong to cost input {item.cost_input_id}"
                    )
                self._match_rate_basis(
                    item,
                    risk.payload,
                    expected_basis_type="RISK_RESERVE",
                )
            elif item.derived_cost_model_id:
                commercial = self.session.scalar(
                    select(CommercialCostModelRow).where(
                        CommercialCostModelRow.id == item.derived_cost_model_id,
                        CommercialCostModelRow.project_id == project_id,
                        CommercialCostModelRow.status == "VALIDATED",
                        CommercialCostModelRow.is_current.is_(True),
                        CommercialCostModelRow.target_line_id == item.line_id,
                        CommercialCostModelRow.target_semantic_key == item.semantic_key,
                        CommercialCostModelRow.category == item.category.value,
                        CommercialCostModelRow.document_set_revision_id == document_set_revision_id,
                    )
                )
                commercial_policy = (
                    versions.get(commercial.policy_version_id) if commercial is not None else None
                )
                if (
                    commercial is None
                    or commercial_policy is None
                    or commercial_policy.kind != "commercial_cost_model"
                ):
                    raise ValueError(
                        f"Cost input {item.cost_input_id} references no current, "
                        "document-aligned, policy-bound commercial cost model"
                    )
                self._match_rate_basis(
                    item,
                    commercial.payload,
                    expected_basis_type="DERIVED_COMMERCIAL_COST",
                )
            else:
                raise ValueError(f"Cost input {item.cost_input_id} has no evidence basis")

            for factor in item.factors:
                version = versions.get(factor.version_id)
                if version is None:
                    raise ValueError(
                        f"Factor {factor.factor_id} does not reference a bound approved version"
                    )
                factors = version.payload.get("factors")
                definition = factors.get(factor.factor_id) if isinstance(factors, dict) else None
                if not isinstance(definition, dict):
                    raise ValueError(
                        f"Factor {factor.factor_id} is absent from controlled version "
                        f"{factor.version_id}"
                    )
                try:
                    controlled_value = Decimal(str(definition["value"]))
                except (KeyError, InvalidOperation, TypeError, ValueError) as error:
                    raise ValueError(
                        f"Factor {factor.factor_id} has no valid controlled value"
                    ) from error
                if controlled_value != factor.value:
                    raise ValueError(f"Factor {factor.factor_id} differs from controlled version")
                if factor.evidence_or_rule_id not in {
                    factor.version_id,
                    str(definition.get("rule_id", "")),
                }:
                    raise ValueError(
                        f"Factor {factor.factor_id} has no matching controlled rule reference"
                    )

    @staticmethod
    def _match_rate_basis(
        item: AtomicCostInput,
        payload: dict[str, object],
        *,
        expected_basis_type: str,
    ) -> None:
        try:
            basis_rate = Decimal(str(payload["unit_rate"]))
        except (KeyError, InvalidOperation, TypeError, ValueError) as error:
            raise ValueError(
                f"Basis for cost input {item.cost_input_id} has no valid unit rate"
            ) from error
        if (
            payload.get("basis_type") != expected_basis_type
            or basis_rate != item.unit_rate
            or payload.get("currency") != item.currency
            or payload.get("unit") != item.unit
        ):
            raise ValueError(
                f"Basis for cost input {item.cost_input_id} does not reproduce "
                "its rate, currency, and unit"
            )
