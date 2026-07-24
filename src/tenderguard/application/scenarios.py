from __future__ import annotations

from typing import Any
from uuid import uuid4

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from tenderguard.application.projects import ProjectService
from tenderguard.application.snapshot_integrity import read_verified_snapshot
from tenderguard.config import Settings
from tenderguard.domain.calculation import AtomicCostInput, CalculationPolicy
from tenderguard.domain.common import content_hash, utc_now
from tenderguard.domain.enums import ActorRole, ApprovalState, VerificationStatus, VersionStatus
from tenderguard.domain.models import DomainModel
from tenderguard.domain.scenarios import (
    ScenarioDefinition,
    ScenarioResult,
    calculate_scenario,
)
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.object_store import ObjectStore
from tenderguard.infrastructure.orm import (
    ApprovalRecordRow,
    ApprovalTaskRow,
    CalculationSnapshotRow,
    ControlledVersionRow,
    ObservationRow,
    ProjectControlledVersionRow,
    ScenarioRunRow,
)


class ScenarioExecutionCommand(DomainModel):
    snapshot_id: str = Field(min_length=1)
    scenario_key: str = Field(min_length=1, max_length=128)


class ScenarioExecutionResult(DomainModel):
    scenario_run_id: str
    base_snapshot_id: str
    scenario_policy_version_id: str
    definition: ScenarioDefinition
    result: ScenarioResult


