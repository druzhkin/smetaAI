from __future__ import annotations

from datetime import date
from decimal import Decimal

from tenderguard.domain.enums import FindingCode, VarianceReason
from tenderguard.domain.models import DomainModel


class ForecastFact(DomainModel):
    forecast_id: str
    project_id: str
    entity_id: str
    metric: str
    value: Decimal
    unit: str
    snapshot_id: str


class ActualFact(DomainModel):
    actual_id: str
    project_id: str
    entity_id: str
    metric: str
    value: Decimal
    unit: str
    occurred_on: date
    source_observation_id: str
    verified: bool
    verified_by: str | None = None


class VarianceRecord(DomainModel):
    forecast_id: str
    actual_id: str
    absolute_variance: Decimal
    relative_variance: Decimal | None
    reason: VarianceReason
    reason_detail: str
    classified_by: str


class CalibrationExample(DomainModel):
    example_id: str
    project_id: str
    metric: str
    features_snapshot_id: str
    verified_actual_id: str
    target_value: Decimal
    unit: str
    variance_reason: VarianceReason


def compare_forecast_to_actual(
    forecast: ForecastFact,
    actual: ActualFact,
    *,
    reason: VarianceReason,
    reason_detail: str,
    classified_by: str,
) -> VarianceRecord:
    if not actual.verified or not actual.verified_by:
        raise ValueError(FindingCode.ACTUAL_NOT_VERIFIED.value)
    if (
        forecast.project_id != actual.project_id
        or forecast.entity_id != actual.entity_id
        or forecast.metric != actual.metric
    ):
        raise ValueError("Forecast and actual facts describe different entities/metrics")
    if forecast.unit != actual.unit:
        raise ValueError("Forecast and actual units differ")
    absolute = actual.value - forecast.value
    relative = absolute / forecast.value if forecast.value != 0 else None
    return VarianceRecord(
        forecast_id=forecast.forecast_id,
        actual_id=actual.actual_id,
        absolute_variance=absolute,
        relative_variance=relative,
        reason=reason,
        reason_detail=reason_detail,
        classified_by=classified_by,
    )


def build_calibration_example(
    forecast: ForecastFact,
    actual: ActualFact,
    variance: VarianceRecord,
) -> CalibrationExample:
    if not actual.verified or not actual.verified_by:
        raise ValueError("Only verified actual facts may become calibration labels")
    if variance.actual_id != actual.actual_id or variance.forecast_id != forecast.forecast_id:
        raise ValueError("Variance does not link the supplied forecast and actual")
    return CalibrationExample(
        example_id=f"calibration:{forecast.forecast_id}:{actual.actual_id}",
        project_id=actual.project_id,
        metric=actual.metric,
        features_snapshot_id=forecast.snapshot_id,
        verified_actual_id=actual.actual_id,
        target_value=actual.value,
        unit=actual.unit,
        variance_reason=variance.reason,
    )
