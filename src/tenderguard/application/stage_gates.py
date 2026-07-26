from __future__ import annotations

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
from tenderguard.domain.common import content_hash
from tenderguard.domain.enums import (
    CostBasisKind,
    EvidenceMethod,
    Severity,
    VerificationStatus,
    VersionStatus,
)
from tenderguard.domain.models import Observation
from tenderguard.domain.passport import PassportRequirementsPolicy
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
