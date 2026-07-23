from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from tenderguard.domain.common import content_hash
from tenderguard.domain.enums import (
    CostBasisKind,
    Severity,
    VerificationStatus,
    VersionStatus,
)
from tenderguard.infrastructure.orm import (
    ApprovalTaskRow,
    BoqLineRow,
    ContractTermRow,
    ControlledVersionRow,
    NomenclatureMatchRow,
    NormativeCalculationRow,
    PriceDecisionRow,
    ProjectControlledVersionRow,
    ProjectPassportFactRow,
    QuantityRow,
    RiskCalculationRow,
    RiskItemRow,
    ScopeEvaluationRow,
    ScopeFindingRow,
)


def passport_stage_blockers(session: Session, project_id: str) -> tuple[str, ...]:
    requirements = session.scalar(
        select(ControlledVersionRow)
        .join(
            ProjectControlledVersionRow,
            ProjectControlledVersionRow.controlled_version_id == ControlledVersionRow.id,
        )
        .where(
            ProjectControlledVersionRow.project_id == project_id,
            ProjectControlledVersionRow.purpose == "document_requirements",
            ControlledVersionRow.kind == "document_requirements",
            ControlledVersionRow.status == VersionStatus.APPROVED.value,
        )
    )
    if requirements is None:
        return ("document_requirements:missing",)
    passport = requirements.payload.get("passport")
    if not isinstance(passport, dict):
        return (f"{requirements.id}:passport-section-invalid",)
    required_fields = passport.get("required_fields")
    if not isinstance(required_fields, list) or not all(
        isinstance(item, str) for item in required_fields
    ):
        return (f"{requirements.id}:required-fields-invalid",)
    facts = {
        row.field_name: row
        for row in session.scalars(
            select(ProjectPassportFactRow).where(
                ProjectPassportFactRow.project_id == project_id,
                ProjectPassportFactRow.is_current.is_(True),
            )
        )
    }
    return tuple(
        f"passport:{field_name}"
        for field_name in sorted(required_fields)
        if field_name not in facts
        or facts[field_name].status != VerificationStatus.VERIFIED.value
        or facts[field_name].payload.get("requirements_version_id") != requirements.id
    )


def boq_stage_blockers(session: Session, project_id: str) -> tuple[str, ...]:
    quantity_policy = session.scalar(
        select(ControlledVersionRow)
        .join(
            ProjectControlledVersionRow,
            ProjectControlledVersionRow.controlled_version_id == ControlledVersionRow.id,
        )
        .where(
            ProjectControlledVersionRow.project_id == project_id,
            ProjectControlledVersionRow.purpose == "quantity_policy",
            ControlledVersionRow.kind == "quantity_policy",
            ControlledVersionRow.status == VersionStatus.APPROVED.value,
        )
    )
    lines = list(
        session.scalars(
            select(BoqLineRow).where(
                BoqLineRow.project_id == project_id,
                BoqLineRow.is_current.is_(True),
            )
        )
    )
    if not lines:
        return ("boq:no-lines",)
    blockers = [
        f"boq-line:{line.id}" for line in lines if line.status != VerificationStatus.VERIFIED.value
    ]
    current_quantities = {
        row.boq_line_id: row
        for row in session.scalars(
            select(QuantityRow)
            .join(BoqLineRow, BoqLineRow.id == QuantityRow.boq_line_id)
            .where(
                BoqLineRow.project_id == project_id,
                BoqLineRow.is_current.is_(True),
                QuantityRow.is_current.is_(True),
            )
        )
    }
    blockers.extend(
        f"quantity:{line.id}"
        for line in lines
        if line.id not in current_quantities
        or current_quantities[line.id].status != VerificationStatus.VERIFIED.value
        or quantity_policy is None
        or current_quantities[line.id].payload.get("quantity_policy_version_id")
        != quantity_policy.id
    )
    if quantity_policy is None:
        blockers.append("quantity-policy:missing")
    return tuple(sorted(blockers))


