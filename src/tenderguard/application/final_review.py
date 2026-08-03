from __future__ import annotations

from typing import Literal
from uuid import uuid4

from pydantic import Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from tenderguard.application.pricing import PricingService
from tenderguard.application.projects import OptimisticLockError, ProjectService, ProjectView
from tenderguard.config import Settings
from tenderguard.domain.common import content_hash, utc_now
from tenderguard.domain.enums import ActorRole, ApprovalState
from tenderguard.domain.models import DomainModel
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.object_store import ObjectStore
from tenderguard.infrastructure.orm import CalculationSnapshotRow, ExpertReworkRequestRow


class ExpertReworkIssue(DomainModel):
    kind: Literal["BOQ_PRICE_ROW", "RELEASE_FINDING"]
    reference_id: str = Field(min_length=1, max_length=300)
    code: str = Field(min_length=1, max_length=100)
    comment: str = Field(min_length=3, max_length=2000)

    @field_validator("reference_id", "code", "comment")
    @classmethod
    def text_is_trimmed(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("Expert rework text must not contain surrounding whitespace")
        return value


class ExpertReworkCommand(DomainModel):
    expected_project_row_version: int = Field(ge=1)
    gate_target: Literal["bid", "internal"]
    gate_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    issues: tuple[ExpertReworkIssue, ...] = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=10, max_length=2000)

    @field_validator("reason")
    @classmethod
    def reason_is_trimmed(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("Expert rework reason must not contain surrounding whitespace")
        return value

    @model_validator(mode="after")
    def issue_references_are_unique(self) -> ExpertReworkCommand:
        keys = tuple((issue.kind, issue.reference_id, issue.code) for issue in self.issues)
        if len(keys) != len(set(keys)):
            raise ValueError("Expert rework issues must be unique")
        return self


class ExpertReworkResult(DomainModel):
    rework_request_id: str
    project: ProjectView
    snapshot_id: str
    requested_state: ApprovalState
    target_stage: ApprovalState
    gate_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    issues: tuple[ExpertReworkIssue, ...]


_EXTRACTION_FINDINGS = frozenset(
    {
        "CURRENT_DOCUMENT_SET_NOT_CONFIRMED",
        "CRITICAL_DOCUMENT_MISSING",
        "UNRESOLVED_CONFLICT",
    }
)
_BOQ_FINDINGS = frozenset(
    {
        "KEY_QUANTITY_UNVERIFIED",
        "TECHNICAL_ANALOGUE_UNVERIFIED",
    }
)
_PRICING_FINDINGS = frozenset(
    {
        "COST_WITHOUT_BASIS",
        "PRICE_NORMALIZATION_FAILED",
        "UNVERIFIED_COST_SHARE_EXCEEDED",
        "CONTRACT_RISK_UNRESOLVED",
    }
)
_CALCULATION_FINDINGS = frozenset(
    {
        "INDEPENDENT_VALIDATION_MISSING",
        "INDEPENDENT_RECALCULATION_MISMATCH",
        "CALCULATION_SNAPSHOT_MISSING",
        "CALCULATION_SNAPSHOT_STALE",
        "CALCULATION_SNAPSHOT_INTEGRITY_FAILED",
        "NORMATIVE_CALCULATION_MISSING",
    }
)
_AUTOMATIC_STAGE_ORDER = (
    ApprovalState.EXTRACTION_IN_PROGRESS,
    ApprovalState.BOQ_IN_PROGRESS,
    ApprovalState.PRICING_IN_PROGRESS,
    ApprovalState.CALCULATION_IN_PROGRESS,
)


class FinalReviewService:
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

    def request_rework(
        self,
        *,
        actor: Actor,
        project_id: str,
        command: ExpertReworkCommand,
        request_id: str,
    ) -> ExpertReworkResult:
        project_service = self._project_service()
        project = project_service.get_project(
            actor=actor,
            project_id=project_id,
            lock=True,
            required_roles=(ActorRole.REVIEWER, ActorRole.APPROVER),
        )
        if project.state != ApprovalState.EXPERT_REVIEW.value:
            raise ValueError("Final expert rework is available only in EXPERT_REVIEW")
        if project.row_version != command.expected_project_row_version:
            raise OptimisticLockError(
                "Project changed after the final review was loaded; reload before deciding"
            )

        (
            project_view,
            bid_decision,
            bid_hash,
            internal_decision,
            internal_hash,
        ) = project_service.evaluate_release_gates(actor=actor, project_id=project.id)
        decision = bid_decision if command.gate_target == "bid" else internal_decision
        current_gate_hash = bid_hash if command.gate_target == "bid" else internal_hash
        if current_gate_hash != command.gate_hash:
            raise ValueError("Final review gate changed; reload the complete server evaluation")
        if project_view.row_version != command.expected_project_row_version:
            raise OptimisticLockError("Project changed during final review evaluation")

        snapshot = self.session.scalar(
            select(CalculationSnapshotRow)
            .where(CalculationSnapshotRow.project_id == project.id)
            .order_by(CalculationSnapshotRow.created_at.desc(), CalculationSnapshotRow.id.desc())
            .limit(1)
        )
        if snapshot is None or not snapshot.fixed:
            raise ValueError("A fixed calculation snapshot is required before final expert review")
        if snapshot.document_set_revision_id != project.current_document_set_revision_id:
            raise ValueError("The final calculation snapshot is stale")

        matrix = PricingService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        ).boq_price_matrix(actor=actor, project_id=project.id)
        matrix_rows = {row.row_id: row for row in matrix.rows}
        release_findings = {finding.code.value: finding for finding in decision.findings}
        enriched_issues: list[dict[str, object]] = []
        target_stages: list[ApprovalState] = []

        for issue in command.issues:
            if issue.kind == "BOQ_PRICE_ROW":
                row = matrix_rows.get(issue.reference_id)
                if row is None:
                    raise ValueError(
                        f"BoQ price row {issue.reference_id!r} is absent from the current matrix"
                    )
                if issue.code != "EXPERT_RECHECK_REQUESTED" and issue.code not in row.blockers:
                    raise ValueError(
                        f"Issue code {issue.code!r} is not current for row {issue.reference_id!r}"
                    )
                target_stages.append(ApprovalState.PRICING_IN_PROGRESS)
                enriched_issues.append(
                    {
                        **issue.model_dump(mode="json"),
                        "boq_line_id": row.boq_line_id,
                        "item_id": row.item_id,
                        "boq_item_name": row.boq_item_name,
                        "current_row_status": row.row_status,
                        "current_blockers": list(row.blockers),
                    }
                )
                continue

            finding = release_findings.get(issue.code)
            if finding is None:
                raise ValueError(f"Release finding {issue.code!r} is no longer current")
            current_references = finding.entity_ids or (finding.code.value,)
            if issue.reference_id not in current_references:
                raise ValueError(
                    f"Reference {issue.reference_id!r} does not belong to {issue.code!r}"
                )
            target_stages.append(self._stage_for_finding(issue.code))
            enriched_issues.append(
                {
                    **issue.model_dump(mode="json"),
                    "current_message": finding.message,
                    "current_details": finding.details,
                }
            )

        target_stage = self._earliest_target(target_stages)
        requested_state = decision.requested_state
        now = utc_now()
        rework_request_id = f"expert-rework-{uuid4()}"
        base_payload: dict[str, object] = {
            "project_id": project.id,
            "snapshot_id": snapshot.id,
            "document_set_revision_id": project.current_document_set_revision_id,
            "project_row_version": project.row_version,
            "gate_target": command.gate_target,
            "gate_hash": current_gate_hash,
            "requested_state": requested_state.value,
            "target_stage": target_stage.value,
            "reason": command.reason,
            "issues": enriched_issues,
        }
        payload = {**base_payload, "request_hash": content_hash(base_payload)}
        self.session.add(
            ExpertReworkRequestRow(
                id=rework_request_id,
                project_id=project.id,
                snapshot_id=snapshot.id,
                requested_state=requested_state.value,
                gate_hash=current_gate_hash,
                target_stage=target_stage.value,
                payload=payload,
                requested_by=actor.actor_id,
                requested_at=now,
            )
        )
        project_service._change_state(
            project=project,
            to_state=target_stage,
            actor=actor,
            request_id=request_id,
            reason=command.reason,
        )
        project_service.record_event(
            aggregate_type="project",
            aggregate_id=project.id,
            event_type="final_expert_rework_requested",
            actor=actor,
            request_id=request_id,
            reason=command.reason,
            payload={
                "rework_request_id": rework_request_id,
                "snapshot_id": snapshot.id,
                "gate_hash": current_gate_hash,
                "target_stage": target_stage.value,
                "request_hash": payload["request_hash"],
                "issue_references": [
                    {
                        "kind": issue.kind,
                        "reference_id": issue.reference_id,
                        "code": issue.code,
                    }
                    for issue in command.issues
                ],
            },
        )
        project_service.enqueue_event(
            topic="project.final-review.rework-requested",
            aggregate_id=rework_request_id,
            payload={
                "project_id": project.id,
                "rework_request_id": rework_request_id,
                "snapshot_id": snapshot.id,
                "target_stage": target_stage.value,
                "request_hash": payload["request_hash"],
            },
        )
        return ExpertReworkResult(
            rework_request_id=rework_request_id,
            project=project_service._view(project),
            snapshot_id=snapshot.id,
            requested_state=requested_state,
            target_stage=target_stage,
            gate_hash=current_gate_hash,
            issues=command.issues,
        )

    @staticmethod
    def _stage_for_finding(code: str) -> ApprovalState:
        if code in _EXTRACTION_FINDINGS:
            return ApprovalState.EXTRACTION_IN_PROGRESS
        if code in _BOQ_FINDINGS:
            return ApprovalState.BOQ_IN_PROGRESS
        if code in _PRICING_FINDINGS:
            return ApprovalState.PRICING_IN_PROGRESS
        if code in _CALCULATION_FINDINGS:
            return ApprovalState.CALCULATION_IN_PROGRESS
        return ApprovalState.BLOCKED

    @staticmethod
    def _earliest_target(stages: list[ApprovalState]) -> ApprovalState:
        if not stages or ApprovalState.BLOCKED in stages:
            return ApprovalState.BLOCKED
        for stage in _AUTOMATIC_STAGE_ORDER:
            if stage in stages:
                return stage
        return ApprovalState.BLOCKED

    def _project_service(self) -> ProjectService:
        return ProjectService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        )
