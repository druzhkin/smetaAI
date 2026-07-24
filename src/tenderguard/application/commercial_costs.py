from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from tenderguard.application.approvals import ApprovalService
from tenderguard.application.projects import ProjectService
from tenderguard.config import Settings
from tenderguard.domain.approvals import ApprovalSubject
from tenderguard.domain.commercial_costs import (
    CommercialCostEvaluation,
    CommercialCostModelInput,
    CommercialCostPolicy,
    evaluate_commercial_cost,
)
from tenderguard.domain.common import canonical_data, content_hash, utc_now
from tenderguard.domain.enums import (
    ActorRole,
    ApprovalReason,
    ApprovalState,
    CommercialCostModelKind,
    ContractTermKind,
    CostBasisKind,
    CostCategory,
    Severity,
    VerificationStatus,
    VersionStatus,
)
from tenderguard.domain.models import DomainModel, ValidationFinding
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.object_store import ObjectStore
from tenderguard.infrastructure.orm import (
    ApprovalRecordRow,
    ApprovalTaskRow,
    BoqLineRow,
    CommercialCostModelRow,
    ContractTermRow,
    ControlledVersionRow,
    DocumentSetRevisionRow,
    ObservationRow,
    ProjectControlledVersionRow,
    QuantityRow,
    VerificationFindingRow,
)

_CATEGORY_BY_MODEL = {
    CommercialCostModelKind.LOGISTICS: CostCategory.LOGISTICS,
    CommercialCostModelKind.MOBILISATION: CostCategory.MOBILISATION,
    CommercialCostModelKind.CONTRACT_FINANCE: CostCategory.CONTRACT_FINANCE,
}

_APPROVAL_REASON_BY_MODEL = {
    CommercialCostModelKind.LOGISTICS: ApprovalReason.LOGISTICS_MODEL,
    CommercialCostModelKind.MOBILISATION: ApprovalReason.MOBILISATION_MODEL,
    CommercialCostModelKind.CONTRACT_FINANCE: ApprovalReason.CONTRACT_FINANCE_MODEL,
}


@dataclass(frozen=True)
class _EvidenceClaim:
    model_kind: CommercialCostModelKind
    component_id: str
    observation_ids: tuple[str, ...]
    expected_values: dict[str, Any]


class CommercialCostModelView(DomainModel):
    model_id: str
    model_kind: CommercialCostModelKind
    status: str
    target_line_id: str
    target_semantic_key: str
    category: CostCategory
    policy_version_id: str
    document_set_revision_id: str
    currency: str
    total: Decimal
    independent_total: Decimal
    input_hash: str
    output_hash: str
    approval_task_ids: tuple[str, ...]
    approval_record_ids: tuple[str, ...]
    supersedes_model_id: str | None
    is_current: bool
    created_by: str
    finalized_by: str | None
    model_input: CommercialCostModelInput
    evaluation: CommercialCostEvaluation


class CommercialCostProposalResult(DomainModel):
    model: CommercialCostModelView
    evaluation: CommercialCostEvaluation
    findings: tuple[ValidationFinding, ...]


