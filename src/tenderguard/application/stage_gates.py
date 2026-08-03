from __future__ import annotations

from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal

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
from tenderguard.config import Settings
from tenderguard.domain.common import content_hash, ensure_utc
from tenderguard.domain.contract import ContractRequirementsPolicy
from tenderguard.domain.enums import (
    CostBasisKind,
    EvidenceMethod,
    Severity,
    VerificationStatus,
    VersionStatus,
)
from tenderguard.domain.models import Observation
from tenderguard.domain.passport import PassportRequirementsPolicy
from tenderguard.domain.risk import RiskItem, RiskModelDefinition, calculate_risk_reserve
from tenderguard.infrastructure.orm import (
    ApprovalRecordRow,
    ApprovalTaskRow,
    AuditEventRow,
    BoqLineRow,
    CommercialCostModelRow,
    ConflictRow,
    ContractTermRow,
    ControlledVersionRow,
    ManualChangeRow,
    NomenclatureMatchRow,
    NormativeCalculationRow,
    ObservationRow,
    PriceDecisionRow,
    ProjectControlledVersionRow,
    ProjectPassportFactRow,
    ProjectRow,
    QuantityManualChangeApplicationRow,
    QuantityRow,
    RiskCalculationRow,
    RiskItemRow,
    ScopeEvaluationRow,
    ScopeFindingRow,
)


def passport_stage_blockers(
    session: Session,
    settings: Settings,
    project_id: str,
) -> tuple[str, ...]:
    project = session.get(ProjectRow, project_id)
    if project is None:
        return ("project:missing",)
    try:
        requirements = require_bound_controlled_version(
            session=session,
            settings=settings,
            project_id=project_id,
            organization_id=project.organization_id,
            purpose="document_requirements",
            kind="document_requirements",
        )
        document_set = require_confirmed_document_set_integrity(
            session=session,
            settings=settings,
            project_id=project_id,
            document_set_revision_id=project.current_document_set_revision_id,
        )
    except (KeyError, LookupError, TypeError, ValueError):
        return ("passport:governance-integrity-failed",)
    passport = requirements.payload.get("passport")
    if not isinstance(passport, dict):
        return (f"{requirements.id}:passport-section-invalid",)
    try:
        policy = PassportRequirementsPolicy.model_validate(passport)
    except (TypeError, ValueError):
        return (f"{requirements.id}:passport-policy-invalid",)
    required_fields = policy.required_fields
    independent_fields = policy.independently_verified_fields
    review_role = policy.review_role.value
    facts = {
        row.field_name: row
        for row in session.scalars(
            select(ProjectPassportFactRow).where(
                ProjectPassportFactRow.project_id == project_id,
                ProjectPassportFactRow.is_current.is_(True),
            )
        )
    }
    unresolved_conflict_fields = set(
        session.scalars(
            select(ConflictRow.field_name).where(
                ConflictRow.project_id == project_id,
                ConflictRow.field_name.in_(tuple(required_fields)),
                ConflictRow.status != VerificationStatus.VERIFIED.value,
            )
        )
    )
    blockers: list[str] = []
    for field_name in sorted(required_fields):
        if field_name in unresolved_conflict_fields:
            blockers.append(f"passport:{field_name}:unresolved-conflict")
        fact = facts.get(field_name)
        if fact is None:
            blockers.append(f"passport:{field_name}:missing")
            continue
        try:
            _require_passport_fact_integrity(
                session=session,
                fact=fact,
                field_name=field_name,
                requirements=requirements,
                document_revision_ids=frozenset(document_set.revision_ids),
                document_set_revision_id=document_set.id,
                review_role=review_role,
                independent=field_name in independent_fields,
            )
        except (KeyError, LookupError, TypeError, ValueError):
            blockers.append(f"passport:{field_name}:integrity-failed")
    return tuple(blockers)


