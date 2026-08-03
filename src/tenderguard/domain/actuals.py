from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from tenderguard.domain.enums import (
    ActorRole,
    FindingCode,
    VarianceReason,
    VerificationStatus,
)
from tenderguard.domain.models import DomainModel


class ActualSourceClass(StrEnum):
    ACCEPTANCE_CERTIFICATE = "ACCEPTANCE_CERTIFICATE"
    SUPPLIER_INVOICE = "SUPPLIER_INVOICE"
    ERP_POSTING = "ERP_POSTING"
    FINANCIAL_LEDGER = "FINANCIAL_LEDGER"
    TIMESHEET = "TIMESHEET"
    LOGISTICS_DOCUMENT = "LOGISTICS_DOCUMENT"
    RISK_REGISTER = "RISK_REGISTER"
    AS_BUILT_MEASUREMENT = "AS_BUILT_MEASUREMENT"
    OTHER_CONTROLLED = "OTHER_CONTROLLED"


class ForecastBasis(StrEnum):
    ATOMIC_QUANTITY = "ATOMIC_QUANTITY"
    ATOMIC_UNIT_RATE = "ATOMIC_UNIT_RATE"
    ATOMIC_AMOUNT = "ATOMIC_AMOUNT"
    PROJECT_COST_TOTAL = "PROJECT_COST_TOTAL"