def scope_input_signature(session: Session, project_id: str, wbs_node_id: str) -> str:
    lines = list(
        session.scalars(
            select(BoqLineRow)
            .where(
                BoqLineRow.project_id == project_id,
                BoqLineRow.wbs_node_id == wbs_node_id,
                BoqLineRow.is_current.is_(True),
            )
            .order_by(BoqLineRow.id)
        )
    )
    quantities = {
        row.boq_line_id: row
        for row in session.scalars(
            select(QuantityRow).where(
                QuantityRow.boq_line_id.in_([line.id for line in lines]),
                QuantityRow.is_current.is_(True),
            )
        )
    }
    project_tags = session.scalar(
        select(ProjectPassportFactRow).where(
            ProjectPassportFactRow.project_id == project_id,
            ProjectPassportFactRow.field_name == "project_tags",
            ProjectPassportFactRow.is_current.is_(True),
        )
    )
    return content_hash(
        {
            "lines": [
                {
                    "id": line.id,
                    "work_code": line.work_code,
                    "unit": line.unit,
                    "status": line.status,
                    "quantity_id": (quantities[line.id].id if line.id in quantities else None),
                    "quantity_status": (
                        quantities[line.id].status if line.id in quantities else None
                    ),
                }
                for line in lines
            ],
            "project_tags_fact": (
                {
                    "id": project_tags.id,
                    "status": project_tags.status,
                    "value": project_tags.payload.get("value"),
                }
                if project_tags
                else None
            ),
        }
    )


def scope_stage_blockers(session: Session, project_id: str) -> tuple[str, ...]:
    blockers = list(boq_stage_blockers(session, project_id))
    current_rules = session.scalar(
        select(ControlledVersionRow)
        .join(
            ProjectControlledVersionRow,
            ProjectControlledVersionRow.controlled_version_id == ControlledVersionRow.id,
        )
        .where(
            ProjectControlledVersionRow.project_id == project_id,
            ProjectControlledVersionRow.purpose == "scope_rules",
            ControlledVersionRow.kind == "scope_rules",
            ControlledVersionRow.status == VersionStatus.APPROVED.value,
        )
    )
    if current_rules is None:
        blockers.append("scope-rules:missing")
    wbs_nodes = tuple(
        session.scalars(
            select(BoqLineRow.wbs_node_id)
            .where(
                BoqLineRow.project_id == project_id,
                BoqLineRow.is_current.is_(True),
            )
            .distinct()
        )
    )
    evaluations = {
        row.wbs_node_id: row
        for row in session.scalars(
            select(ScopeEvaluationRow).where(
                ScopeEvaluationRow.project_id == project_id,
                ScopeEvaluationRow.is_current.is_(True),
            )
        )
    }
    for wbs_node_id in wbs_nodes:
        evaluation = evaluations.get(wbs_node_id)
        if evaluation is None:
            blockers.append(f"scope-evaluation:{wbs_node_id}:missing")
            continue
        expected_signature = scope_input_signature(session, project_id, wbs_node_id)
        if current_rules is None or evaluation.rule_pack_version_id != current_rules.id:
            blockers.append(f"scope-evaluation:{evaluation.id}:wrong-rule-pack")
        elif evaluation.input_signature != expected_signature:
            blockers.append(f"scope-evaluation:{evaluation.id}:stale")
        elif evaluation.status != "PASSED":
            blockers.append(f"scope-evaluation:{evaluation.id}:blocked")
    blockers.extend(
        f"scope-finding:{finding_id}"
        for finding_id in session.scalars(
            select(ScopeFindingRow.id).where(
                ScopeFindingRow.project_id == project_id,
                ScopeFindingRow.severity == Severity.BLOCKER.value,
                ScopeFindingRow.resolved.is_(False),
            )
        )
    )
    return tuple(sorted(set(blockers)))