def _require_passport_fact_integrity(
    *,
    session: Session,
    fact: ProjectPassportFactRow,
    field_name: str,
    requirements: ControlledVersionRow,
    document_revision_ids: frozenset[str],
    document_set_revision_id: str,
    review_role: str,
    independent: bool,
) -> None:
    observation_ids = fact.payload.get("observation_ids")
    independence_source_ids = fact.payload.get("independence_source_ids")
    task_id = fact.payload.get("approval_task_id")
    created_by = fact.payload.get("created_by")
    verified_by = fact.payload.get("verified_by")
    if (
        fact.status != VerificationStatus.VERIFIED.value
        or fact.field_name != field_name
        or not isinstance(observation_ids, list)
        or not observation_ids
        or len(observation_ids) != len(set(observation_ids))
        or not isinstance(independence_source_ids, list)
        or not isinstance(task_id, str)
        or not task_id
        or not isinstance(created_by, str)
        or not created_by
        or not isinstance(verified_by, str)
        or not verified_by
        or created_by == verified_by
        or fact.payload.get("reviewed_by") != verified_by
        or fact.payload.get("review_decision") != "APPROVED"
        or fact.payload.get("requirements_version_id") != requirements.id
        or fact.payload.get("requirements_content_hash") != requirements.content_hash
        or fact.payload.get("document_set_revision_id") != document_set_revision_id
        or fact.payload.get("review_role") != review_role
    ):
        raise ValueError("Verified passport fact provenance is invalid")
    observations = list(
        session.scalars(
            select(ObservationRow).where(
                ObservationRow.project_id == fact.project_id,
                ObservationRow.id.in_(observation_ids),
            )
        )
    )
    if len(observations) != len(observation_ids):
        raise ValueError("Passport fact evidence is missing")
    value_hash = content_hash(fact.payload.get("value"))
    for row in observations:
        observation = Observation.model_validate(row.payload.get("observation"))
        if (
            row.id != observation.observation_id
            or row.document_revision_id != observation.location.document_revision_id
            or row.document_revision_id not in document_revision_ids
            or row.field_name != field_name
            or row.field_name != observation.field_name
            or row.method != observation.method.value
            or row.method_version != observation.method_version
            or row.status != observation.status.value
            or observation.status
            not in {VerificationStatus.UNVERIFIED, VerificationStatus.VERIFIED}
            or (
                observation.method is EvidenceMethod.MANUAL
                and observation.status is not VerificationStatus.VERIFIED
            )
            or content_hash(observation.value) != value_hash
            or observation.unit != fact.payload.get("unit")
        ):
            raise ValueError("Passport fact evidence no longer reproduces the fact")
    provenance_leaves = resolve_observation_leaves(
        session,
        project_id=fact.project_id,
        observations=observations,
    )
    for row in provenance_leaves:
        observation = Observation.model_validate(row.payload.get("observation"))
        if (
            row.id != observation.observation_id
            or row.project_id != fact.project_id
            or row.document_revision_id != observation.location.document_revision_id
            or row.document_revision_id not in document_revision_ids
            or row.field_name != field_name
            or row.field_name != observation.field_name
            or row.method != observation.method.value
            or row.method_version != observation.method_version
            or row.status != observation.status.value
            or content_hash(observation.value) != value_hash
            or observation.unit != fact.payload.get("unit")
        ):
            raise ValueError("Passport provenance leaves no longer reproduce the fact")
    if independent:
        leaves = require_distinct_qualified_independence(
            session,
            project_id=fact.project_id,
            observations=observations,
        )
        if tuple(independence_source_ids) != leaves:
            raise ValueError("Passport fact independence sources changed")
        if leaves != tuple(row.id for row in provenance_leaves):
            raise ValueError("Passport fact independence leaf resolution changed")
    task = session.get(ApprovalTaskRow, task_id)
    if (
        task is None
        or task.project_id != fact.project_id
        or task.task_type != "PASSPORT_FACT_REVIEW"
        or task.entity_type != "passport_fact"
        or task.entity_id != fact.id
        or task.assigned_role != review_role
        or not task.required
        or task.status != "APPROVED"
        or task.payload.get("created_by") != created_by
        or task.payload.get("fact_id") != fact.id
        or task.payload.get("observation_ids") != observation_ids
        or task.payload.get("independence_source_ids") != independence_source_ids
        or task.payload.get("requirements_version_id") != requirements.id
        or task.payload.get("requirements_content_hash") != requirements.content_hash
        or task.payload.get("document_set_revision_id") != document_set_revision_id
        or task.payload.get("review_role") != review_role
    ):
        raise ValueError("Passport approval task integrity failed")
    expected_submission_hash = content_hash(
        {
            "field_name": fact.field_name,
            "value": fact.payload.get("value"),
            "unit": fact.payload.get("unit"),
            "observation_ids": observation_ids,
            "independence_source_ids": independence_source_ids,
            "created_by": created_by,
            "requirements_version_id": requirements.id,
            "requirements_content_hash": requirements.content_hash,
            "document_set_revision_id": document_set_revision_id,
            "review_role": review_role,
        }
    )
    if task.payload.get("fact_submission_hash") != expected_submission_hash:
        raise ValueError("Passport approval task no longer reproduces its submission")
    approval = session.scalar(select(ApprovalRecordRow).where(ApprovalRecordRow.task_id == task.id))
    if (
        approval is None
        or approval.decision != "APPROVED"
        or approval.decided_by != verified_by
        or approval.payload.get("fact_id") != fact.id
        or approval.payload.get("evidence_ids") != observation_ids
        or approval.payload.get("independence_source_ids") != independence_source_ids
        or approval.payload.get("requirements_version_id") != requirements.id
        or approval.payload.get("requirements_content_hash") != requirements.content_hash
        or approval.payload.get("document_set_revision_id") != document_set_revision_id
    ):
        raise ValueError("Passport approval record integrity failed")
    decision_event = next(
        (
            event
            for event in session.scalars(
                select(AuditEventRow)
                .where(
                    AuditEventRow.aggregate_type == "project",
                    AuditEventRow.aggregate_id == fact.project_id,
                    AuditEventRow.event_type == "passport_fact_review_decided",
                )
                .order_by(AuditEventRow.sequence.desc())
            )
            if event.payload.get("fact_id") == fact.id
        ),
        None,
    )
    if (
        decision_event is None
        or decision_event.actor_id != verified_by
        or decision_event.payload.get("approval_id") != approval.id
        or decision_event.payload.get("approval_task_id") != task.id
        or decision_event.payload.get("fact_id") != fact.id
        or decision_event.payload.get("field_name") != field_name
        or decision_event.payload.get("decision") != "APPROVED"
        or decision_event.payload.get("evidence_ids") != observation_ids
        or decision_event.payload.get("requirements_version_id") != requirements.id
        or decision_event.payload.get("document_set_revision_id") != document_set_revision_id
    ):
        raise ValueError("Passport approval audit event integrity failed")


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
    applied_manual_change_ids = set(
        session.scalars(
            select(QuantityManualChangeApplicationRow.manual_change_id).where(
                QuantityManualChangeApplicationRow.project_id == project_id
            )
        )
    )
    blockers.extend(
        f"manual-change:{change.id}:unapplied"
        for change in session.scalars(
            select(ManualChangeRow).where(
                ManualChangeRow.project_id == project_id,
                ManualChangeRow.entity_type == "quantity",
                ManualChangeRow.field_name == "record",
            )
        )
        if change.payload.get("lifecycle_version") == "quantity-manual-change-v1"
        and change.id not in applied_manual_change_ids
    )
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
                ProjectControlledVersionRow.purpose.in_(
                    ("catalog", "price_policy", "commercial_cost_model")
                ),
                ControlledVersionRow.status == VersionStatus.APPROVED.value,
                ControlledVersionRow.kind.in_(("catalog", "price_policy", "commercial_cost_model")),
            )
        )
    }
    catalog = bound_versions.get("catalog")
    price_policy = bound_versions.get("price_policy")
    commercial_policy = bound_versions.get("commercial_cost_model")
    project = session.get(ProjectRow, project_id)
    current_document_set = project.current_document_set_revision_id if project is not None else None
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
    all_commercial_models = list(
        session.scalars(
            select(CommercialCostModelRow)
            .where(CommercialCostModelRow.project_id == project_id)
            .order_by(CommercialCostModelRow.created_at, CommercialCostModelRow.id)
        )
    )
    commercial_models = {
        (row.target_line_id, row.target_semantic_key): row
        for row in all_commercial_models
        if row.status == "VALIDATED" and row.is_current
    }
    latest_commercial_models = {
        (row.target_line_id, row.target_semantic_key): row for row in all_commercial_models
    }
    current_component_keys = {
        (line.id, component.get("semantic_key"))
        for line, component in components
        if isinstance(component.get("semantic_key"), str)
    }
    blockers.extend(
        f"commercial-cost-model:{line_id}:{semantic_key}:latest-unresolved"
        for (line_id, semantic_key), row in latest_commercial_models.items()
        if (line_id, semantic_key) in current_component_keys
        and (row.status != "VALIDATED" or not row.is_current)
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
        elif basis_kind == CostBasisKind.DERIVED_MODEL.value:
            model = commercial_models.get((line.id, semantic_key))
            if (
                model is None
                or commercial_policy is None
                or model.policy_version_id != commercial_policy.id
                or model.document_set_revision_id != current_document_set
                or model.category != component.get("category")
            ):
                blockers.append(f"commercial-cost-model:{line.id}:{semantic_key}")
        else:
            blockers.append(f"cost-components:{line.id}:{semantic_key}:basis-invalid")
    if not lines:
        blockers.append("boq:no-verified-lines")
    if catalog is None:
        blockers.append("catalog:missing")
    if price_policy is None:
        blockers.append("price-policy:missing")
    if commercial_policy is not None:
        policy_payload = commercial_policy.payload.get("policy")
        required_model_kinds = (
            policy_payload.get("required_model_kinds") if isinstance(policy_payload, dict) else None
        )
        if not isinstance(required_model_kinds, list) or not all(
            isinstance(item, str) for item in required_model_kinds
        ):
            blockers.append(f"commercial-cost-policy:{commercial_policy.id}:invalid")
        else:
            current_model_kinds = {
                row.model_kind
                for row in commercial_models.values()
                if row.policy_version_id == commercial_policy.id
                and row.document_set_revision_id == current_document_set
            }
            blockers.extend(
                f"commercial-cost-model:{kind}"
                for kind in sorted(set(required_model_kinds) - current_model_kinds)
            )
    return tuple(sorted(set(blockers)))


def contract_stage_blockers(
    session: Session,
    settings: Settings,
    project_id: str,
) -> tuple[str, ...]:
    project = session.get(ProjectRow, project_id)
    if project is None:
        return ("project:missing",)
    try:
        rules = require_bound_controlled_version(
            session=session,
            settings=settings,
            project_id=project_id,
            organization_id=project.organization_id,
            purpose="contract_risk_rules",
            kind="contract_risk_rules",
        )
        document_set = require_confirmed_document_set_integrity(
            session=session,
            settings=settings,
            project_id=project_id,
            document_set_revision_id=project.current_document_set_revision_id,
        )
    except (KeyError, LookupError, TypeError, ValueError):
        return ("contract:governance-integrity-failed",)
    contract = rules.payload.get("contract")
    if not isinstance(contract, dict):
        return (f"contract-risk-rules:{rules.id}:invalid",)
    try:
        policy = ContractRequirementsPolicy.model_validate(contract)
    except (TypeError, ValueError):
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
    blockers: list[str] = []
    for kind in sorted(policy.required_term_kinds, key=lambda item: item.value):
        row = terms.get(kind.value)
        if row is None:
            blockers.append(f"contract-term:{kind.value}:missing")
            continue
        field_name = policy.evidence_field_names[kind]
        if session.scalar(
            select(ConflictRow.id).where(
                ConflictRow.project_id == project_id,
                ConflictRow.field_name == field_name,
                ConflictRow.status != VerificationStatus.VERIFIED.value,
            )
        ):
            blockers.append(f"contract-term:{kind.value}:unresolved-conflict")
        try:
            _require_contract_term_integrity(
                session=session,
                row=row,
                policy=policy,
                rules=rules,
                document_revision_ids=frozenset(document_set.revision_ids),
                document_set_revision_id=document_set.id,
            )
        except (KeyError, LookupError, TypeError, ValueError):
            blockers.append(f"contract-term:{kind.value}:integrity-failed")
    return tuple(sorted(set(blockers)))


def _require_contract_term_integrity(
    *,
    session: Session,
    row: ContractTermRow,
    policy: ContractRequirementsPolicy,
    rules: ControlledVersionRow,
    document_revision_ids: frozenset[str],
    document_set_revision_id: str,
) -> None:
    kind = next(item for item in policy.required_term_kinds if item.value == row.kind)
    field_name = policy.evidence_field_names[kind]
    observation_ids = row.payload.get("observation_ids")
    independence_source_ids = row.payload.get("independence_source_ids")
    review_task_id = row.payload.get("approval_task_id")
    created_by = row.payload.get("created_by")
    verified_by = row.payload.get("verified_by")
    if (
        not row.is_current
        or not row.verified
        or not row.cost_impact_resolved
        or not isinstance(observation_ids, list)
        or not observation_ids
        or len(observation_ids) != len(set(observation_ids))
        or not isinstance(independence_source_ids, list)
        or not isinstance(review_task_id, str)
        or not review_task_id
        or not isinstance(created_by, str)
        or not created_by
        or not isinstance(verified_by, str)
        or not verified_by
        or created_by == verified_by
        or row.payload.get("reviewed_by") != verified_by
        or row.payload.get("review_decision") != "APPROVED"
        or row.payload.get("rules_version_id") != rules.id
        or row.payload.get("rules_content_hash") != rules.content_hash
        or row.payload.get("document_set_revision_id") != document_set_revision_id
        or row.payload.get("evidence_field_name") != field_name
        or row.payload.get("review_role") != policy.review_role.value
    ):
        raise ValueError("Verified contract term provenance is invalid")
    observations = list(
        session.scalars(
            select(ObservationRow).where(
                ObservationRow.project_id == row.project_id,
                ObservationRow.id.in_(observation_ids),
            )
        )
    )
    by_id = {item.id: item for item in observations}
    if len(by_id) != len(observation_ids):
        raise ValueError("Contract term evidence is missing")
    ordered = tuple(by_id[item] for item in observation_ids)
    value_hash = content_hash(row.payload.get("value"))
    for evidence in ordered:
        observation = Observation.model_validate(evidence.payload.get("observation"))
        if (
            evidence.id != observation.observation_id
            or evidence.document_revision_id != observation.location.document_revision_id
            or evidence.document_revision_id not in document_revision_ids
            or evidence.field_name != field_name
            or evidence.field_name != observation.field_name
            or evidence.method != observation.method.value
            or evidence.method_version != observation.method_version
            or evidence.status != observation.status.value
            or observation.status
            not in {VerificationStatus.UNVERIFIED, VerificationStatus.VERIFIED}
            or (
                observation.method is EvidenceMethod.MANUAL
                and observation.status is not VerificationStatus.VERIFIED
            )
            or content_hash(observation.value) != value_hash
            or observation.unit is not None
        ):
            raise ValueError("Contract evidence no longer reproduces the term")
    leaves = resolve_observation_leaves(
        session,
        project_id=row.project_id,
        observations=ordered,
    )
    for evidence in leaves:
        observation = Observation.model_validate(evidence.payload.get("observation"))
        if (
            evidence.id != observation.observation_id
            or evidence.project_id != row.project_id
            or evidence.document_revision_id != observation.location.document_revision_id
            or evidence.document_revision_id not in document_revision_ids
            or evidence.field_name != field_name
            or evidence.field_name != observation.field_name
            or evidence.method != observation.method.value
            or evidence.method_version != observation.method_version
            or evidence.status != observation.status.value
            or content_hash(observation.value) != value_hash
            or observation.unit is not None
        ):
            raise ValueError("Contract provenance leaves no longer reproduce the term")
    if kind in policy.independently_verified_term_kinds:
        independent_leaves = require_distinct_qualified_independence(
            session,
            project_id=row.project_id,
            observations=ordered,
        )
        if tuple(independence_source_ids) != independent_leaves or independent_leaves != tuple(
            item.id for item in leaves
        ):
            raise ValueError("Contract independence sources changed")
    review_task = session.get(ApprovalTaskRow, review_task_id)
    if (
        review_task is None
        or review_task.project_id != row.project_id
        or review_task.task_type != "CONTRACT_TERM_REVIEW"
        or review_task.entity_type != "contract_term"
        or review_task.assigned_role != policy.review_role.value
        or not review_task.required
        or review_task.status != "APPROVED"
        or review_task.payload.get("created_by") != created_by
        or review_task.payload.get("observation_ids") != observation_ids
        or review_task.payload.get("independence_source_ids") != independence_source_ids
        or review_task.payload.get("rules_version_id") != rules.id
        or review_task.payload.get("rules_content_hash") != rules.content_hash
        or review_task.payload.get("document_set_revision_id") != document_set_revision_id
        or review_task.payload.get("review_role") != policy.review_role.value
        or not _contract_supersession_chain_contains(
            session,
            current=row,
            ancestor_id=review_task.entity_id,
        )
    ):
        raise ValueError("Contract review task integrity failed")
    expected_submission_hash = content_hash(
        {
            "kind": row.kind,
            "value": row.payload.get("value"),
            "evidence_field_name": field_name,
            "observation_ids": observation_ids,
            "independence_source_ids": independence_source_ids,
            "created_by": created_by,
            "rules_version_id": rules.id,
            "rules_content_hash": rules.content_hash,
            "document_set_revision_id": document_set_revision_id,
            "review_role": policy.review_role.value,
        }
    )
    if review_task.payload.get("term_submission_hash") != expected_submission_hash:
        raise ValueError("Contract review task no longer reproduces its submission")
    approval = session.scalar(
        select(ApprovalRecordRow).where(ApprovalRecordRow.task_id == review_task.id)
    )
    if (
        approval is None
        or approval.decision != "APPROVED"
        or approval.decided_by != verified_by
        or approval.payload.get("term_id") != review_task.entity_id
        or approval.payload.get("kind") != row.kind
        or approval.payload.get("evidence_ids") != observation_ids
        or approval.payload.get("independence_source_ids") != independence_source_ids
        or approval.payload.get("rules_version_id") != rules.id
        or approval.payload.get("rules_content_hash") != rules.content_hash
        or approval.payload.get("document_set_revision_id") != document_set_revision_id
    ):
        raise ValueError("Contract review approval integrity failed")
    decision_event = next(
        (
            event
            for event in session.scalars(
                select(AuditEventRow)
                .where(
                    AuditEventRow.aggregate_type == "project",
                    AuditEventRow.aggregate_id == row.project_id,
                    AuditEventRow.event_type == "contract_term_review_decided",
                )
                .order_by(AuditEventRow.sequence.desc())
            )
            if event.payload.get("term_id") == review_task.entity_id
        ),
        None,
    )
    if (
        decision_event is None
        or decision_event.actor_id != verified_by
        or decision_event.payload.get("approval_id") != approval.id
        or decision_event.payload.get("approval_task_id") != review_task.id
        or decision_event.payload.get("decision") != "APPROVED"
        or decision_event.payload.get("evidence_ids") != observation_ids
        or decision_event.payload.get("rules_version_id") != rules.id
        or decision_event.payload.get("document_set_revision_id") != document_set_revision_id
    ):
        raise ValueError("Contract review audit event integrity failed")
    _require_contract_cost_impact_integrity(session=session, row=row)


def _contract_supersession_chain_contains(
    session: Session,
    *,
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
            session.get(ContractTermRow, candidate.supersedes_term_id)
            if candidate.supersedes_term_id
            else None
        )
    return False


def _require_contract_cost_impact_integrity(
    *,
    session: Session,
    row: ContractTermRow,
) -> None:
    impact = row.payload.get("cost_impact")
    task_ids = row.payload.get("approval_task_ids")
    approval_id = row.payload.get("cost_impact_approval_id")
    proposed_by = row.payload.get("cost_impact_proposed_by")
    approved_by = row.payload.get("cost_impact_approved_by")
    if (
        not isinstance(impact, dict)
        or not isinstance(task_ids, list)
        or not task_ids
        or len(task_ids) != len(set(task_ids))
        or not isinstance(approval_id, str)
        or not approval_id
        or not isinstance(proposed_by, str)
        or not proposed_by
        or not isinstance(approved_by, str)
        or not approved_by
        or proposed_by == approved_by
    ):
        raise ValueError("Contract cost impact provenance is invalid")
    amount = impact.get("amount")
    if amount in {0, "0", "0.0", "0.00"}:
        if (
            not isinstance(impact.get("no_cost_reason"), str)
            or not impact["no_cost_reason"].strip()
            or any(
                impact.get(key) is not None
                for key in (
                    "currency",
                    "cost_component_line_id",
                    "cost_component_semantic_key",
                    "derived_cost_model_id",
                )
            )
        ):
            raise ValueError("Zero contract cost impact is not explicit")
    else:
        model = session.get(CommercialCostModelRow, impact.get("derived_cost_model_id"))
        line = session.get(BoqLineRow, impact.get("cost_component_line_id"))
        components = line.payload.get("cost_components") if line is not None else None
        component = (
            next(
                (
                    item
                    for item in components
                    if isinstance(item, dict)
                    and item.get("semantic_key") == impact.get("cost_component_semantic_key")
                ),
                None,
            )
            if isinstance(components, list)
            else None
        )
        if (
            model is None
            or line is None
            or line.project_id != row.project_id
            or model.project_id != row.project_id
            or model.status != "VALIDATED"
            or not model.is_current
            or model.model_kind != "CONTRACT_FINANCE"
            or model.category != "CONTRACT_FINANCE"
            or model.target_line_id != line.id
            or model.target_semantic_key != impact.get("cost_component_semantic_key")
            or model.document_set_revision_id != row.payload.get("document_set_revision_id")
            or str(model.total) != str(amount)
            or model.currency != impact.get("currency")
            or not isinstance(component, dict)
            or component.get("category") != "CONTRACT_FINANCE"
            or component.get("basis_kind") != "DERIVED_MODEL"
        ):
            raise ValueError("Contract cost impact model no longer reproduces the amount")
    tasks = list(
        session.scalars(
            select(ApprovalTaskRow).where(
                ApprovalTaskRow.project_id == row.project_id,
                ApprovalTaskRow.id.in_(task_ids),
                ApprovalTaskRow.task_type == "CONTRACT_COST_IMPACT",
                ApprovalTaskRow.entity_type == "contract_term",
                ApprovalTaskRow.entity_id == row.id,
                ApprovalTaskRow.status == "APPROVED",
                ApprovalTaskRow.required.is_(True),
            )
        )
    )
    if len(tasks) != len(task_ids) or any(
        task.payload.get("created_by") != proposed_by for task in tasks
    ):
        raise ValueError("Contract cost impact task integrity failed")
    approval = session.get(ApprovalRecordRow, approval_id)
    if (
        approval is None
        or approval.task_id not in task_ids
        or approval.decision != "APPROVED"
        or approval.decided_by != approved_by
        or approval.decided_by == proposed_by
        or not approval.payload.get("evidence_ids")
    ):
        raise ValueError("Contract cost impact approval integrity failed")
    finalized_event = next(
        (
            event
            for event in session.scalars(
                select(AuditEventRow)
                .where(
                    AuditEventRow.aggregate_type == "project",
                    AuditEventRow.aggregate_id == row.project_id,
                    AuditEventRow.event_type == "contract_cost_impact_finalized",
                )
                .order_by(AuditEventRow.sequence.desc())
            )
            if event.payload.get("term_id") == row.id
        ),
        None,
    )
    if (
        finalized_event is None
        or finalized_event.payload.get("approval_record_id") != approval.id
        or finalized_event.payload.get("approved_by") != approved_by
    ):
        raise ValueError("Contract cost impact audit event integrity failed")


def risk_stage_blockers(
    session: Session,
    settings: Settings,
    project_id: str,
) -> tuple[str, ...]:
    project = session.get(ProjectRow, project_id)
    if project is None:
        return ("project:missing",)
    try:
        model = require_bound_controlled_version(
            session=session,
            settings=settings,
            project_id=project_id,
            organization_id=project.organization_id,
            purpose="risk_model",
            kind="risk_model",
        )
        definition = RiskModelDefinition.model_validate(
            {key: value for key, value in model.payload.items() if key != "_governance"}
        )
        document_set = require_confirmed_document_set_integrity(
            session=session,
            settings=settings,
            project_id=project_id,
            document_set_revision_id=project.current_document_set_revision_id,
        )
        reserve_reference = _require_risk_reserve_component(
            session=session,
            project_id=project_id,
            definition=definition,
        )
    except (KeyError, LookupError, TypeError, ValueError):
        return ("risk:governance-integrity-failed",)

    risk_rows = tuple(
        session.scalars(
            select(RiskItemRow)
            .where(
                RiskItemRow.project_id == project_id,
                RiskItemRow.is_current.is_(True),
            )
            .order_by(RiskItemRow.risk_key)
        )
    )
    by_key = {row.risk_key: row for row in risk_rows}
    blockers: list[str] = []
    if len(by_key) != len(risk_rows):
        blockers.append("risk-register:ambiguous-key")
    for risk_key in definition.required_risk_keys:
        if risk_key not in by_key:
            blockers.append(f"risk-item:{risk_key}:missing")
    if len(risk_rows) < definition.minimum_risk_items:
        blockers.append("risk-register:below-approved-minimum")
    risk_item_signatures: list[dict[str, str]] = []
    risks: list[RiskItem] = []
    for row in risk_rows:
        if row.risk_key not in definition.risk_keys:
            blockers.append(f"risk-item:{row.risk_key}:undeclared")
            continue
        try:
            signature, risk = _require_risk_item_integrity(
                session=session,
                row=row,
                definition=definition,
                model=model,
                document_set_revision_id=document_set.id,
                document_revision_ids=frozenset(document_set.revision_ids),
            )
            risk_item_signatures.append(signature)
            risks.append(risk)
        except (KeyError, LookupError, TypeError, ValueError):
            blockers.append(f"risk-item:{row.risk_key}:integrity-failed")
    if blockers:
        return tuple(sorted(set(blockers)))

    policy = definition.calculation_policy(model.id)
    primary = calculate_risk_reserve(tuple(risks), policy)
    independent = _independent_risk_recalculation(tuple(risks), definition, model.id)
    if (
        independent["expected_reserve"] != str(primary.expected_reserve)
        or independent["per_risk_expected_impact"]
        != {key: str(value) for key, value in primary.per_risk_expected_impact.items()}
        or not primary.passed
    ):
        blockers.append("risk-calculation:independent-validation-failed")
    expected_signature = content_hash(
        {
            "risk_items": risk_item_signatures,
            "risk_model_version_id": model.id,
            "risk_model_content_hash": model.content_hash,
            "document_set_revision_id": document_set.id,
            "document_set_manifest_hash": document_set.manifest_hash,
            "reserve_cost_component": reserve_reference,
        }
    )
    expected_output_hash = content_hash(
        {
            "calculation": primary,
            "independent_validation": independent,
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
        return tuple(sorted(set(blockers)))
    if (
        calculation.status != "VALIDATED"
        or calculation.policy_version_id != model.id
        or calculation.expected_reserve != primary.expected_reserve
        or calculation.currency != primary.currency
        or calculation.unit != definition.reserve_unit
        or calculation.payload.get("calculation") != primary.model_dump(mode="json")
        or calculation.payload.get("independent_validation") != independent
        or calculation.payload.get("input_signature") != expected_signature
        or calculation.payload.get("output_hash") != expected_output_hash
        or calculation.payload.get("risk_item_ids") != [row.id for row in risk_rows]
        or calculation.payload.get("risk_item_signatures") != risk_item_signatures
        or calculation.payload.get("risk_model_version_id") != model.id
        or calculation.payload.get("risk_model_content_hash") != model.content_hash
        or calculation.payload.get("document_set_revision_id") != document_set.id
        or calculation.payload.get("document_set_manifest_hash") != document_set.manifest_hash
        or calculation.payload.get("reserve_cost_component") != reserve_reference
        or calculation.payload.get("basis_type") != "RISK_RESERVE"
        or calculation.payload.get("unit_rate") != str(primary.expected_reserve)
        or calculation.payload.get("currency") != primary.currency
        or calculation.payload.get("unit") != definition.reserve_unit
    ):
        blockers.append(f"risk-calculation:{calculation.id}:integrity-failed")
    event = next(
        (
            row
            for row in session.scalars(
                select(AuditEventRow)
                .where(
                    AuditEventRow.aggregate_type == "project",
                    AuditEventRow.aggregate_id == project_id,
                    AuditEventRow.event_type == "risk_reserve_calculated",
                )
                .order_by(AuditEventRow.sequence.desc())
            )
            if row.payload.get("risk_calculation_id") == calculation.id
        ),
        None,
    )
    if (
        event is None
        or event.payload.get("status") != "VALIDATED"
        or event.payload.get("expected_reserve") != str(primary.expected_reserve)
        or event.payload.get("currency") != primary.currency
        or event.payload.get("input_signature") != expected_signature
        or event.payload.get("output_hash") != expected_output_hash
        or event.payload.get("risk_item_ids") != [row.id for row in risk_rows]
        or event.payload.get("risk_model_version_id") != model.id
        or event.payload.get("risk_model_content_hash") != model.content_hash
        or event.payload.get("document_set_revision_id") != document_set.id
        or event.payload.get("independent_validation_passed") is not True
    ):
        blockers.append(f"risk-calculation:{calculation.id}:audit-integrity-failed")
    return tuple(sorted(set(blockers)))


def _require_risk_item_integrity(
    *,
    session: Session,
    row: RiskItemRow,
    definition: RiskModelDefinition,
    model: ControlledVersionRow,
    document_set_revision_id: str,
    document_revision_ids: frozenset[str],
) -> tuple[dict[str, str], RiskItem]:
    risk = RiskItem.model_validate(row.payload.get("risk"))
    evidence_value = row.payload.get("evidence_value")
    observation_ids = row.payload.get("observation_ids")
    independence_source_ids = row.payload.get("independence_source_ids")
    created_by = row.payload.get("created_by")
    verified_by = row.payload.get("verified_by")
    task_id = row.payload.get("approval_task_id")
    expected_evidence_value = {
        "risk_key": row.risk_key,
        **risk.model_dump(
            mode="json",
            exclude={"risk_id", "observation_ids", "status"},
        ),
    }
    if (
        row.status != VerificationStatus.VERIFIED.value
        or not row.is_current
        or risk.risk_id != row.id
        or risk.status is not VerificationStatus.VERIFIED
        or risk.currency != row.currency
        or risk.correlated
        or evidence_value != expected_evidence_value
        or not isinstance(observation_ids, list)
        or not observation_ids
        or len(observation_ids) != len(set(observation_ids))
        or tuple(observation_ids) != risk.observation_ids
        or not isinstance(independence_source_ids, list)
        or not independence_source_ids
        or not isinstance(created_by, str)
        or not created_by
        or not isinstance(verified_by, str)
        or not verified_by
        or created_by == verified_by
        or row.payload.get("reviewed_by") != verified_by
        or row.payload.get("review_decision") != "APPROVED"
        or row.payload.get("risk_model_version_id") != model.id
        or row.payload.get("risk_model_content_hash") != model.content_hash
        or row.payload.get("document_set_revision_id") != document_set_revision_id
        or row.payload.get("review_role") != definition.review_role.value
        or not isinstance(task_id, str)
        or not task_id
    ):
        raise ValueError("Verified risk item provenance is invalid")
    field_name = definition.evidence_field_names[row.risk_key]
    observations = list(
        session.scalars(
            select(ObservationRow).where(
                ObservationRow.project_id == row.project_id,
                ObservationRow.id.in_(observation_ids),
            )
        )
    )
    if len(observations) != len(observation_ids):
        raise ValueError("Risk evidence is missing")
    by_id = {item.id: item for item in observations}
    ordered = tuple(by_id[item] for item in observation_ids)
    expected_hash = content_hash(evidence_value)
    for evidence_row in ordered:
        observation = Observation.model_validate(evidence_row.payload.get("observation"))
        if (
            evidence_row.id != observation.observation_id
            or evidence_row.project_id != row.project_id
            or evidence_row.document_revision_id != observation.location.document_revision_id
            or evidence_row.document_revision_id not in document_revision_ids
            or evidence_row.field_name != field_name
            or evidence_row.field_name != observation.field_name
            or evidence_row.method != observation.method.value
            or evidence_row.method_version != observation.method_version
            or evidence_row.status != observation.status.value
            or observation.status
            not in {VerificationStatus.UNVERIFIED, VerificationStatus.VERIFIED}
            or (
                observation.method is EvidenceMethod.MANUAL
                and observation.status is not VerificationStatus.VERIFIED
            )
            or content_hash(observation.value) != expected_hash
            or observation.unit is not None
        ):
            raise ValueError("Risk evidence no longer reproduces the item")
    leaves = resolve_observation_leaves(
        session,
        project_id=row.project_id,
        observations=ordered,
    )
    for evidence_row in leaves:
        observation = Observation.model_validate(evidence_row.payload.get("observation"))
        if (
            evidence_row.id != observation.observation_id
            or evidence_row.project_id != row.project_id
            or evidence_row.document_revision_id != observation.location.document_revision_id
            or evidence_row.document_revision_id not in document_revision_ids
            or evidence_row.field_name != field_name
            or evidence_row.field_name != observation.field_name
            or evidence_row.method != observation.method.value
            or evidence_row.method_version != observation.method_version
            or evidence_row.status != observation.status.value
            or content_hash(observation.value) != expected_hash
            or observation.unit is not None
        ):
            raise ValueError("Risk evidence leaves no longer reproduce the item")
    leaf_ids = tuple(item.id for item in leaves)
    if tuple(independence_source_ids) != leaf_ids:
        raise ValueError("Risk evidence leaf identity changed")
    if (
        row.risk_key in definition.independently_verified_risk_keys
        and require_distinct_qualified_independence(
            session,
            project_id=row.project_id,
            observations=ordered,
        )
        != leaf_ids
    ):
        raise ValueError("Risk independent evidence changed")
    submission = {
        "risk_item_id": row.id,
        "risk_key": row.risk_key,
        "evidence_value": evidence_value,
        "observation_ids": observation_ids,
        "independence_source_ids": independence_source_ids,
        "created_by": created_by,
        "risk_model_version_id": model.id,
        "risk_model_content_hash": model.content_hash,
        "document_set_revision_id": document_set_revision_id,
        "review_role": definition.review_role.value,
    }
    submission_hash = content_hash(submission)
    expected_task_payload = {
        "created_by": created_by,
        "risk_item_id": row.id,
        "risk_key": row.risk_key,
        "risk_submission_hash": submission_hash,
        "observation_ids": observation_ids,
        "independence_source_ids": independence_source_ids,
        "risk_model_version_id": model.id,
        "risk_model_content_hash": model.content_hash,
        "document_set_revision_id": document_set_revision_id,
        "review_role": definition.review_role.value,
    }
    task = session.get(ApprovalTaskRow, task_id)
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
        raise ValueError("Risk review task integrity failed")
    approval = session.scalar(select(ApprovalRecordRow).where(ApprovalRecordRow.task_id == task.id))
    if (
        approval is None
        or approval.decision != "APPROVED"
        or approval.decided_by != verified_by
        or approval.payload.get("risk_item_id") != row.id
        or approval.payload.get("risk_key") != row.risk_key
        or approval.payload.get("evidence_ids") != observation_ids
        or approval.payload.get("independence_source_ids") != independence_source_ids
        or approval.payload.get("risk_model_version_id") != model.id
        or approval.payload.get("risk_model_content_hash") != model.content_hash
        or approval.payload.get("document_set_revision_id") != document_set_revision_id
        or approval.payload.get("risk_submission_hash") != submission_hash
    ):
        raise ValueError("Risk approval record integrity failed")
    event = next(
        (
            candidate
            for candidate in session.scalars(
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
        or event.payload.get("evidence_ids") != observation_ids
        or event.payload.get("risk_submission_hash") != submission_hash
    ):
        raise ValueError("Risk approval audit event integrity failed")
    updated_at = ensure_utc(row.updated_at)
    if updated_at is None:
        raise ValueError("Risk updated timestamp is missing")
    return (
        {
            "risk_item_id": row.id,
            "risk_key": row.risk_key,
            "risk_submission_hash": submission_hash,
            "updated_at": updated_at.isoformat(),
        },
        risk,
    )


def _require_risk_reserve_component(
    *,
    session: Session,
    project_id: str,
    definition: RiskModelDefinition,
) -> dict[str, str]:
    reference = definition.reserve_cost_component
    line = session.scalar(
        select(BoqLineRow).where(
            BoqLineRow.id == reference.line_id,
            BoqLineRow.project_id == project_id,
            BoqLineRow.is_current.is_(True),
            BoqLineRow.status == VerificationStatus.VERIFIED.value,
        )
    )
    if line is None or line.unit != definition.reserve_unit:
        raise ValueError("Risk reserve BoQ component is missing")
    components = line.payload.get("cost_components")
    component = (
        next(
            (
                item
                for item in components
                if isinstance(item, dict) and item.get("semantic_key") == reference.semantic_key
            ),
            None,
        )
        if isinstance(components, list)
        else None
    )
    if (
        not isinstance(component, dict)
        or component.get("category") != "RISK"
        or component.get("basis_kind") != "RISK_MODEL"
    ):
        raise ValueError("Risk reserve BoQ component is not governed")
    return {
        "line_id": reference.line_id,
        "semantic_key": reference.semantic_key,
    }


def _independent_risk_recalculation(
    risks: tuple[RiskItem, ...],
    definition: RiskModelDefinition,
    policy_version: str,
) -> dict[str, object]:
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
        expected[risk.risk_id] = (
            risk.probability
            * (risk.impact_min + risk.impact_most_likely + risk.impact_max)
            / Decimal(3)
        ).quantize(quantum, rounding=rounding)
    reserve = sum(expected.values(), start=Decimal(0)).quantize(
        quantum,
        rounding=rounding,
    )
    return {
        "validator_version": f"independent:{policy_version}",
        "expected_reserve": str(reserve),
        "currency": policy.currency,
        "per_risk_expected_impact": {key: str(expected[key]) for key in sorted(expected)},
        "passed": len(expected) == len(risks),
    }
