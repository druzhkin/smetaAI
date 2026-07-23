from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from tenderguard.application.projects import ProjectService
from tenderguard.config import Settings
from tenderguard.domain.actuals import (
    ActualFact,
    CalibrationExample,
    ForecastFact,
    VarianceRecord,
    build_calibration_example,
    compare_forecast_to_actual,
)
from tenderguard.domain.calculation import AtomicCostInput
from tenderguard.domain.common import content_hash, utc_now
from tenderguard.domain.enums import (
    ActorRole,
    ApprovalState,
    VarianceReason,
    VerificationStatus,
)
from tenderguard.domain.models import DomainModel
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.object_store import ObjectStore
from tenderguard.infrastructure.orm import (
    ActualRecordRow,
    CalculationSnapshotRow,
    CalibrationExampleRow,
    CostInputRow,
    ObservationRow,
    VarianceRecordRow,
)


class ActualRecordDraft(DomainModel):
    actual_key: str = Field(min_length=1, max_length=128)
    entity_type: str = Field(min_length=1, max_length=100)
    entity_id: str = Field(min_length=1, max_length=128)
    metric: str = Field(min_length=1, max_length=100)
    value: Decimal
    unit: str = Field(min_length=1, max_length=64)
    source_observation_id: str = Field(min_length=1)
    occurred_on: date


class ActualRecordView(DomainModel):
    actual: ActualFact
    actual_key: str
    supersedes_actual_id: str | None
    is_current: bool


class CompareActualCommand(DomainModel):
    snapshot_id: str = Field(min_length=1)
    cost_input_row_id: str = Field(min_length=1)
    forecast_metric: str = Field(pattern=r"^(quantity|unit_rate)$")
    reason: VarianceReason
    reason_detail: str = Field(min_length=1)


class ActualComparisonResult(DomainModel):
    forecast: ForecastFact
    actual: ActualFact
    variance: VarianceRecord
    variance_record_id: str
    calibration_example: CalibrationExample
    calibration_approved: bool