class ScenarioService:
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
        command: ScenarioExecutionCommand,
        request_id: str,
        reason: str,
    ) -> ScenarioExecutionResult:
        project_service = ProjectService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        )
        project = project_service.get_project(
            actor=actor,
            project_id=project_id,
            lock=True,
            required_roles=(
                ActorRole.ESTIMATOR,
                ActorRole.REVIEWER,
                ActorRole.APPROVER,
                ActorRole.METHODOLOGY_OWNER,
            ),
        )
        if ApprovalState(project.state) not in {
            ApprovalState.INDEPENDENT_VALIDATION,
            ApprovalState.EXPERT_REVIEW,
            ApprovalState.APPROVED_FOR_INTERNAL_USE,
            ApprovalState.APPROVED_FOR_BID,
        }:
            raise ValueError("Scenario calculation requires a fixed base calculation")
        snapshot = self.session.scalar(
            select(CalculationSnapshotRow).where(
                CalculationSnapshotRow.id == command.snapshot_id,
                CalculationSnapshotRow.project_id == project.id,
                CalculationSnapshotRow.fixed.is_(True),
            )
        )
        if snapshot is None:
            raise LookupError(command.snapshot_id)
        if snapshot.document_set_revision_id != project.current_document_set_revision_id:
            raise ValueError("Scenario base snapshot belongs to a superseded document set")
        snapshot_payload = read_verified_snapshot(
            object_store=self.object_store,
            snapshot=snapshot,
        )
        policy_row = self.session.scalar(
            select(ControlledVersionRow)
            .join(
                ProjectControlledVersionRow,
                ProjectControlledVersionRow.controlled_version_id == ControlledVersionRow.id,
            )
            .where(
                ProjectControlledVersionRow.project_id == project.id,
                ProjectControlledVersionRow.purpose == "scenario_policy",
                ControlledVersionRow.kind == "scenario_policy",
                ControlledVersionRow.status == VersionStatus.APPROVED.value,
            )
        )
        if policy_row is None:
            raise ValueError("A bound approved scenario_policy version is required")
        raw_scenarios = policy_row.payload.get("scenarios")
        if not isinstance(raw_scenarios, dict):
            raise ValueError("Scenario policy has no scenario definitions")
        raw_definition = raw_scenarios.get(command.scenario_key)
        if not isinstance(raw_definition, dict):
            raise LookupError(command.scenario_key)
        definition_payload = self._bind_policy_evidence(
            raw_definition,
            policy_version_id=policy_row.id,
        )
        definition = ScenarioDefinition.model_validate(
            {
                **definition_payload,
                "scenario_id": command.scenario_key,
                "scenario_version": policy_row.id,
            }
        )
        raw_inputs = snapshot_payload.get("inputs")
        raw_calculation_policy = snapshot_payload.get("policy")
        if not isinstance(raw_inputs, list) or not isinstance(raw_calculation_policy, dict):
            raise RuntimeError("Base snapshot lacks atomic inputs or calculation policy")
        inputs = tuple(AtomicCostInput.model_validate(item) for item in raw_inputs)
        calculation_policy = CalculationPolicy.model_validate(raw_calculation_policy)
        self._validate_override_evidence(
            project_id=project.id,
            definition=definition,
            scenario_policy_version_id=policy_row.id,
        )

        now = utc_now()
        result = calculate_scenario(
            inputs,
            definition,
            calculation_policy,
            calculated_at=now,
        )
        run_id = f"scenario-run-{uuid4()}"
        self.session.add(
            ScenarioRunRow(
                id=run_id,
                project_id=project.id,
                base_calculation_run_id=snapshot.calculation_run_id,
                scenario_version=policy_row.id,
                status="VALIDATED" if result.independent.passed else "FAILED_VALIDATION",
                grand_total=result.primary.grand_total,
                payload={
                    "base_snapshot_id": snapshot.id,
                    "base_snapshot_hash": snapshot.snapshot_hash,
                    "scenario_key": command.scenario_key,
                    "definition": definition.model_dump(mode="json"),
                    "result": result.model_dump(mode="json"),
                    "input_signature": content_hash(
                        {
                            "base_snapshot_hash": snapshot.snapshot_hash,
                            "scenario_policy_version_id": policy_row.id,
                            "definition": definition,
                        }
                    ),
                    "executed_by": actor.actor_id,
                },
                created_at=now,
            )
        )
        project_service.record_event(
            aggregate_type="project",
            aggregate_id=project.id,
            event_type="scenario_calculated",
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload={
                "scenario_run_id": run_id,
                "scenario_key": command.scenario_key,
                "scenario_policy_version_id": policy_row.id,
                "base_snapshot_id": snapshot.id,
                "grand_total": result.primary.grand_total,
                "independent_validation_passed": result.independent.passed,
            },
        )
        return ScenarioExecutionResult(
            scenario_run_id=run_id,
            base_snapshot_id=snapshot.id,
            scenario_policy_version_id=policy_row.id,
            definition=definition,
            result=result,
        )

    @staticmethod
    def _bind_policy_evidence(
        raw_definition: dict[str, Any],
        *,
        policy_version_id: str,
    ) -> dict[str, Any]:
        raw_overrides = raw_definition.get("overrides")
        if not isinstance(raw_overrides, list):
            raise ValueError("Scenario definition overrides must be a list")
        overrides: list[dict[str, Any]] = []
        for item in raw_overrides:
            if not isinstance(item, dict):
                raise ValueError("Scenario override must be an object")
            evidence_id = item.get("evidence_or_assumption_id")
            overrides.append(
                {
                    **item,
                    "evidence_or_assumption_id": (
                        policy_version_id if evidence_id == "BOUND_SCENARIO_POLICY" else evidence_id
                    ),
                }
            )
        return {**raw_definition, "overrides": overrides}

    def _validate_override_evidence(
        self,
        *,
        project_id: str,
        definition: ScenarioDefinition,
        scenario_policy_version_id: str,
    ) -> None:
        for override in definition.overrides:
            if override.evidence_or_assumption_id == scenario_policy_version_id:
                continue
            observation = self.session.scalar(
                select(ObservationRow).where(
                    ObservationRow.id == override.evidence_or_assumption_id,
                    ObservationRow.project_id == project_id,
                    ObservationRow.status == VerificationStatus.VERIFIED.value,
                )
            )
            if observation is not None and self._basis_reproduces_override(
                observation.payload,
                override.model_dump(mode="json"),
            ):
                continue
            approval = self.session.scalar(
                select(ApprovalRecordRow)
                .join(ApprovalTaskRow, ApprovalTaskRow.id == ApprovalRecordRow.task_id)
                .where(
                    ApprovalRecordRow.id == override.evidence_or_assumption_id,
                    ApprovalRecordRow.decision == "APPROVED",
                    ApprovalTaskRow.project_id == project_id,
                )
            )
            if approval is not None and self._basis_reproduces_override(
                approval.payload,
                override.model_dump(mode="json"),
            ):
                continue
            raise ValueError(
                f"Scenario override {override.cost_input_id} has no reproducing "
                "approved evidence or assumption"
            )

    @staticmethod
    def _basis_reproduces_override(
        payload: dict[str, Any],
        expected: dict[str, Any],
    ) -> bool:
        candidate = payload.get("scenario_override")
        if not isinstance(candidate, dict):
            observation = payload.get("observation")
            candidate = observation.get("value") if isinstance(observation, dict) else None
        if not isinstance(candidate, dict):
            return False
        fields = ("cost_input_id", "quantity", "unit_rate", "factor_values")
        return all(candidate.get(field) == expected.get(field) for field in fields)
