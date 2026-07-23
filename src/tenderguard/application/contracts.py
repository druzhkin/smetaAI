from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

from pydantic import Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from tenderguard.application.approvals import ApprovalService
from tenderguard.application.evidence_independence import (
    require_distinct_qualified_independence,
)
from tenderguard.application.projects import ProjectService
from tenderguard.config import Settings
from tenderguard.domain.approvals import ApprovalSubject
from tenderguard.domain.common import content_hash, utc_now
from tenderguard.domain.contract import (
    ContractAssessment,
    ContractTerm,
    validate_contract,
)
from tenderguard.domain.enums import (
    ActorRole,
    ApprovalReason,
    ApprovalState,
    ContractTermKind,
    CostCategory,
    Severity,
    VersionStatus,
)
from tenderguard.domain.models import DomainModel, ValidationFinding
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.object_store import ObjectStore
from tenderguard.infrastructure.orm import (
    ApprovalRecordRow,
    ApprovalTaskRow,
    BoqLineRow,
    ContractTermRow,
    ControlledVersionRow,
    ObservationRow,
    ProjectControlledVersionRow,
    VerificationFindingRow,
)


class ContractTermDraft(DomainModel):
    kind: ContractTermKind
    value: str = Field(min_length=1)
    observation_ids: tuple[str, ...] = Field(min_length=1)


