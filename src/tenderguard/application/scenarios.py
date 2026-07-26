from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import uuid4

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from tenderguard.application.controlled_version_integrity import (
    ControlledVersionIntegrityError,
    require_bound_controlled_version,
)
from tenderguard.application.projects import ProjectService
from tenderguard.application.snapshot_integrity import read_verified_snapshot
from tenderguard.config import Settings
from tenderguard.domain.calculation import (
    AtomicCostInput,
    CalculationPolicy,
    calculate_primary,
    validate_independently,
)
from tenderguard.domain.common import content_hash, ensure_utc, utc_now
from tenderguard.domain.enums import ActorRole, ApprovalState, VerificationStatus
from tenderguard.domain.models import (
    CalculationResult,
    DomainModel,
    IndependentValidationResult,
)
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
    CalculationRunRow,
    CalculationSnapshotRow,
    ControlledVersionRow,
    ObservationRow,
    ScenarioRunRow,
)

SCENARIO_STATES = frozenset(
    {
        ApprovalState.INDEPENDENT_VALIDATION,
        ApprovalState.EXPERT_REVIEW,
        ApprovalState.APPROVED_FOR_INTERNAL_USE,
        ApprovalState.APPROVED_FOR_BID,
    }
)
SCENARIO_RELATIVE_DELTA_QUANTUM = Decimal("0.0001")


class ScenarioExecutionCommand(DomainModel):
    snapshot_id: str = Field(min_length=1, max_length=64)
    scenario_key: str = Field(min_length=1, max_length=128)


class ScenarioExecutionResult(DomainModel):
    scenario_run_id: str
    base_snapshot_id: str
    scenario_policy_version_id: str
    definition: ScenarioDefinition
    result: ScenarioResult


class ScenarioSnapshotView(DomainModel):
    snapshot_id: str
    calculation_run_id: str
    document_set_revision_id: str
    snapshot_hash: str
    currency: str | None = None
    grand_total: Decimal | None = None
    independent_validation_passed: bool | None = None
    created_by: str
    created_at: datetime
    integrity_valid: bool
    integrity_error: str | None = None


class ScenarioRunComparisonView(DomainModel):
    scenario_run_id: str
    scenario_key: str
    scenario_name: str
    base_snapshot_id: str
    scenario_policy_version_id: str
    status: str
    currency: str | None = None
    base_grand_total: Decimal | None = None
    scenario_grand_total: Decimal | None = None
    absolute_delta: Decimal | None = None
    relative_delta_percent: Decimal | None = None
    independent_validation_passed: bool | None = None
    executed_by: str | None = None
    created_at: datetime
    integrity_valid: bool
    integrity_error: str | None = None