class ActualsService:
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

    def record_actual(
        self,
        *,
        actor: Actor,
        project_id: str,
        draft: ActualRecordDraft,
        request_id: str,
        reason: str,
    ) -> ActualRecordView:
        actor.require_any(
            ActorRole.ESTIMATOR,
            ActorRole.PROCUREMENT,
            ActorRole.TECHNICAL_EXPERT,
            ActorRole.AUDITOR,
            ActorRole.ADMIN,
        )
        project = self._require_post_award_state(actor, project_id)
        observation = self.session.scalar(
            select(ObservationRow).where(
                ObservationRow.id == draft.source_observation_id,
                ObservationRow.project_id == project.id,
                ObservationRow.status == VerificationStatus.VERIFIED.value,
            )
        )
        if observation is None:
            raise ValueError("Actual fact requires a verified project evidence observation")
        raw = observation.payload.get("observation", {})
        try:
            observed_value = Decimal(str(raw.get("value")))
        except (InvalidOperation, TypeError, ValueError) as error:
            raise ValueError("Actual evidence value is not a valid decimal") from error
        if observed_value != draft.value or raw.get("unit") != draft.unit:
            raise ValueError("Actual evidence does not reproduce value and unit")
        previous = self.session.scalar(
            select(ActualRecordRow)
            .where(
                ActualRecordRow.project_id == project.id,
                ActualRecordRow.actual_key == draft.actual_key,
                ActualRecordRow.is_current.is_(True),
            )
            .with_for_update()
        )
        now = utc_now()
        if previous is not None:
            previous.is_current = False
        row = ActualRecordRow(
            id=f"actual-{uuid4()}",
            project_id=project.id,
            actual_key=draft.actual_key,
            entity_type=draft.entity_type,
            entity_id=draft.entity_id,
            metric=draft.metric,
            value=draft.value,
            unit=draft.unit,
            verified=False,
            source_observation_id=draft.source_observation_id,
            occurred_on=draft.occurred_on,
            payload={
                "created_by": actor.actor_id,
                "evidence_location": raw.get("location"),
            },
            supersedes_actual_id=previous.id if previous else None,
            is_current=True,
            created_at=now,
        )
        self.session.add(row)
        self._audit(
            project.id,
            actor,
            request_id,
            reason,
            "actual_fact_recorded",
            {
                "actual_id": row.id,
                "actual_key": row.actual_key,
                "entity_type": row.entity_type,
                "entity_id": row.entity_id,
                "metric": row.metric,
                "value": row.value,
                "unit": row.unit,
                "source_observation_id": row.source_observation_id,
                "supersedes_actual_id": row.supersedes_actual_id,
            },
        )
        return self._view(row)

    def verify_actual(
        self,
        *,
        actor: Actor,
        project_id: str,
        actual_id: str,
        request_id: str,
        reason: str,
    ) -> ActualRecordView:
        actor.require_any(ActorRole.REVIEWER, ActorRole.AUDITOR)
        project = self._require_post_award_state(actor, project_id)
        row = self.session.scalar(
            select(ActualRecordRow)
            .where(
                ActualRecordRow.id == actual_id,
                ActualRecordRow.project_id == project.id,
                ActualRecordRow.is_current.is_(True),
            )
            .with_for_update()
        )
        if row is None:
            raise LookupError(actual_id)
        if row.verified:
            raise ValueError("Actual fact is already verified")
        if row.payload.get("created_by") == actor.actor_id:
            raise ValueError("Actual fact verification requires a different actor")
        now = utc_now()
        row.verified = True
        row.payload = {
            **row.payload,
            "verified_by": actor.actor_id,
            "verified_at": now.isoformat(),
        }
        self._audit(
            project.id,
            actor,
            request_id,
            reason,
            "actual_fact_verified",
            {
                "actual_id": row.id,
                "actual_key": row.actual_key,
                "verified_by": actor.actor_id,
            },
        )
        return self._view(row)

    def compare_to_forecast(
        self,
        *,
        actor: Actor,
        project_id: str,
        actual_id: str,
        command: CompareActualCommand,
        request_id: str,
        reason: str,
    ) -> ActualComparisonResult:
        actor.require_any(ActorRole.REVIEWER, ActorRole.AUDITOR)
        project = self._require_post_award_state(actor, project_id)
        row = self.session.scalar(
            select(ActualRecordRow).where(
                ActualRecordRow.id == actual_id,
                ActualRecordRow.project_id == project.id,
                ActualRecordRow.is_current.is_(True),
            )
        )
        if row is None:
            raise LookupError(actual_id)
        actual = self._actual_fact(row)
        snapshot = self.session.scalar(
            select(CalculationSnapshotRow).where(
                CalculationSnapshotRow.id == command.snapshot_id,
                CalculationSnapshotRow.project_id == project.id,
                CalculationSnapshotRow.fixed.is_(True),
            )
        )
        if snapshot is None:
            raise ValueError("Forecast comparison requires a fixed project snapshot")
        cost_row = self.session.scalar(
            select(CostInputRow).where(
                CostInputRow.id == command.cost_input_row_id,
                CostInputRow.project_id == project.id,
                CostInputRow.calculation_run_id == snapshot.calculation_run_id,
            )
        )
        if cost_row is None:
            raise ValueError("Forecast cost input is not part of the selected snapshot")
        cost_input = AtomicCostInput.model_validate(cost_row.payload)
        if row.entity_type != "COST_INPUT" or row.entity_id != cost_row.id:
            raise ValueError("Actual fact does not identify the selected forecast cost input")
        if row.metric != command.forecast_metric:
            raise ValueError("Actual metric differs from the selected forecast metric")
        if command.forecast_metric == "quantity":
            forecast_value = cost_input.quantity
            forecast_unit = cost_input.unit
        else:
            forecast_value = cost_input.unit_rate
            forecast_unit = f"{cost_input.currency}/{cost_input.unit}"
        forecast_identity = {
            "snapshot_id": snapshot.id,
            "cost_input_id": cost_row.id,
            "metric": command.forecast_metric,
        }
        forecast = ForecastFact(
            forecast_id=f"forecast-{content_hash(forecast_identity)[:24]}",
            project_id=project.id,
            entity_id=cost_row.id,
            metric=command.forecast_metric,
            value=forecast_value,
            unit=forecast_unit,
            snapshot_id=snapshot.id,
        )
        variance = compare_forecast_to_actual(
            forecast,
            actual,
            reason=command.reason,
            reason_detail=command.reason_detail,
            classified_by=actor.actor_id,
        )
        existing = self.session.scalar(
            select(VarianceRecordRow).where(
                VarianceRecordRow.actual_record_id == row.id,
            )
        )
        if existing is not None:
            raise ValueError("Current actual fact already has a variance classification")
        now = utc_now()
        variance_id = f"variance-{uuid4()}"
        self.session.add(
            VarianceRecordRow(
                id=variance_id,
                project_id=project.id,
                actual_record_id=row.id,
                snapshot_id=snapshot.id,
                metric=command.forecast_metric,
                reason=variance.reason.value,
                absolute_variance=variance.absolute_variance,
                relative_variance=variance.relative_variance,
                payload=variance.model_dump(mode="json"),
                classified_by=actor.actor_id,
                created_at=now,
            )
        )
        calibration = build_calibration_example(forecast, actual, variance)
        calibration = calibration.model_copy(
            update={
                "example_id": f"calibration-{content_hash(calibration)[:24]}",
            }
        )
        self.session.add(
            CalibrationExampleRow(
                id=calibration.example_id,
                project_id=project.id,
                actual_record_id=row.id,
                variance_record_id=variance_id,
                features_snapshot_id=snapshot.id,
                metric=calibration.metric,
                target_value=calibration.target_value,
                unit=calibration.unit,
                approved=False,
                payload={
                    "calibration_example": calibration.model_dump(mode="json"),
                    "created_by": actor.actor_id,
                },
                created_at=now,
            )
        )
        self._audit(
            project.id,
            actor,
            request_id,
            reason,
            "forecast_actual_variance_classified",
            {
                "actual_id": row.id,
                "forecast_id": forecast.forecast_id,
                "snapshot_id": snapshot.id,
                "cost_input_row_id": cost_row.id,
                "variance_record_id": variance_id,
                "calibration_example_id": calibration.example_id,
                "variance_reason": variance.reason,
            },
        )
        return ActualComparisonResult(
            forecast=forecast,
            actual=actual,
            variance=variance,
            variance_record_id=variance_id,
            calibration_example=calibration,
            calibration_approved=False,
        )

    def approve_calibration_example(
        self,
        *,
        actor: Actor,
        project_id: str,
        example_id: str,
        request_id: str,
        reason: str,
    ) -> CalibrationExample:
        actor.require_any(ActorRole.METHODOLOGY_OWNER)
        project = self._require_post_award_state(actor, project_id)
        row = self.session.scalar(
            select(CalibrationExampleRow)
            .where(
                CalibrationExampleRow.id == example_id,
                CalibrationExampleRow.project_id == project.id,
            )
            .with_for_update()
        )
        if row is None:
            raise LookupError(example_id)
        if row.approved:
            raise ValueError("Calibration example is already approved")
        variance = self.session.get(VarianceRecordRow, row.variance_record_id)
        if variance is None:
            raise RuntimeError("Calibration example has no variance record")
        if variance.classified_by == actor.actor_id:
            raise ValueError("Calibration approval requires a different actor")
        row.approved = True
        now = utc_now()
        row.payload = {
            **row.payload,
            "approved_by": actor.actor_id,
            "approved_at": now.isoformat(),
            "approval_reason": reason,
        }
        self._audit(
            project.id,
            actor,
            request_id,
            reason,
            "calibration_example_approved",
            {
                "calibration_example_id": row.id,
                "actual_record_id": row.actual_record_id,
                "variance_record_id": row.variance_record_id,
                "approved_by": actor.actor_id,
            },
        )
        return CalibrationExample.model_validate(row.payload["calibration_example"])

    def _require_post_award_state(self, actor: Actor, project_id: str) -> Any:
        project = self._project_service().get_project(
            actor=actor,
            project_id=project_id,
            lock=True,
        )
        if ApprovalState(project.state) not in {
            ApprovalState.APPROVED_FOR_INTERNAL_USE,
            ApprovalState.APPROVED_FOR_BID,
            ApprovalState.SUPERSEDED,
            ApprovalState.ARCHIVED,
        }:
            raise ValueError("Actual facts may only be recorded in a post-award project state")
        return project

    def _audit(
        self,
        project_id: str,
        actor: Actor,
        request_id: str,
        reason: str,
        event_type: str,
        payload: dict[str, object],
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

    def _project_service(self) -> ProjectService:
        return ProjectService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        )

    @staticmethod
    def _actual_fact(row: ActualRecordRow) -> ActualFact:
        return ActualFact(
            actual_id=row.id,
            project_id=row.project_id,
            entity_id=row.entity_id,
            metric=row.metric,
            value=row.value,
            unit=row.unit,
            occurred_on=row.occurred_on,
            source_observation_id=row.source_observation_id,
            verified=row.verified,
            verified_by=row.payload.get("verified_by"),
        )

    @classmethod
    def _view(cls, row: ActualRecordRow) -> ActualRecordView:
        return ActualRecordView(
            actual=cls._actual_fact(row),
            actual_key=row.actual_key,
            supersedes_actual_id=row.supersedes_actual_id,
            is_current=row.is_current,
        )