class ContractCostImpactCommand(DomainModel):
    amount: Decimal = Field(ge=0)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    cost_component_line_id: str | None = None
    cost_component_semantic_key: str | None = None
    no_cost_reason: str | None = None

    @model_validator(mode="after")
    def resolution_is_explicit(self) -> ContractCostImpactCommand:
        if self.amount == 0:
            if not self.no_cost_reason:
                raise ValueError("Zero contract cost impact requires an explicit reason")
            if self.cost_component_line_id or self.cost_component_semantic_key:
                raise ValueError("Zero contract cost impact cannot reference a cost component")
        elif not (
            self.currency and self.cost_component_line_id and self.cost_component_semantic_key
        ):
            raise ValueError(
                "Non-zero contract impact requires currency and a planned cost component"
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
    approval_task_ids: tuple[str, ...] = ()


class ContractValidationResult(DomainModel):
    assessment: ContractAssessment
    findings: tuple[ValidationFinding, ...]
    rules_version_id: str


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

    def submit_term(
        self,
        *,
        actor: Actor,
        project_id: str,
        draft: ContractTermDraft,
        request_id: str,
        reason: str,
    ) -> ContractTermView:
        actor.require_any(ActorRole.TECHNICAL_EXPERT, ActorRole.REVIEWER)
        project = self._require_editable_state(actor, project_id)
        observations = self._observations(project.id, draft.observation_ids)
        self._validate_observation_values(draft, observations)
        requirements, rules = self._requirements(project.id)
        if draft.kind in requirements["independently_verified_term_kinds"]:
            require_distinct_qualified_independence(
                self.session,
                project_id=project.id,
                observations=observations,
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
        if previous is not None:
            previous.is_current = False
            previous.updated_at = now
        row = ContractTermRow(
            id=f"contract-term-{uuid4()}",
            project_id=project.id,
            kind=draft.kind.value,
            verified=False,
            cost_impact_resolved=False,
            supersedes_term_id=previous.id if previous else None,
            is_current=True,
            payload={
                **draft.model_dump(mode="json"),
                "created_by": actor.actor_id,
                "rules_version_id": rules.id,
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
            "contract_term_submitted",
            {
                "term_id": row.id,
                "kind": draft.kind,
                "value_hash": content_hash(draft.value),
                "observation_ids": list(draft.observation_ids),
                "rules_version_id": rules.id,
                "supersedes_term_id": row.supersedes_term_id,
            },
        )
        return self._view(row)

    def verify_term(
        self,
        *,
        actor: Actor,
        project_id: str,
        term_id: str,
        request_id: str,
        reason: str,
    ) -> tuple[ContractTermView, ContractValidationResult]:
        actor.require_any(ActorRole.TECHNICAL_EXPERT, ActorRole.REVIEWER)
        project = self._require_editable_state(actor, project_id)
        row = self._current_term(project.id, term_id)
        if row.verified:
            raise ValueError("Only an unverified current contract term can be verified")
        if row.payload.get("document_set_revision_id") != project.current_document_set_revision_id:
            raise ValueError("Contract term belongs to a superseded document-set revision")
        if row.payload.get("created_by") == actor.actor_id:
            raise ValueError("Contract term verification requires a different actor")
        draft = ContractTermDraft.model_validate(
            {
                "kind": row.kind,
                "value": row.payload.get("value"),
                "observation_ids": row.payload.get("observation_ids"),
            }
        )
        observations = self._observations(project.id, draft.observation_ids)
        self._validate_observation_values(draft, observations)
        requirements, rules = self._requirements(project.id)
        if row.payload.get("rules_version_id") != rules.id:
            raise ValueError("Contract term was prepared under a superseded rule version")
        if draft.kind in requirements["independently_verified_term_kinds"]:
            require_distinct_qualified_independence(
                self.session,
                project_id=project.id,
                observations=observations,
            )
        now = utc_now()
        row.verified = True
        row.updated_at = now
        row.payload = {
            **row.payload,
            "verified_by": actor.actor_id,
            "verified_at": now.isoformat(),
        }
        validation = self._validate_current(project.id)
        self._audit(
            project.id,
            actor,
            request_id,
            reason,
            "contract_term_verified",
            {
                "term_id": row.id,
                "kind": row.kind,
                "remaining_findings": [
                    finding.model_dump(mode="json") for finding in validation.findings
                ],
            },
        )
        return self._view(row), validation

    def propose_cost_impact(
        self,
        *,
        actor: Actor,
        project_id: str,
        term_id: str,
        command: ContractCostImpactCommand,
        request_id: str,
        reason: str,
    ) -> ContractTermView:
        actor.require_any(ActorRole.ESTIMATOR, ActorRole.TECHNICAL_EXPERT, ActorRole.ADMIN)
        project = self._require_editable_state(actor, project_id)
        source = self._current_term(project.id, term_id)
        if not source.verified:
            raise ValueError("Contract cost impact requires a verified term")
        if command.amount > 0:
            self._validate_contract_cost_component(project.id, command)
        now = utc_now()
        source.is_current = False
        source.updated_at = now
        row = ContractTermRow(
            id=f"contract-term-{uuid4()}",
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
            },
        )
        return self._view(row)

    def finalize_cost_impact(
        self,
        *,
        actor: Actor,
        project_id: str,
        term_id: str,
        request_id: str,
        reason: str,
    ) -> tuple[ContractTermView, ContractValidationResult]:
        actor.require_any(
            ActorRole.ESTIMATOR,
            ActorRole.TECHNICAL_EXPERT,
            ActorRole.REVIEWER,
            ActorRole.ADMIN,
        )
        project = self._require_editable_state(actor, project_id)
        row = self._current_term(project.id, term_id)
        if row.cost_impact_resolved:
            raise ValueError("Contract cost impact is already resolved")
        proposal = row.payload.get("cost_impact_proposal")
        if not isinstance(proposal, dict):
            raise ValueError("Contract term has no cost impact proposal")
        task_ids = tuple(row.payload.get("approval_task_ids", []))
        if not task_ids:
            raise ValueError("Contract cost impact has no approval task")
        tasks = list(
            self.session.scalars(
                select(ApprovalTaskRow).where(
                    ApprovalTaskRow.project_id == project.id,
                    ApprovalTaskRow.id.in_(task_ids),
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
        validation = self._validate_current(project.id)
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
        actor.require_any(
            ActorRole.ESTIMATOR,
            ActorRole.TECHNICAL_EXPERT,
            ActorRole.REVIEWER,
            ActorRole.AUDITOR,
            ActorRole.ADMIN,
        )
        self._project_service().get_project(
            actor=actor,
            project_id=project_id,
            lock=True,
        )
        result = self._validate_current(project_id)
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

    def _validate_current(self, project_id: str) -> ContractValidationResult:
        requirements, rules = self._requirements(project_id)
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
            required_term_kinds=requirements["required_term_kinds"],
        )
        findings = validate_contract(assessment)
        self._replace_findings(project_id, findings, assessment.assessment_version, rules.id)
        return ContractValidationResult(
            assessment=assessment,
            findings=findings,
            rules_version_id=rules.id,
        )

    def _requirements(
        self,
        project_id: str,
    ) -> tuple[dict[str, frozenset[ContractTermKind]], ControlledVersionRow]:
        rules = self.session.scalar(
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
            raise ValueError("A bound approved contract_risk_rules version is required")
        contract = rules.payload.get("contract")
        if not isinstance(contract, dict):
            raise ValueError("Contract risk rules lack a contract section")
        required = contract.get("required_term_kinds")
        independent = contract.get("independently_verified_term_kinds")
        if not isinstance(required, list) or not isinstance(independent, list):
            raise ValueError("Contract term requirements must be approved lists")
        try:
            return {
                "required_term_kinds": frozenset(ContractTermKind(item) for item in required),
                "independently_verified_term_kinds": frozenset(
                    ContractTermKind(item) for item in independent
                ),
            }, rules
        except ValueError as error:
            raise ValueError("Contract term requirements contain an unknown kind") from error

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
            raise ValueError("One or more contract evidence observations are missing")
        return rows

    @staticmethod
    def _validate_observation_values(
        draft: ContractTermDraft,
        observations: tuple[ObservationRow, ...],
    ) -> None:
        for row in observations:
            value = row.payload.get("observation", {}).get("value")
            if value != draft.value:
                raise ValueError("Contract evidence observations do not reproduce the term")

    def _validate_contract_cost_component(
        self,
        project_id: str,
        command: ContractCostImpactCommand,
    ) -> None:
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
        ):
            raise ValueError("Contract impact must reference a CONTRACT_FINANCE component")

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

    def _require_editable_state(self, actor: Actor, project_id: str) -> Any:
        project = self._project_service().get_project(
            actor=actor,
            project_id=project_id,
            lock=True,
        )
        if ApprovalState(project.state) not in {
            ApprovalState.EXTRACTION_IN_PROGRESS,
            ApprovalState.EXTRACTION_REVIEW,
            ApprovalState.BOQ_IN_PROGRESS,
            ApprovalState.BOQ_REVIEW,
            ApprovalState.PRICING_IN_PROGRESS,
            ApprovalState.RFQ_REQUIRED,
        }:
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
            verified=row.verified,
            cost_impact_resolved=row.cost_impact_resolved,
            cost_impact_amount=(
                Decimal(str(impact_payload["amount"])) if "amount" in impact_payload else None
            ),
            cost_impact_currency=impact_payload.get("currency"),
            cost_input_id=cost_input_id,
            approved_assumption_id=row.payload.get("cost_impact_approval_id"),
        )

    @staticmethod
    def _view(row: ContractTermRow) -> ContractTermView:
        return ContractTermView(
            term_id=row.id,
            kind=ContractTermKind(row.kind),
            value=str(row.payload.get("value")),
            observation_ids=tuple(row.payload.get("observation_ids", [])),
            verified=row.verified,
            cost_impact_resolved=row.cost_impact_resolved,
            supersedes_term_id=row.supersedes_term_id,
            is_current=row.is_current,
            approval_task_ids=tuple(row.payload.get("approval_task_ids", [])),
        )