def pricing_stage_blockers(session: Session, project_id: str) -> tuple[str, ...]:
    bound_versions = {
        purpose: row
        for row, purpose in session.execute(
            select(ControlledVersionRow, ProjectControlledVersionRow.purpose)
            .join(
                ProjectControlledVersionRow,
                ProjectControlledVersionRow.controlled_version_id == ControlledVersionRow.id,
            )
            .where(
                ProjectControlledVersionRow.project_id == project_id,
                ProjectControlledVersionRow.purpose.in_(("catalog", "price_policy")),
                ControlledVersionRow.status == VersionStatus.APPROVED.value,
                ControlledVersionRow.kind.in_(("catalog", "price_policy")),
            )
        )
    }
    catalog = bound_versions.get("catalog")
    price_policy = bound_versions.get("price_policy")
    lines = list(
        session.scalars(
            select(BoqLineRow).where(
                BoqLineRow.project_id == project_id,
                BoqLineRow.status == VerificationStatus.VERIFIED.value,
                BoqLineRow.is_current.is_(True),
            )
        )
    )
    components: list[tuple[BoqLineRow, dict[str, object]]] = []
    blockers: list[str] = []
    for line in lines:
        raw_components = line.payload.get("cost_components")
        if not isinstance(raw_components, list) or not raw_components:
            blockers.append(f"cost-components:{line.id}:missing")
            continue
        for component in raw_components:
            if not isinstance(component, dict):
                blockers.append(f"cost-components:{line.id}:invalid")
                continue
            components.append((line, component))

    matches = list(
        session.scalars(
            select(NomenclatureMatchRow).where(
                NomenclatureMatchRow.project_id == project_id,
                NomenclatureMatchRow.is_current.is_(True),
            )
        )
    )
    matches_by_item = {row.source_item_id: row for row in matches}
    decisions = {
        row.item_id: row
        for row in session.scalars(
            select(PriceDecisionRow).where(
                PriceDecisionRow.project_id == project_id,
                PriceDecisionRow.is_current.is_(True),
            )
        )
    }
    normative = {
        (str(row.payload.get("line_id")), str(row.payload.get("semantic_key"))): row
        for row in session.scalars(
            select(NormativeCalculationRow).where(
                NormativeCalculationRow.project_id == project_id,
                NormativeCalculationRow.status == "VALIDATED",
                NormativeCalculationRow.artifact_hash.is_not(None),
            )
        )
    }
    assumptions = {
        row.entity_id: row
        for row in session.scalars(
            select(ApprovalTaskRow).where(
                ApprovalTaskRow.project_id == project_id,
                ApprovalTaskRow.entity_type == "cost_assumption",
                ApprovalTaskRow.status == "APPROVED",
            )
        )
    }
    risk_calculation = session.scalar(
        select(RiskCalculationRow).where(
            RiskCalculationRow.project_id == project_id,
            RiskCalculationRow.is_current.is_(True),
            RiskCalculationRow.status == "VALIDATED",
        )
    )
    for line, component in components:
        semantic_key = component.get("semantic_key")
        basis_kind = component.get("basis_kind")
        if not isinstance(semantic_key, str):
            blockers.append(f"cost-components:{line.id}:semantic-key-invalid")
            continue
        if basis_kind == CostBasisKind.MARKET.value:
            match = matches_by_item.get(semantic_key)
            if (
                match is None
                or match.status != VerificationStatus.VERIFIED.value
                or catalog is None
                or match.catalog_version_id != catalog.id
                or match.match_class
                in {
                    "TECHNICALLY_UNACCEPTABLE",
                    "INSUFFICIENT_DATA",
                }
            ):
                blockers.append(f"nomenclature:{line.id}:{semantic_key}")
            decision = decisions.get(semantic_key)
            if (
                decision is None
                or decision.status != "VERIFIED"
                or decision.derived_observation_id is None
                or price_policy is None
                or decision.policy_version_id != price_policy.id
            ):
                blockers.append(f"price-decision:{line.id}:{semantic_key}")
        elif basis_kind == CostBasisKind.NORMATIVE.value:
            if (line.id, semantic_key) not in normative:
                blockers.append(f"normative-rate:{line.id}:{semantic_key}")
        elif basis_kind == CostBasisKind.APPROVED_ASSUMPTION.value:
            if f"{line.id}:{semantic_key}" not in assumptions:
                blockers.append(f"cost-assumption:{line.id}:{semantic_key}")
        elif basis_kind == CostBasisKind.RISK_MODEL.value:
            reference = (
                risk_calculation.payload.get("reserve_cost_component") if risk_calculation else None
            )
            if (
                not isinstance(reference, dict)
                or reference.get("line_id") != line.id
                or reference.get("semantic_key") != semantic_key
            ):
                blockers.append(f"risk-reserve:{line.id}:{semantic_key}")
        else:
            blockers.append(f"cost-components:{line.id}:{semantic_key}:basis-invalid")
    if not lines:
        blockers.append("boq:no-verified-lines")
    if catalog is None:
        blockers.append("catalog:missing")
    if price_policy is None:
        blockers.append("price-policy:missing")
    return tuple(sorted(set(blockers)))


