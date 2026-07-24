from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from tenderguard.application.projects import OptimisticLockError, ProjectService
from tenderguard.config import Settings
from tenderguard.domain.approvals import (
    DEDICATED_APPROVAL_TASK_TYPES,
    ApprovalPlan,
    ApprovalPolicyDefinition,
    ApprovalSubject,
    build_approval_plan,
)
from tenderguard.domain.common import content_hash, ensure_utc, utc_now
from tenderguard.domain.enums import (
    ActorRole,
    ApprovalDecision,
    VersionStatus,
)
from tenderguard.domain.models import DomainModel
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.object_store import ObjectStore
from tenderguard.infrastructure.orm import (
    ApprovalRecordRow,
    ApprovalTaskRow,
    ControlledVersionRow,
    ManualChangeRow,
    ObservationRow,
    ProjectControlledVersionRow,
    VerificationFindingRow,
)


class ApprovalDecisionResult(DomainModel):
    approval_id: str
    task_id: str
    decision: ApprovalDecision
    decided_by: str


class ApprovalPlanResult(DomainModel):
    plan: ApprovalPlan
    task_ids_by_key: dict[str, str]


class ApprovalDecisionCommand(DomainModel):
    decision: ApprovalDecision
    reason: str = Field(min_length=1, max_length=4000)
    expected_task_updated_at: datetime
    related_change_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()

    @field_validator("expected_task_updated_at")
    @classmethod
    def timestamp_is_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Expected task timestamp must include a timezone")
        return value

    @field_validator("related_change_ids", "evidence_ids")
    @classmethod
    def identifiers_are_bounded_and_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) > 100:
            raise ValueError("No more than 100 identifiers may be supplied")
        if len(values) != len(set(values)):
            raise ValueError("Identifiers must be unique")
        if any(
            not value
            or value != value.strip()
            or len(value) > 128
            or any(character.isspace() for character in value)
            for value in values
        ):
            raise ValueError("An identifier is invalid")
        return values