class ActualEvidenceValue(DomainModel):
    actual_key: str = Field(min_length=1, max_length=128)
    entity_type: str = Field(min_length=1, max_length=100)
    entity_id: str = Field(min_length=1, max_length=128)
    metric: str = Field(min_length=1, max_length=100)
    value: Decimal = Field(max_digits=38, decimal_places=12)
    unit: str = Field(min_length=1, max_length=64)
    source_class: ActualSourceClass
    occurred_on: date

    @field_validator("actual_key", "entity_type", "entity_id", "metric", "unit")
    @classmethod
    def identifiers_are_normalized(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("Actual evidence identifiers must be normalized")
        return value


class ActualMetricDefinition(DomainModel):
    metric: str = Field(min_length=1, max_length=100)
    entity_type: str = Field(min_length=1, max_length=100)
    evidence_field_name: str = Field(min_length=1, max_length=300)
    forecast_basis: ForecastBasis
    allowed_units: tuple[str, ...] = Field(min_length=1, max_length=50)
    allowed_source_classes: tuple[ActualSourceClass, ...] = Field(
        min_length=1,
        max_length=20,
    )

    @field_validator(
        "metric",
        "entity_type",
        "evidence_field_name",
    )
    @classmethod
    def strings_are_normalized(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("Actual metric definition values must be normalized")
        return value

    @field_validator("allowed_units")
    @classmethod
    def units_are_unique_and_normalized(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(
            not value or value != value.strip() or len(value) > 64 for value in values
        ):
            raise ValueError("Actual metric units must be unique and normalized")
        return values

    @field_validator("allowed_source_classes")
    @classmethod
    def source_classes_are_unique(
        cls,
        values: tuple[ActualSourceClass, ...],
    ) -> tuple[ActualSourceClass, ...]:
        if len(values) != len(set(values)):
            raise ValueError("Actual source classes must be unique")
        return values

    @model_validator(mode="after")
    def entity_and_forecast_are_compatible(self) -> ActualMetricDefinition:
        atomic = {
            ForecastBasis.ATOMIC_QUANTITY,
            ForecastBasis.ATOMIC_UNIT_RATE,
            ForecastBasis.ATOMIC_AMOUNT,
        }
        if self.forecast_basis in atomic and self.entity_type != "COST_INPUT":
            raise ValueError("Atomic forecast basis requires COST_INPUT entity type")
        if (
            self.forecast_basis is ForecastBasis.PROJECT_COST_TOTAL
            and self.entity_type != "PROJECT"
        ):
            raise ValueError("Project total forecast basis requires PROJECT entity type")
        return self


class ActualsPolicyDefinition(DomainModel):
    metric_definitions: tuple[ActualMetricDefinition, ...] = Field(
        min_length=1,
        max_length=100,
    )
    required_metric_keys: tuple[str, ...] = Field(default=(), max_length=100)
    independently_verified_metric_keys: tuple[str, ...] = Field(
        default=(),
        max_length=100,
    )
    record_roles: tuple[ActorRole, ...] = Field(min_length=1, max_length=8)
    actual_review_role: ActorRole
    variance_classifier_roles: tuple[ActorRole, ...] = Field(
        min_length=1,
        max_length=8,
    )
    variance_review_role: ActorRole
    calibration_approval_role: ActorRole
    project_outcome_field_name: str = Field(min_length=1, max_length=300)
    eligible_project_outcomes: tuple[str, ...] = Field(min_length=1, max_length=20)
    relative_variance_scale: int = Field(ge=0, le=12)
    relative_variance_rounding_mode: str

    @field_validator("required_metric_keys", "independently_verified_metric_keys")
    @classmethod
    def metric_keys_are_unique_and_normalized(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(
            not value
            or value != value.strip()
            or len(value) > 100
            or any(character.isspace() for character in value)
            for value in values
        ):
            raise ValueError("Actual metric keys must be unique and normalized")
        return values

    @field_validator("record_roles", "variance_classifier_roles")
    @classmethod
    def roles_are_unique(cls, values: tuple[ActorRole, ...]) -> tuple[ActorRole, ...]:
        if len(values) != len(set(values)):
            raise ValueError("Actuals policy roles must be unique")
        return values

    @field_validator("project_outcome_field_name")
    @classmethod
    def outcome_field_is_normalized(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("Project outcome field must be normalized")
        return value

    @field_validator("eligible_project_outcomes")
    @classmethod
    def outcomes_are_unique_and_normalized(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(
            not value or value != value.strip() or len(value) > 100 for value in values
        ):
            raise ValueError("Eligible project outcomes must be unique and normalized")
        return values

    @model_validator(mode="after")
    def policy_is_closed_and_segregated(self) -> ActualsPolicyDefinition:
        metrics = tuple(item.metric for item in self.metric_definitions)
        fields = tuple(item.evidence_field_name for item in self.metric_definitions)
        declared = set(metrics)
        if len(metrics) != len(declared) or len(fields) != len(set(fields)):
            raise ValueError("Actual metrics and evidence fields must be unique")
        if not set(self.required_metric_keys).issubset(declared):
            raise ValueError("Required actual metrics must be declared")
        if not set(self.independently_verified_metric_keys).issubset(declared):
            raise ValueError("Independent actual metrics must be declared")
        if any(
            role
            not in {
                ActorRole.ESTIMATOR,
                ActorRole.PROCUREMENT,
                ActorRole.TECHNICAL_EXPERT,
                ActorRole.REVIEWER,
                ActorRole.AUDITOR,
            }
            for role in self.record_roles
        ):
            raise ValueError("Actual record roles contain an unsupported role")
        if self.actual_review_role not in {
            ActorRole.REVIEWER,
            ActorRole.AUDITOR,
            ActorRole.TECHNICAL_EXPERT,
        }:
            raise ValueError("Actual review role is unsupported")
        if any(
            role
            not in {
                ActorRole.REVIEWER,
                ActorRole.AUDITOR,
                ActorRole.TECHNICAL_EXPERT,
            }
            for role in self.variance_classifier_roles
        ):
            raise ValueError("Variance classifier role is unsupported")
        if self.variance_review_role not in {
            ActorRole.REVIEWER,
            ActorRole.AUDITOR,
            ActorRole.TECHNICAL_EXPERT,
        }:
            raise ValueError("Variance review role is unsupported")
        if self.calibration_approval_role is not ActorRole.METHODOLOGY_OWNER:
            raise ValueError("Calibration approval must remain with METHODOLOGY_OWNER")
        if self.relative_variance_rounding_mode not in {
            "ROUND_HALF_UP",
            "ROUND_HALF_EVEN",
        }:
            raise ValueError("Relative variance rounding mode is unsupported")
        return self

    def metric(self, metric: str) -> ActualMetricDefinition:
        result = next(
            (item for item in self.metric_definitions if item.metric == metric),
            None,
        )
        if result is None:
            raise ValueError("Actual metric is outside the approved actuals policy")
        return result


class ForecastFact(DomainModel):
    forecast_id: str
    project_id: str
    entity_id: str
    metric: str
    value: Decimal
    unit: str
    snapshot_id: str
    snapshot_hash: str | None = None
    cost_input_row_id: str | None = None
    forecast_basis: ForecastBasis | None = None


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
    actual_key: str | None = None
    source_class: ActualSourceClass | None = None
    status: VerificationStatus = VerificationStatus.IN_REVIEW


class VarianceRecord(DomainModel):
    forecast_id: str
    actual_id: str
    absolute_variance: Decimal
    relative_variance: Decimal | None
    reason: VarianceReason
    reason_detail: str
    classified_by: str
    status: VerificationStatus = VerificationStatus.IN_REVIEW
    reviewed_by: str | None = None


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
    relative_scale: int | None = None,
    relative_rounding: str = "ROUND_HALF_EVEN",
) -> VarianceRecord:
    if (
        not actual.verified
        or not actual.verified_by
        or actual.status is not VerificationStatus.VERIFIED
    ):
        raise ValueError(FindingCode.ACTUAL_NOT_VERIFIED.value)
    if (
        forecast.project_id != actual.project_id
        or forecast.entity_id != actual.entity_id
        or forecast.metric != actual.metric
    ):
        raise ValueError("Forecast and actual facts describe different entities/metrics")
    if forecast.unit != actual.unit:
        raise ValueError("Forecast and actual units differ")
    if not reason_detail.strip():
        raise ValueError("Variance classification requires a reason detail")
    absolute = actual.value - forecast.value
    relative = absolute / forecast.value if forecast.value != 0 else None
    if relative is not None and relative_scale is not None:
        rounding_modes = {
            "ROUND_HALF_UP": ROUND_HALF_UP,
            "ROUND_HALF_EVEN": ROUND_HALF_EVEN,
        }
        if relative_rounding not in rounding_modes:
            raise ValueError("Relative variance rounding mode is unsupported")
        relative = relative.quantize(
            Decimal(1).scaleb(-relative_scale),
            rounding=rounding_modes[relative_rounding],
        )
    return VarianceRecord(
        forecast_id=forecast.forecast_id,
        actual_id=actual.actual_id,
        absolute_variance=absolute,
        relative_variance=relative,
        reason=reason,
        reason_detail=reason_detail.strip(),
        classified_by=classified_by,
    )


def build_calibration_example(
    forecast: ForecastFact,
    actual: ActualFact,
    variance: VarianceRecord,
) -> CalibrationExample:
    if (
        not actual.verified
        or not actual.verified_by
        or actual.status is not VerificationStatus.VERIFIED
    ):
        raise ValueError("Only verified actual facts may become calibration labels")
    if (
        variance.status is not VerificationStatus.VERIFIED
        or not variance.reviewed_by
        or variance.reviewed_by == variance.classified_by
    ):
        raise ValueError("Only independently approved variances may become calibration labels")
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