def contract_stage_blockers(session: Session, project_id: str) -> tuple[str, ...]:
    rules = session.scalar(
        select(ControlledVersionRow)
        .join(
            ProjectControlledVersionRow,
            ProjectControlledVersionRow.controlled_version_id == ControlledVersionRow.id,
        )
        .where(
            ProjectControlledVersionRow.project_id == project_id,
            ProjectControlledVersionRow.purpose == "contract_risk_rules",
            ControlledVersionRow.kind == "contract_risk_rules",
            ControlledVersionRow.status == VersionStatus.APPROVED.value,
        )
    )
    if rules is None:
        return ("contract-risk-rules:missing",)
    contract = rules.payload.get("contract")
    required = contract.get("required_term_kinds") if isinstance(contract, dict) else None
    if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
        return (f"contract-risk-rules:{rules.id}:invalid",)
    terms = {
        row.kind: row
        for row in session.scalars(
            select(ContractTermRow).where(
                ContractTermRow.project_id == project_id,
                ContractTermRow.is_current.is_(True),
            )
        )
    }
    return tuple(
        f"contract-term:{kind}"
        for kind in sorted(required)
        if kind not in terms
        or not terms[kind].verified
        or not terms[kind].cost_impact_resolved
        or terms[kind].payload.get("rules_version_id") != rules.id
    )


def risk_stage_blockers(session: Session, project_id: str) -> tuple[str, ...]:
    model = session.scalar(
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
    if model is None:
        return ("risk-model:missing",)
    minimum = model.payload.get("minimum_risk_items")
    reserve_reference = model.payload.get("reserve_cost_component")
    if not isinstance(minimum, int) or minimum < 1 or not isinstance(reserve_reference, dict):
        return (f"risk-model:{model.id}:invalid",)
    risk_rows = list(
        session.scalars(
            select(RiskItemRow)
            .where(
                RiskItemRow.project_id == project_id,
                RiskItemRow.is_current.is_(True),
            )
            .order_by(RiskItemRow.risk_key)
        )
    )
    blockers = [
        f"risk-item:{row.risk_key}"
        for row in risk_rows
        if row.status != VerificationStatus.VERIFIED.value
    ]
    if len(risk_rows) < minimum:
        blockers.append("risk-register:below-approved-minimum")
    expected_signature = content_hash(
        {
            "risk_item_ids": [row.id for row in risk_rows],
            "risk_model_version_id": model.id,
            "reserve_cost_component": reserve_reference,
        }
    )
    calculation = session.scalar(
        select(RiskCalculationRow).where(
            RiskCalculationRow.project_id == project_id,
            RiskCalculationRow.is_current.is_(True),
        )
    )
    if calculation is None:
        blockers.append("risk-calculation:missing")
    elif calculation.status != "VALIDATED":
        blockers.append(f"risk-calculation:{calculation.id}:blocked")
    elif calculation.policy_version_id != model.id:
        blockers.append(f"risk-calculation:{calculation.id}:wrong-model")
    elif calculation.payload.get("input_signature") != expected_signature:
        blockers.append(f"risk-calculation:{calculation.id}:stale")
    return tuple(sorted(set(blockers)))