class ScenarioContextView(DomainModel):
    project_id: str
    project_state: ApprovalState
    current_document_set_revision_id: str | None
    scenario_policy_version_id: str | None
    selected_snapshot_id: str | None
    snapshots: tuple[ScenarioSnapshotView, ...]
    snapshots_truncated: bool
    definitions: tuple[ScenarioDefinition, ...]
    comparisons: tuple[ScenarioRunComparisonView, ...]
    comparisons_truncated: bool
    blockers: tuple[str, ...]


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

    def context(
        self,
        *,
        actor: Actor,
        project_id: str,
        snapshot_id: str | None,
    ) -> ScenarioContextView:
        project = self._project_service().get_project(
            actor=actor,
            project_id=project_id,
            required_roles=(
                ActorRole.ESTIMATOR,
                ActorRole.REVIEWER,
                ActorRole.APPROVER,
                ActorRole.METHODOLOGY_OWNER,
            ),
        )
        normalized_snapshot_id = snapshot_id.strip() if snapshot_id is not None else None
        if normalized_snapshot_id == "":
            normalized_snapshot_id = None
        if normalized_snapshot_id is not None and (
            normalized_snapshot_id != snapshot_id or len(normalized_snapshot_id) > 64
        ):
            raise ValueError("Scenario snapshot ID must be normalized and at most 64 characters")

        blockers: list[str] = []
        if ApprovalState(project.state) not in SCENARIO_STATES:
            blockers.append("PROJECT_STATE_NOT_ALLOWED")
        if not project.current_document_set_revision_id:
            blockers.append("CURRENT_DOCUMENT_SET_MISSING")

        policy = None
        try:
            policy = self._bound_policy(
                project_id=project.id,
                organization_id=project.organization_id,
            )
        except ControlledVersionIntegrityError:
            blockers.append("SCENARIO_POLICY_INTEGRITY_FAILED")

        rows: list[CalculationSnapshotRow] = []
        if project.current_document_set_revision_id:
            rows = list(
                self.session.scalars(
                    select(CalculationSnapshotRow)
                    .where(
                        CalculationSnapshotRow.project_id == project.id,
                        CalculationSnapshotRow.document_set_revision_id
                        == project.current_document_set_revision_id,
                        CalculationSnapshotRow.fixed.is_(True),
                    )
                    .order_by(
                        CalculationSnapshotRow.created_at.desc(),
                        CalculationSnapshotRow.id.desc(),
                    )
                    .limit(51)
                )
            )
        snapshots_truncated = len(rows) > 50
        rows = rows[:50]
        selected_row = (
            next((row for row in rows if row.id == normalized_snapshot_id), None)
            if normalized_snapshot_id is not None
            else (rows[0] if rows else None)
        )
        if normalized_snapshot_id is not None and selected_row is None:
            selected_row = self.session.scalar(
                select(CalculationSnapshotRow).where(
                    CalculationSnapshotRow.id == normalized_snapshot_id,
                    CalculationSnapshotRow.project_id == project.id,
                    CalculationSnapshotRow.document_set_revision_id
                    == project.current_document_set_revision_id,
                    CalculationSnapshotRow.fixed.is_(True),
                )
            )
            if selected_row is None:
                raise LookupError(normalized_snapshot_id)
            rows = [selected_row, *rows[:49]]

        snapshot_views: list[ScenarioSnapshotView] = []
        selected_basis: (
            tuple[
                tuple[AtomicCostInput, ...],
                CalculationPolicy,
                CalculationResult,
            ]
            | None
        ) = None
        for row in rows:
            created_at = ensure_utc(row.created_at)
            if created_at is None:
                raise RuntimeError("Calculation snapshot timestamp is missing")
            try:
                view, inputs, calculation_policy, primary = self._verified_snapshot_basis(row)
                if selected_row is not None and row.id == selected_row.id:
                    selected_basis = (inputs, calculation_policy, primary)
            except (LookupError, OSError, RuntimeError, ValueError):
                view = ScenarioSnapshotView(
                    snapshot_id=row.id,
                    calculation_run_id=row.calculation_run_id,
                    document_set_revision_id=row.document_set_revision_id,
                    snapshot_hash=row.snapshot_hash,
                    created_by=row.created_by,
                    created_at=created_at,
                    integrity_valid=False,
                    integrity_error=(
                        "Fixed snapshot failed object, arithmetic, or calculation-run validation"
                    ),
                )
            snapshot_views.append(view)

        if selected_row is None:
            blockers.append("CURRENT_FIXED_SNAPSHOT_MISSING")
        elif selected_basis is None:
            blockers.append("SELECTED_SNAPSHOT_INTEGRITY_FAILED")

        definitions: tuple[ScenarioDefinition, ...] = ()
        comparisons: tuple[ScenarioRunComparisonView, ...] = ()
        comparisons_truncated = False
        if policy is not None and selected_row is not None and selected_basis is not None:
            inputs, calculation_policy, primary = selected_basis
            try:
                definitions = self._definitions(
                    project_id=project.id,
                    policy=policy,
                    inputs=inputs,
                )
            except (ArithmeticError, KeyError, LookupError, TypeError, ValueError):
                blockers.append("SCENARIO_DEFINITIONS_INVALID")
            else:
                comparisons, comparisons_truncated = self._comparisons(
                    project_id=project.id,
                    snapshot=selected_row,
                    inputs=inputs,
                    calculation_policy=calculation_policy,
                    base_primary=primary,
                    policy_version_id=policy.id,
                    definitions=definitions,
                )
                if any(not item.integrity_valid for item in comparisons):
                    blockers.append("SCENARIO_RUN_INTEGRITY_FAILED")

        return ScenarioContextView(
            project_id=project.id,
            project_state=ApprovalState(project.state),
            current_document_set_revision_id=project.current_document_set_revision_id,
            scenario_policy_version_id=policy.id if policy is not None else None,
            selected_snapshot_id=selected_row.id if selected_row is not None else None,
            snapshots=tuple(snapshot_views),
            snapshots_truncated=snapshots_truncated,
            definitions=definitions,
            comparisons=comparisons,
            comparisons_truncated=comparisons_truncated,
            blockers=tuple(dict.fromkeys(blockers)),
        )

    def execute(
        self,
        *,
        actor: Actor,
        project_id: str,
        command: ScenarioExecutionCommand,
        request_id: str,
        reason: str,
    ) -> ScenarioExecutionResult:
        reason = reason.strip()
        if not reason or len(reason) > 2000:
            raise ValueError("Scenario reason must contain 1 to 2000 characters")
        snapshot_id = command.snapshot_id.strip()
        scenario_key = command.scenario_key.strip()
        if (
            snapshot_id != command.snapshot_id
            or len(snapshot_id) > 64
            or scenario_key != command.scenario_key
        ):
            raise ValueError("Scenario command identifiers must be normalized")
        project_service = self._project_service()
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
        if ApprovalState(project.state) not in SCENARIO_STATES:
            raise ValueError("Scenario calculation requires a fixed base calculation")
        snapshot = self.session.scalar(
            select(CalculationSnapshotRow).where(
                CalculationSnapshotRow.id == snapshot_id,
                CalculationSnapshotRow.project_id == project.id,
                CalculationSnapshotRow.fixed.is_(True),
            )
        )
        if snapshot is None:
            raise LookupError(snapshot_id)
        if snapshot.document_set_revision_id != project.current_document_set_revision_id:
            raise ValueError("Scenario base snapshot belongs to a superseded document set")
        _, inputs, calculation_policy, _ = self._verified_snapshot_basis(snapshot)
        policy_row = self._bound_policy(
            project_id=project.id,
            organization_id=project.organization_id,
        )
        definitions = {
            definition.scenario_id: definition
            for definition in self._definitions(
                project_id=project.id,
                policy=policy_row,
                inputs=inputs,
            )
        }
        definition = definitions.get(scenario_key)
        if definition is None:
            raise LookupError(scenario_key)

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
                    "scenario_key": scenario_key,
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
                "scenario_key": scenario_key,
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

    def _project_service(self) -> ProjectService:
        return ProjectService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        )

    def _bound_policy(
        self,
        *,
        project_id: str,
        organization_id: str,
    ) -> ControlledVersionRow:
        return require_bound_controlled_version(
            session=self.session,
            settings=self.settings,
            project_id=project_id,
            organization_id=organization_id,
            purpose="scenario_policy",
            kind="scenario_policy",
        )

    def _definitions(
        self,
        *,
        project_id: str,
        policy: ControlledVersionRow,
        inputs: tuple[AtomicCostInput, ...],
    ) -> tuple[ScenarioDefinition, ...]:
        raw_scenarios = policy.payload.get("scenarios")
        if not isinstance(raw_scenarios, dict) or not raw_scenarios:
            raise ValueError("Scenario policy has no scenario definitions")
        definitions: list[ScenarioDefinition] = []
        for scenario_key in sorted(raw_scenarios):
            raw_definition = raw_scenarios[scenario_key]
            if (
                not isinstance(scenario_key, str)
                or not scenario_key
                or scenario_key != scenario_key.strip()
                or len(scenario_key) > 128
                or not isinstance(raw_definition, dict)
            ):
                raise ValueError("Scenario policy contains an invalid scenario definition")
            definition_payload = self._bind_policy_evidence(
                raw_definition,
                policy_version_id=policy.id,
                inputs=inputs,
            )
            definition = ScenarioDefinition.model_validate(
                {
                    **definition_payload,
                    "scenario_id": scenario_key,
                    "scenario_version": policy.id,
                }
            )
            self._validate_override_evidence(
                project_id=project_id,
                definition=definition,
                scenario_policy_version_id=policy.id,
            )
            definitions.append(definition)
        return tuple(definitions)

    def _verified_snapshot_basis(
        self,
        snapshot: CalculationSnapshotRow,
    ) -> tuple[
        ScenarioSnapshotView,
        tuple[AtomicCostInput, ...],
        CalculationPolicy,
        CalculationResult,
    ]:
        if not snapshot.fixed:
            raise ValueError("Scenario base snapshot is not fixed")
        payload = read_verified_snapshot(
            object_store=self.object_store,
            snapshot=snapshot,
        )
        raw_inputs = payload.get("inputs")
        raw_calculation_policy = payload.get("policy")
        raw_primary = payload.get("primary")
        raw_independent = payload.get("independent")
        if (
            not isinstance(raw_inputs, list)
            or not isinstance(raw_calculation_policy, dict)
            or not isinstance(raw_primary, dict)
            or not isinstance(raw_independent, dict)
        ):
            raise RuntimeError("Base snapshot lacks calculation inputs or results")
        inputs = tuple(AtomicCostInput.model_validate(item) for item in raw_inputs)
        calculation_policy = CalculationPolicy.model_validate(raw_calculation_policy)
        primary = CalculationResult.model_validate(raw_primary)
        independent = IndependentValidationResult.model_validate(raw_independent)
        replayed_primary = calculate_primary(
            inputs,
            calculation_policy,
            engine_version=primary.engine_version,
            calculated_at=primary.calculated_at,
        )
        replayed_independent = validate_independently(
            inputs,
            replayed_primary,
            calculation_policy,
            validator_version=independent.validator_version,
            validated_at=independent.validated_at,
        )
        run = self.session.scalar(
            select(CalculationRunRow).where(
                CalculationRunRow.id == snapshot.calculation_run_id,
                CalculationRunRow.project_id == snapshot.project_id,
            )
        )
        expected_status = "VALIDATED" if independent.passed else "FAILED_VALIDATION"
        if (
            replayed_primary != primary
            or replayed_independent != independent
            or run is None
            or run.engine_version != primary.engine_version
            or run.status != expected_status
            or run.currency != primary.currency
            or run.grand_total != primary.grand_total
            or run.payload.get("primary") != primary.model_dump(mode="json")
            or run.payload.get("independent_validation") != independent.model_dump(mode="json")
        ):
            raise RuntimeError("Scenario base calculation does not replay")
        created_at = ensure_utc(snapshot.created_at)
        if created_at is None:
            raise RuntimeError("Calculation snapshot timestamp is missing")
        return (
            ScenarioSnapshotView(
                snapshot_id=snapshot.id,
                calculation_run_id=snapshot.calculation_run_id,
                document_set_revision_id=snapshot.document_set_revision_id,
                snapshot_hash=snapshot.snapshot_hash,
                currency=primary.currency,
                grand_total=primary.grand_total,
                independent_validation_passed=independent.passed,
                created_by=snapshot.created_by,
                created_at=created_at,
                integrity_valid=True,
            ),
            inputs,
            calculation_policy,
            primary,
        )

    def _comparisons(
        self,
        *,
        project_id: str,
        snapshot: CalculationSnapshotRow,
        inputs: tuple[AtomicCostInput, ...],
        calculation_policy: CalculationPolicy,
        base_primary: CalculationResult,
        policy_version_id: str,
        definitions: tuple[ScenarioDefinition, ...],
    ) -> tuple[tuple[ScenarioRunComparisonView, ...], bool]:
        rows = list(
            self.session.scalars(
                select(ScenarioRunRow)
                .where(
                    ScenarioRunRow.project_id == project_id,
                    ScenarioRunRow.base_calculation_run_id == snapshot.calculation_run_id,
                    ScenarioRunRow.scenario_version == policy_version_id,
                )
                .order_by(ScenarioRunRow.created_at.desc(), ScenarioRunRow.id.desc())
                .limit(101)
            )
        )
        truncated = len(rows) > 100
        rows = rows[:100]
        definitions_by_key = {item.scenario_id: item for item in definitions}
        comparisons: list[ScenarioRunComparisonView] = []
        for row in rows:
            created_at = ensure_utc(row.created_at)
            if created_at is None:
                raise RuntimeError("Scenario run timestamp is missing")
            scenario_key = row.payload.get("scenario_key")
            normalized_key = scenario_key if isinstance(scenario_key, str) else "UNKNOWN"
            definition = definitions_by_key.get(normalized_key)
            fallback_name = definition.name if definition is not None else normalized_key
            try:
                stored_definition = ScenarioDefinition.model_validate(row.payload.get("definition"))
                stored_result = ScenarioResult.model_validate(row.payload.get("result"))
                executed_by = row.payload.get("executed_by")
                if not isinstance(executed_by, str) or not executed_by:
                    raise RuntimeError("Scenario executor identity is missing")
                if definition is None:
                    raise RuntimeError("Scenario definition is no longer present in policy")
                replay = calculate_scenario(
                    inputs,
                    definition,
                    calculation_policy,
                    calculated_at=stored_result.primary.calculated_at,
                )
                expected_status = (
                    "VALIDATED" if stored_result.independent.passed else "FAILED_VALIDATION"
                )
                expected_signature = content_hash(
                    {
                        "base_snapshot_hash": snapshot.snapshot_hash,
                        "scenario_policy_version_id": policy_version_id,
                        "definition": definition,
                    }
                )
                if (
                    row.payload.get("base_snapshot_id") != snapshot.id
                    or row.payload.get("base_snapshot_hash") != snapshot.snapshot_hash
                    or row.payload.get("input_signature") != expected_signature
                    or stored_definition != definition
                    or stored_result != replay
                    or row.status != expected_status
                    or row.grand_total != stored_result.primary.grand_total
                    or stored_result.primary.currency != base_primary.currency
                ):
                    raise RuntimeError("Scenario run does not reproduce")
                delta = stored_result.primary.grand_total - base_primary.grand_total
                relative_delta = (
                    None
                    if base_primary.grand_total == 0
                    else (delta / base_primary.grand_total * Decimal("100")).quantize(
                        SCENARIO_RELATIVE_DELTA_QUANTUM,
                        rounding=ROUND_HALF_UP,
                    )
                )
                comparisons.append(
                    ScenarioRunComparisonView(
                        scenario_run_id=row.id,
                        scenario_key=normalized_key,
                        scenario_name=definition.name,
                        base_snapshot_id=snapshot.id,
                        scenario_policy_version_id=policy_version_id,
                        status=row.status,
                        currency=base_primary.currency,
                        base_grand_total=base_primary.grand_total,
                        scenario_grand_total=stored_result.primary.grand_total,
                        absolute_delta=delta,
                        relative_delta_percent=relative_delta,
                        independent_validation_passed=stored_result.independent.passed,
                        executed_by=executed_by,
                        created_at=created_at,
                        integrity_valid=True,
                    )
                )
            except (RuntimeError, ValueError):
                comparisons.append(
                    ScenarioRunComparisonView(
                        scenario_run_id=row.id,
                        scenario_key=normalized_key,
                        scenario_name=fallback_name,
                        base_snapshot_id=snapshot.id,
                        scenario_policy_version_id=policy_version_id,
                        status="INTEGRITY_FAILED",
                        created_at=created_at,
                        integrity_valid=False,
                        integrity_error=(
                            "Stored scenario result failed policy, snapshot, or arithmetic replay"
                        ),
                    )
                )
        return tuple(comparisons), truncated

    @staticmethod
    def _bind_policy_evidence(
        raw_definition: dict[str, Any],
        *,
        policy_version_id: str,
        inputs: tuple[AtomicCostInput, ...],
    ) -> dict[str, Any]:
        raw_overrides = raw_definition.get("overrides")
        if not isinstance(raw_overrides, list):
            raise ValueError("Scenario definition overrides must be a list")
        overrides: list[dict[str, Any]] = []
        for item in raw_overrides:
            if not isinstance(item, dict):
                raise ValueError("Scenario override must be an object")
            cost_input_id = item.get("cost_input_id")
            semantic_key = item.get("semantic_key")
            line_id = item.get("line_id")
            if cost_input_id is not None and (semantic_key is not None or line_id is not None):
                raise ValueError(
                    "Scenario override cannot combine a technical cost-input ID "
                    "with a stable component selector"
                )
            if cost_input_id is None:
                if not isinstance(semantic_key, str) or not semantic_key:
                    raise ValueError("Scenario override requires cost_input_id or semantic_key")
                if line_id is not None and (not isinstance(line_id, str) or not line_id):
                    raise ValueError("Scenario override line_id is invalid")
                candidates = [
                    candidate
                    for candidate in inputs
                    if candidate.semantic_key == semantic_key
                    and (line_id is None or candidate.line_id == line_id)
                ]
                if len(candidates) != 1:
                    raise ValueError(
                        "Scenario component selector must resolve to exactly one "
                        f"snapshot input: {line_id or '*'}:{semantic_key}"
                    )
                cost_input_id = candidates[0].cost_input_id
            elif not isinstance(cost_input_id, str) or not cost_input_id:
                raise ValueError("Scenario override cost_input_id is invalid")
            evidence_id = item.get("evidence_or_assumption_id")
            overrides.append(
                {
                    **{
                        key: value
                        for key, value in item.items()
                        if key not in {"line_id", "semantic_key"}
                    },
                    "cost_input_id": cost_input_id,
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