class ApprovalService:
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

    def plan(
        self,
        *,
        actor: Actor,
        project_id: str,
        subjects: tuple[ApprovalSubject, ...],
        request_id: str,
        reason: str,
    ) -> ApprovalPlanResult:
        project_service = self._project_service()
        project_service.get_project(
            actor=actor,
            project_id=project_id,
            lock=True,
            required_roles=(
                ActorRole.ESTIMATOR,
                ActorRole.REVIEWER,
                ActorRole.TECHNICAL_EXPERT,
            ),
        )
        policy_row = self.session.scalar(
            select(ControlledVersionRow)
            .join(
                ProjectControlledVersionRow,
                ProjectControlledVersionRow.controlled_version_id == ControlledVersionRow.id,
            )
            .where(
                ProjectControlledVersionRow.project_id == project_id,
                ProjectControlledVersionRow.purpose == "approval_policy",
                ControlledVersionRow.kind == "approval_policy",
                ControlledVersionRow.status == VersionStatus.APPROVED.value,
            )
        )
        if policy_row is None:
            raise ValueError("A bound approved approval_policy version is required")
        policy = ApprovalPolicyDefinition.model_validate(
            {
                "policy_version": policy_row.id,
                "rules": policy_row.payload.get("rules", []),
            }
        )
        plan = build_approval_plan(subjects, policy)
        now = utc_now()
        task_ids_by_key: dict[str, str] = {}
        for task in plan.tasks:
            task_id = f"approval-task-{content_hash(task.task_key)[:24]}"
            task_ids_by_key[task.task_key] = task_id
            existing = self.session.get(ApprovalTaskRow, task_id)
            if existing is not None:
                continue
            self.session.add(
                ApprovalTaskRow(
                    id=task_id,
                    project_id=project_id,
                    task_type=task.reason.value,
                    entity_type=task.entity_type,
                    entity_id=task.entity_id,
                    assigned_role=task.assigned_role.value,
                    status="PENDING",
                    required=task.required,
                    payload={
                        "task_key": task.task_key,
                        "policy_version_id": policy_row.id,
                        "created_by": actor.actor_id,
                    },
                    created_at=now,
                    updated_at=now,
                )
            )
        for finding in plan.findings:
            finding_id = f"finding-{content_hash({'project': project_id, 'finding': finding})[:24]}"
            if self.session.get(VerificationFindingRow, finding_id) is not None:
                continue
            self.session.add(
                VerificationFindingRow(
                    id=finding_id,
                    project_id=project_id,
                    contour="EXPERT_APPROVAL",
                    code=finding.code.value,
                    severity=finding.severity.value,
                    resolved=False,
                    payload=finding.model_dump(mode="json"),
                    created_at=now,
                    updated_at=now,
                )
            )
        project_service.record_event(
            aggregate_type="project",
            aggregate_id=project_id,
            event_type="approval_plan_built",
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload={
                "policy_version_id": policy_row.id,
                "task_keys": [task.task_key for task in plan.tasks],
                "finding_codes": [finding.code for finding in plan.findings],
            },
        )
        return ApprovalPlanResult(plan=plan, task_ids_by_key=task_ids_by_key)

    def decide(
        self,
        *,
        actor: Actor,
        project_id: str,
        task_id: str,
        command: ApprovalDecisionCommand,
        request_id: str,
    ) -> ApprovalDecisionResult:
        project_service = self._project_service()
        project_service.get_project(actor=actor, project_id=project_id, lock=True)
        task = self.session.scalar(
            select(ApprovalTaskRow)
            .where(
                ApprovalTaskRow.id == task_id,
                ApprovalTaskRow.project_id == project_id,
            )
            .with_for_update()
        )
        if task is None:
            raise LookupError(task_id)
        expected_task_updated_at = ensure_utc(command.expected_task_updated_at)
        assert expected_task_updated_at is not None
        if ensure_utc(task.updated_at) != expected_task_updated_at:
            raise OptimisticLockError(
                "Approval task changed after it was loaded; reload before deciding"
            )
        project_service.get_project(
            actor=actor,
            project_id=project_id,
            required_roles=(ActorRole(task.assigned_role),),
        )
        if task.status != "PENDING":
            raise ValueError("Only a PENDING approval task can be decided")
        if task.task_type in DEDICATED_APPROVAL_TASK_TYPES:
            raise ValueError(f"Approval task {task.task_type} requires its dedicated workflow")
        if task.required and task.payload.get("created_by") == actor.actor_id:
            raise ValueError("Four-eyes violation: a task creator cannot approve it")
        change_ids = set(command.related_change_ids)
        if task.entity_type == "manual_change":
            change_ids.add(task.entity_id)
        changes = list(
            self.session.scalars(
                select(ManualChangeRow).where(
                    ManualChangeRow.project_id == project_id,
                    ManualChangeRow.id.in_(change_ids),
                )
            )
        )
        if len(changes) != len(change_ids):
            raise ValueError("One or more related manual changes do not exist")
        if any(change.changed_by == actor.actor_id for change in changes):
            raise ValueError("Four-eyes violation: a change author cannot approve it")
        evidence_ids = set(command.evidence_ids)
        evidence = list(
            self.session.scalars(
                select(ObservationRow).where(
                    ObservationRow.project_id == project_id,
                    ObservationRow.id.in_(evidence_ids),
                )
            )
        )
        if len(evidence) != len(evidence_ids):
            raise ValueError("One or more approval evidence observations do not exist")
        if command.decision is ApprovalDecision.APPROVED and task.required:
            if task.entity_type == "manual_change" and not changes:
                raise ValueError("Critical manual-change approval lacks the change record")
            if not evidence:
                raise ValueError("Required approval needs explicit evidence identifiers")
        now = utc_now()
        approval_id = f"approval-{uuid4()}"
        record_payload: dict[str, Any] = {
            "project_id": project_id,
            "expected_task_updated_at": expected_task_updated_at.isoformat(),
            "related_change_ids": sorted(change_ids),
            "evidence_ids": list(command.evidence_ids),
            "policy_version_id": task.payload.get("policy_version_id"),
        }
        self.session.add(
            ApprovalRecordRow(
                id=approval_id,
                task_id=task.id,
                decision=command.decision.value,
                decided_by=actor.actor_id,
                reason=command.reason,
                payload=record_payload,
                decided_at=now,
            )
        )
        task.status = command.decision.value
        task.updated_at = now
        project_service.record_event(
            aggregate_type="project",
            aggregate_id=project_id,
            event_type="approval_task_decided",
            actor=actor,
            request_id=request_id,
            reason=command.reason,
            payload={
                "approval_id": approval_id,
                "task_id": task.id,
                "decision": command.decision,
                **record_payload,
            },
        )
        return ApprovalDecisionResult(
            approval_id=approval_id,
            task_id=task.id,
            decision=command.decision,
            decided_by=actor.actor_id,
        )

    def _project_service(self) -> ProjectService:
        return ProjectService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        )