class CommercialCostService:
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

    def propose(
        self,
        *,
        actor: Actor,
        project_id: str,
        model: CommercialCostModelInput,
        request_id: str,
        reason: str,
    ) -> CommercialCostProposalResult:
        project = self._project_service().get_project(
            actor=actor,
            project_id=project_id,
            lock=True,
            required_roles=(
                ActorRole.ESTIMATOR,
                ActorRole.PROCUREMENT,
                ActorRole.TECHNICAL_EXPERT,
            ),
        )
        self._require_pricing_state(project.state)
        if not project.current_document_set_revision_id:
            raise ValueError("Current document-set revision is required")
        policy, policy_row = self._policy(project.id)
        self._validate_target(project.id, model)
        self._validate_observations(
            project.id,
            project.current_document_set_revision_id,
            model,
        )
        self._validate_contract_terms(project.id, model, policy)
        pending = self.session.scalar(
            select(CommercialCostModelRow).where(
                CommercialCostModelRow.project_id == project.id,
                CommercialCostModelRow.target_line_id == model.target_line_id,
                CommercialCostModelRow.target_semantic_key == model.target_semantic_key,
                CommercialCostModelRow.status == "REVIEW_REQUIRED",
            )
        )
        if pending is not None:
            terminal_review = self.session.scalar(
                select(ApprovalTaskRow.status).where(
                    ApprovalTaskRow.project_id == project.id,
                    ApprovalTaskRow.id.in_(pending.approval_task_ids),
                    ApprovalTaskRow.status.in_(("REJECTED", "CHANGES_REQUESTED")),
                )
            )
            if terminal_review is None:
                raise ValueError(
                    "A commercial cost model for this target is already awaiting review"
                )
            pending.status = "BLOCKED"
            self.session.flush()
            self._project_service().record_event(
                aggregate_type="project",
                aggregate_id=project.id,
                event_type="commercial_cost_model_blocked_by_review",
                actor=actor,
                request_id=request_id,
                reason=reason,
                payload={
                    "model_id": pending.id,
                    "target_line_id": pending.target_line_id,
                    "target_semantic_key": pending.target_semantic_key,
                    "approval_status": terminal_review,
                },
            )
        current = self.session.scalar(
            select(CommercialCostModelRow)
            .where(
                CommercialCostModelRow.project_id == project.id,
                CommercialCostModelRow.target_line_id == model.target_line_id,
                CommercialCostModelRow.target_semantic_key == model.target_semantic_key,
                CommercialCostModelRow.is_current.is_(True),
            )
            .with_for_update()
        )
        evaluation = evaluate_commercial_cost(
            model,
            policy,
            engine_version=f"commercial-primary:{policy_row.id}",
            validator_version=f"commercial-independent:{policy_row.id}",
        )
        model_id = f"commercial-cost-{uuid4()}"
        findings = list(evaluation.independent.findings)
        task_ids: tuple[str, ...] = ()
        if evaluation.independent.passed:
            approval = self._approval_service().plan(
                actor=actor,
                project_id=project.id,
                subjects=(
                    ApprovalSubject(
                        entity_type="commercial_cost_model",
                        entity_id=model_id,
                        reasons=frozenset({_APPROVAL_REASON_BY_MODEL[model.model_kind]}),
                        monetary_value=evaluation.primary.total,
                    ),
                ),
                request_id=request_id,
                reason=reason,
            )
            findings.extend(approval.plan.findings)
            task_ids = tuple(approval.task_ids_by_key.values())
        status = (
            "REVIEW_REQUIRED"
            if evaluation.independent.passed and not findings and task_ids
            else "BLOCKED"
        )
        now = utc_now()
        output_hash = content_hash(
            {
                "primary": evaluation.primary,
                "independent": evaluation.independent,
            }
        )
        row = CommercialCostModelRow(
            id=model_id,
            project_id=project.id,
            model_kind=model.model_kind.value,
            status=status,
            target_line_id=model.target_line_id,
            target_semantic_key=model.target_semantic_key,
            category=_CATEGORY_BY_MODEL[model.model_kind].value,
            policy_version_id=policy_row.id,
            document_set_revision_id=project.current_document_set_revision_id,
            currency=model.currency,
            total=evaluation.primary.total,
            independent_total=evaluation.independent.independently_calculated_total,
            input_hash=evaluation.input_hash,
            output_hash=output_hash,
            payload=canonical_data(
                {
                    "basis_type": "DERIVED_COMMERCIAL_COST",
                    "unit": self._target_unit(model.target_line_id),
                    "unit_rate": evaluation.primary.total,
                    "currency": model.currency,
                    "model": model,
                    "policy": policy,
                    "evaluation": evaluation,
                    "observation_ids": list(model.observation_ids),
                }
            ),
            approval_task_ids=list(task_ids),
            approval_record_ids=None,
            supersedes_model_id=current.id if current is not None else None,
            is_current=False,
            created_by=actor.actor_id,
            finalized_by=None,
            created_at=now,
            finalized_at=None,
        )
        self.session.add(row)
        self._replace_findings(
            project.id,
            model.target_line_id,
            model.target_semantic_key,
            model_id,
            tuple(findings),
        )
        self._project_service().record_event(
            aggregate_type="project",
            aggregate_id=project.id,
            event_type="commercial_cost_model_proposed",
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload={
                "model_id": model_id,
                "model_kind": model.model_kind,
                "target_line_id": model.target_line_id,
                "target_semantic_key": model.target_semantic_key,
                "policy_version_id": policy_row.id,
                "document_set_revision_id": project.current_document_set_revision_id,
                "input_hash": evaluation.input_hash,
                "output_hash": output_hash,
                "total": evaluation.primary.total,
                "currency": model.currency,
                "independent_validation_passed": evaluation.independent.passed,
                "approval_task_ids": list(task_ids),
                "status": status,
            },
        )
        return CommercialCostProposalResult(
            model=self._view(row),
            evaluation=evaluation,
            findings=tuple(findings),
        )

    def finalize(
        self,
        *,
        actor: Actor,
        project_id: str,
        model_id: str,
        request_id: str,
        reason: str,
    ) -> CommercialCostModelView:
        project = self._project_service().get_project(
            actor=actor,
            project_id=project_id,
            lock=True,
            required_roles=(
                ActorRole.ESTIMATOR,
                ActorRole.PROCUREMENT,
                ActorRole.TECHNICAL_EXPERT,
                ActorRole.REVIEWER,
                ActorRole.APPROVER,
            ),
        )
        self._require_pricing_state(project.state)
        row = self.session.scalar(
            select(CommercialCostModelRow)
            .where(
                CommercialCostModelRow.id == model_id,
                CommercialCostModelRow.project_id == project.id,
            )
            .with_for_update()
        )
        if row is None:
            raise LookupError(model_id)
        if row.status != "REVIEW_REQUIRED":
            raise ValueError("Only a REVIEW_REQUIRED commercial cost model can be finalized")
        if project.current_document_set_revision_id != row.document_set_revision_id:
            raise ValueError("Commercial cost model belongs to a superseded document set")
        model = CommercialCostModelInput.model_validate(row.payload.get("model"))
        stored_policy = CommercialCostPolicy.model_validate(row.payload.get("policy"))
        current_policy, policy_row = self._policy(project.id)
        if (
            row.policy_version_id != policy_row.id
            or stored_policy != current_policy
            or stored_policy.policy_version != policy_row.id
        ):
            raise ValueError("Commercial cost model policy is superseded or changed")
        self._validate_target(project.id, model)
        self._validate_observations(
            project.id,
            row.document_set_revision_id,
            model,
        )
        self._validate_contract_terms(project.id, model, current_policy)
        evaluation = evaluate_commercial_cost(
            model,
            current_policy,
            engine_version=f"commercial-primary:{policy_row.id}",
            validator_version=f"commercial-independent:{policy_row.id}",
        )
        expected_output_hash = content_hash(
            {
                "primary": evaluation.primary,
                "independent": evaluation.independent,
            }
        )
        if (
            not evaluation.independent.passed
            or evaluation.input_hash != row.input_hash
            or expected_output_hash != row.output_hash
            or evaluation.primary.total != row.total
            or evaluation.independent.independently_calculated_total != row.independent_total
        ):
            raise ValueError("Commercial cost model no longer reproduces its stored result")
        approval_records = self._approved_records(project.id, row)
        prior = self.session.scalar(
            select(CommercialCostModelRow)
            .where(
                CommercialCostModelRow.project_id == project.id,
                CommercialCostModelRow.target_line_id == row.target_line_id,
                CommercialCostModelRow.target_semantic_key == row.target_semantic_key,
                CommercialCostModelRow.is_current.is_(True),
            )
            .with_for_update()
        )
        if prior is not None:
            if row.supersedes_model_id != prior.id:
                raise ValueError("Commercial cost model supersession target changed")
            prior.is_current = False
            self.session.flush()
        elif row.supersedes_model_id is not None:
            raise ValueError("Superseded commercial cost model is no longer current")
        now = utc_now()
        row.status = "VALIDATED"
        row.approval_record_ids = [item.id for item in approval_records]
        row.is_current = True
        row.finalized_by = actor.actor_id
        row.finalized_at = now
        self.session.flush()
        self._replace_findings(
            project.id,
            row.target_line_id,
            row.target_semantic_key,
            row.id,
            (),
        )
        self._project_service().record_event(
            aggregate_type="project",
            aggregate_id=project.id,
            event_type="commercial_cost_model_finalized",
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload={
                "model_id": row.id,
                "model_kind": row.model_kind,
                "target_line_id": row.target_line_id,
                "target_semantic_key": row.target_semantic_key,
                "input_hash": row.input_hash,
                "output_hash": row.output_hash,
                "total": row.total,
                "currency": row.currency,
                "approval_record_ids": list(row.approval_record_ids),
                "supersedes_model_id": row.supersedes_model_id,
            },
        )
        return self._view(row)

    def get(
        self,
        *,
        actor: Actor,
        project_id: str,
        model_id: str,
    ) -> CommercialCostModelView:
        self._project_service().get_project(
            actor=actor,
            project_id=project_id,
            required_roles=(
                ActorRole.ESTIMATOR,
                ActorRole.PROCUREMENT,
                ActorRole.TECHNICAL_EXPERT,
                ActorRole.REVIEWER,
                ActorRole.APPROVER,
                ActorRole.AUDITOR,
            ),
        )
        row = self.session.scalar(
            select(CommercialCostModelRow).where(
                CommercialCostModelRow.id == model_id,
                CommercialCostModelRow.project_id == project_id,
            )
        )
        if row is None:
            raise LookupError(model_id)
        return self._view(row)

    def _policy(
        self,
        project_id: str,
    ) -> tuple[CommercialCostPolicy, ControlledVersionRow]:
        row = self.session.scalar(
            select(ControlledVersionRow)
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
        if row is None:
            raise ValueError("A bound approved commercial_cost_model version is required")
        return (
            CommercialCostPolicy.model_validate(
                {
                    "policy_version": row.id,
                    **self._policy_payload(row),
                }
            ),
            row,
        )

    @staticmethod
    def _policy_payload(row: ControlledVersionRow) -> dict[str, Any]:
        payload = row.payload.get("policy")
        if not isinstance(payload, dict):
            raise ValueError("Commercial cost model version lacks a policy object")
        return payload

    def _validate_target(
        self,
        project_id: str,
        model: CommercialCostModelInput,
    ) -> None:
        line = self.session.scalar(
            select(BoqLineRow).where(
                BoqLineRow.id == model.target_line_id,
                BoqLineRow.project_id == project_id,
                BoqLineRow.status == VerificationStatus.VERIFIED.value,
                BoqLineRow.is_current.is_(True),
            )
        )
        if line is None:
            raise ValueError("Commercial cost target is not a current verified BoQ line")
        raw_components = line.payload.get("cost_components")
        component = (
            next(
                (
                    item
                    for item in raw_components
                    if isinstance(item, dict)
                    and item.get("semantic_key") == model.target_semantic_key
                ),
                None,
            )
            if isinstance(raw_components, list)
            else None
        )
        expected_category = _CATEGORY_BY_MODEL[model.model_kind]
        if (
            not isinstance(component, dict)
            or component.get("basis_kind") != CostBasisKind.DERIVED_MODEL.value
            or component.get("category") != expected_category.value
        ):
            raise ValueError(
                "Commercial cost target must be a matching DERIVED_MODEL BoQ component"
            )
        quantity = self.session.scalar(
            select(QuantityRow).where(
                QuantityRow.boq_line_id == line.id,
                QuantityRow.status == VerificationStatus.VERIFIED.value,
                QuantityRow.is_current.is_(True),
            )
        )
        if quantity is None or quantity.value != Decimal("1"):
            raise ValueError(
                "Derived commercial cost target requires a verified lump-sum quantity of 1"
            )

    def _target_unit(self, line_id: str) -> str:
        quantity = self.session.scalar(
            select(QuantityRow).where(
                QuantityRow.boq_line_id == line_id,
                QuantityRow.status == VerificationStatus.VERIFIED.value,
                QuantityRow.is_current.is_(True),
            )
        )
        if quantity is None:
            raise ValueError("Commercial cost target quantity is missing")
        return quantity.unit

    def _validate_observations(
        self,
        project_id: str,
        document_set_revision_id: str,
        model: CommercialCostModelInput,
    ) -> None:
        observation_ids = model.observation_ids
        if not observation_ids:
            raise ValueError("Commercial cost model requires numeric evidence observations")
        document_set = self.session.scalar(
            select(DocumentSetRevisionRow).where(
                DocumentSetRevisionRow.id == document_set_revision_id,
                DocumentSetRevisionRow.project_id == project_id,
                DocumentSetRevisionRow.status == "CONFIRMED",
            )
        )
        if document_set is None:
            raise ValueError("Commercial cost model document set is not confirmed")
        rows = tuple(
            self.session.scalars(
                select(ObservationRow).where(
                    ObservationRow.project_id == project_id,
                    ObservationRow.id.in_(observation_ids),
                    ObservationRow.status == VerificationStatus.VERIFIED.value,
                )
            )
        )
        if len(rows) != len(set(observation_ids)):
            raise ValueError("Commercial cost model references missing or unverified observations")
        revision_ids = set(document_set.revision_ids)
        if any(row.document_revision_id not in revision_ids for row in rows):
            raise ValueError("Commercial cost model evidence is outside the confirmed document set")
        self._validate_observation_values(model, rows)

    @staticmethod
    def _validate_observation_values(
        model: CommercialCostModelInput,
        rows: tuple[ObservationRow, ...],
    ) -> None:
        by_id = {row.id: row for row in rows}
        for claim in _evidence_claims(model):
            observed: dict[str, Any] = {}
            for observation_id in claim.observation_ids:
                row = by_id[observation_id]
                raw_bases = row.payload.get("commercial_cost_bases")
                if not isinstance(raw_bases, list):
                    raise ValueError(f"Observation {observation_id} has no commercial cost basis")
                for raw_basis in raw_bases:
                    if (
                        not isinstance(raw_basis, dict)
                        or raw_basis.get("model_kind") != claim.model_kind.value
                        or raw_basis.get("component_id") != claim.component_id
                    ):
                        continue
                    raw_values = raw_basis.get("values")
                    if not isinstance(raw_values, dict):
                        raise ValueError(
                            f"Observation {observation_id} has an invalid basis value map"
                        )
                    for name, value in raw_values.items():
                        if name in observed and canonical_data(observed[name]) != canonical_data(
                            value
                        ):
                            raise ValueError(
                                f"Commercial evidence conflicts for {claim.component_id}.{name}"
                            )
                        observed[name] = value
            for name, expected in claim.expected_values.items():
                if name not in observed or canonical_data(observed[name]) != canonical_data(
                    expected
                ):
                    raise ValueError(
                        f"Commercial evidence does not reproduce {claim.component_id}.{name}"
                    )

    def _validate_contract_terms(
        self,
        project_id: str,
        model: CommercialCostModelInput,
        policy: CommercialCostPolicy,
    ) -> None:
        related_ids = set(model.related_contract_term_ids)
        nested_ids: set[str] = set()
        if model.contract_finance is not None:
            for cash_flow in model.contract_finance.cash_flows:
                nested_ids.update(cash_flow.contract_term_ids)
            for guarantee_fee in model.contract_finance.guarantee_fees:
                nested_ids.update(guarantee_fee.contract_term_ids)
        if not nested_ids.issubset(related_ids):
            raise ValueError(
                "Cash-flow or guarantee term references are absent from model-level lineage"
            )
        if not related_ids and (
            model.model_kind is CommercialCostModelKind.CONTRACT_FINANCE
            and policy.required_contract_term_kinds
        ):
            raise ValueError("Contract finance model lacks required contract term lineage")
        rows = tuple(
            self.session.scalars(
                select(ContractTermRow).where(
                    ContractTermRow.project_id == project_id,
                    ContractTermRow.id.in_(related_ids),
                    ContractTermRow.is_current.is_(True),
                    ContractTermRow.verified.is_(True),
                )
            )
        )
        if len(rows) != len(related_ids):
            raise ValueError(
                "Commercial cost model references missing, superseded, or unverified terms"
            )
        if model.model_kind is CommercialCostModelKind.CONTRACT_FINANCE:
            present_kinds = {ContractTermKind(row.kind) for row in rows}
            missing = policy.required_contract_term_kinds - present_kinds
            if missing:
                raise ValueError(
                    "Contract finance model lacks required verified terms: "
                    + ", ".join(sorted(item.value for item in missing))
                )

    def _approved_records(
        self,
        project_id: str,
        row: CommercialCostModelRow,
    ) -> tuple[ApprovalRecordRow, ...]:
        task_ids = tuple(row.approval_task_ids)
        if not task_ids:
            raise ValueError("Commercial cost model has no approval task")
        tasks = tuple(
            self.session.scalars(
                select(ApprovalTaskRow).where(
                    ApprovalTaskRow.project_id == project_id,
                    ApprovalTaskRow.id.in_(task_ids),
                    ApprovalTaskRow.entity_type == "commercial_cost_model",
                    ApprovalTaskRow.entity_id == row.id,
                    ApprovalTaskRow.status == "APPROVED",
                )
            )
        )
        if len(tasks) != len(task_ids):
            raise ValueError("Commercial cost model approvals are incomplete")
        records: list[ApprovalRecordRow] = []
        for task_id in task_ids:
            record = self.session.scalar(
                select(ApprovalRecordRow)
                .where(
                    ApprovalRecordRow.task_id == task_id,
                    ApprovalRecordRow.decision == "APPROVED",
                )
                .order_by(ApprovalRecordRow.decided_at.desc())
            )
            if record is None:
                raise ValueError("Approved commercial cost task has no approval record")
            if record.decided_by == row.created_by:
                raise ValueError("Commercial cost model violates four-eyes approval")
            records.append(record)
        return tuple(records)

    def _replace_findings(
        self,
        project_id: str,
        target_line_id: str,
        target_semantic_key: str,
        model_id: str,
        findings: tuple[ValidationFinding, ...],
    ) -> None:
        now = utc_now()
        prior = tuple(
            self.session.scalars(
                select(VerificationFindingRow).where(
                    VerificationFindingRow.project_id == project_id,
                    VerificationFindingRow.contour == "COMMERCIAL_COST",
                    VerificationFindingRow.resolved.is_(False),
                )
            )
        )
        for item in prior:
            target = item.payload.get("target")
            if target != {
                "line_id": target_line_id,
                "semantic_key": target_semantic_key,
            }:
                continue
            item.resolved = True
            item.updated_at = now
            item.payload = {
                **item.payload,
                "resolved_by_model_id": model_id,
                "resolved_at": now.isoformat(),
            }
        for finding in findings:
            identity = {
                "project_id": project_id,
                "model_id": model_id,
                "finding": finding,
            }
            finding_id = f"finding-{content_hash(identity)[:24]}"
            self.session.add(
                VerificationFindingRow(
                    id=finding_id,
                    project_id=project_id,
                    contour="COMMERCIAL_COST",
                    code=finding.code.value,
                    severity=Severity.BLOCKER.value,
                    resolved=False,
                    payload={
                        **finding.model_dump(mode="json"),
                        "model_id": model_id,
                        "target": {
                            "line_id": target_line_id,
                            "semantic_key": target_semantic_key,
                        },
                    },
                    created_at=now,
                    updated_at=now,
                )
            )

    @staticmethod
    def _require_pricing_state(state: str) -> None:
        if ApprovalState(state) not in {
            ApprovalState.PRICING_IN_PROGRESS,
            ApprovalState.RFQ_REQUIRED,
        }:
            raise ValueError("Commercial cost models may be changed only during pricing")

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
    def _view(row: CommercialCostModelRow) -> CommercialCostModelView:
        return CommercialCostModelView(
            model_id=row.id,
            model_kind=CommercialCostModelKind(row.model_kind),
            status=row.status,
            target_line_id=row.target_line_id,
            target_semantic_key=row.target_semantic_key,
            category=CostCategory(row.category),
            policy_version_id=row.policy_version_id,
            document_set_revision_id=row.document_set_revision_id,
            currency=row.currency,
            total=row.total,
            independent_total=row.independent_total,
            input_hash=row.input_hash,
            output_hash=row.output_hash,
            approval_task_ids=tuple(row.approval_task_ids),
            approval_record_ids=tuple(row.approval_record_ids or []),
            supersedes_model_id=row.supersedes_model_id,
            is_current=row.is_current,
            created_by=row.created_by,
            finalized_by=row.finalized_by,
            model_input=CommercialCostModelInput.model_validate(row.payload.get("model")),
            evaluation=CommercialCostEvaluation.model_validate(row.payload.get("evaluation")),
        )


def _evidence_claims(model: CommercialCostModelInput) -> tuple[_EvidenceClaim, ...]:
    claims: list[_EvidenceClaim] = []
    if model.logistics is not None:
        for leg in model.logistics.transport_legs:
            claims.extend(
                (
                    _EvidenceClaim(
                        model_kind=model.model_kind,
                        component_id=leg.component_id,
                        observation_ids=leg.route_observation_ids,
                        expected_values={
                            "mode": leg.mode,
                            "origin": leg.origin,
                            "destination": leg.destination,
                            "distance_km": leg.distance_km,
                            "charged_distance_factor": leg.charged_distance_factor,
                        },
                    ),
                    _EvidenceClaim(
                        model_kind=model.model_kind,
                        component_id=leg.component_id,
                        observation_ids=leg.cargo_observation_ids,
                        expected_values={
                            name: value
                            for name, value in {
                                "cargo_mass_tonnes": leg.cargo_mass_tonnes,
                                "vehicle_mass_capacity_tonnes": (leg.vehicle_mass_capacity_tonnes),
                                "cargo_volume_m3": leg.cargo_volume_m3,
                                "vehicle_volume_capacity_m3": (leg.vehicle_volume_capacity_m3),
                                "cargo_units": leg.cargo_units,
                                "vehicle_unit_capacity": leg.vehicle_unit_capacity,
                            }.items()
                            if value is not None
                        },
                    ),
                    _EvidenceClaim(
                        model_kind=model.model_kind,
                        component_id=leg.component_id,
                        observation_ids=leg.rate_observation_ids,
                        expected_values={
                            "fixed_cost_per_trip": leg.fixed_cost_per_trip,
                            "rate_per_vehicle_km": leg.rate_per_vehicle_km,
                            "toll_per_trip": leg.toll_per_trip,
                            "currency": model.currency,
                        },
                    ),
                )
            )
        for handling in model.logistics.handling_costs:
            claims.append(
                _EvidenceClaim(
                    model_kind=model.model_kind,
                    component_id=handling.component_id,
                    observation_ids=handling.observation_ids,
                    expected_values={
                        "kind": handling.kind,
                        "quantity": handling.quantity,
                        "unit": handling.unit,
                        "operation_count": handling.operation_count,
                        "unit_rate": handling.unit_rate,
                        "currency": model.currency,
                    },
                )
            )
        for storage in model.logistics.storage_costs:
            claims.append(
                _EvidenceClaim(
                    model_kind=model.model_kind,
                    component_id=storage.component_id,
                    observation_ids=storage.observation_ids,
                    expected_values={
                        "quantity": storage.quantity,
                        "unit": storage.unit,
                        "duration_days": storage.duration_days,
                        "rate_per_unit_day": storage.rate_per_unit_day,
                        "currency": model.currency,
                    },
                )
            )
        for ancillary in model.logistics.ancillary_costs:
            claims.append(
                _EvidenceClaim(
                    model_kind=model.model_kind,
                    component_id=ancillary.component_id,
                    observation_ids=ancillary.observation_ids,
                    expected_values={
                        "kind": ancillary.kind,
                        "quantity": ancillary.quantity,
                        "unit": ancillary.unit,
                        "unit_rate": ancillary.unit_rate,
                        "currency": model.currency,
                    },
                )
            )
    elif model.mobilisation is not None:
        for mobilisation in model.mobilisation.components:
            claims.append(
                _EvidenceClaim(
                    model_kind=model.model_kind,
                    component_id=mobilisation.component_id,
                    observation_ids=mobilisation.observation_ids,
                    expected_values={
                        "kind": mobilisation.kind,
                        "description": mobilisation.description,
                        "quantity": mobilisation.quantity,
                        "unit": mobilisation.unit,
                        "occurrence_count": mobilisation.occurrence_count,
                        "duration_days": mobilisation.duration_days,
                        "unit_rate": mobilisation.unit_rate,
                        "currency": model.currency,
                    },
                )
            )
    elif model.contract_finance is not None:
        for cash_flow in model.contract_finance.cash_flows:
            claims.append(
                _EvidenceClaim(
                    model_kind=model.model_kind,
                    component_id=cash_flow.cash_flow_id,
                    observation_ids=cash_flow.observation_ids,
                    expected_values={
                        "kind": cash_flow.kind,
                        "cash_date": cash_flow.cash_date,
                        "amount": cash_flow.amount,
                        "currency": model.currency,
                        "contract_term_ids": list(cash_flow.contract_term_ids),
                    },
                )
            )
        for rate_period in model.contract_finance.funding_rate_periods:
            claims.append(
                _EvidenceClaim(
                    model_kind=model.model_kind,
                    component_id=rate_period.rate_period_id,
                    observation_ids=rate_period.observation_ids,
                    expected_values={
                        "starts_on": rate_period.starts_on,
                        "ends_on": rate_period.ends_on,
                        "annual_rate": rate_period.annual_rate,
                    },
                )
            )
        for guarantee in model.contract_finance.guarantee_fees:
            claims.append(
                _EvidenceClaim(
                    model_kind=model.model_kind,
                    component_id=guarantee.guarantee_id,
                    observation_ids=guarantee.observation_ids,
                    expected_values={
                        "kind": guarantee.kind,
                        "notional_amount": guarantee.notional_amount,
                        "annual_rate": guarantee.annual_rate,
                        "starts_on": guarantee.starts_on,
                        "ends_on": guarantee.ends_on,
                        "currency": model.currency,
                        "contract_term_ids": list(guarantee.contract_term_ids),
                    },
                )
            )
    return tuple(claims)
