from __future__ import annotations

from datetime import datetime
from decimal import (
    ROUND_DOWN,
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    ROUND_UP,
    Decimal,
    localcontext,
)
from fractions import Fraction
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from tenderguard.config import APPLICATION_BUILD_REFERENCE_PATTERN
from tenderguard.domain.common import content_hash
from tenderguard.domain.models import DomainModel, EvidenceLocation

BUSINESS_QUALIFICATION_PROFILE_SCHEMA = "tenderguard.business-qualification-profile/v1"
BUSINESS_QUALIFICATION_DATASET_SCHEMA = "tenderguard.business-qualification-dataset/v1"
QUALIFICATION_MODES = frozenset({"HISTORICAL", "BLIND", "PARALLEL"})

QualificationMode = Literal["HISTORICAL", "BLIND", "PARALLEL"]
ROUNDING_MODES = {
    "ROUND_HALF_UP": ROUND_HALF_UP,
    "ROUND_HALF_EVEN": ROUND_HALF_EVEN,
    "ROUND_UP": ROUND_UP,
    "ROUND_DOWN": ROUND_DOWN,
}


def _reject_floats(value: Any) -> Any:
    if isinstance(value, float):
        raise ValueError("Floating-point values are forbidden in qualification records")
    if isinstance(value, dict):
        for item in value.values():
            _reject_floats(item)
    elif isinstance(value, list | tuple | set | frozenset):
        for item in value:
            _reject_floats(item)
    return value


class ModeAccuracyThresholds(DomainModel):
    minimum_cases: int = Field(gt=0)
    maximum_case_absolute_percentage_error: Decimal = Field(
        ge=0,
        max_digits=38,
        decimal_places=18,
    )
    maximum_mean_absolute_percentage_error: Decimal = Field(
        ge=0,
        max_digits=38,
        decimal_places=18,
    )
    maximum_absolute_bias_percentage: Decimal = Field(
        ge=0,
        max_digits=38,
        decimal_places=18,
    )
    material_discrepancy_percentage: Decimal = Field(
        ge=0,
        max_digits=38,
        decimal_places=18,
    )

    @model_validator(mode="before")
    @classmethod
    def floating_point_is_forbidden(cls, value: Any) -> Any:
        return _reject_floats(value)


class BusinessQualificationProfile(DomainModel):
    schema_version: Literal["tenderguard.business-qualification-profile/v1"]
    expected_application_build_reference: str
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    comparison_metric: str = Field(min_length=1, max_length=100)
    comparison_basis_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    mode_thresholds: dict[QualificationMode, ModeAccuracyThresholds]
    maximum_exclusion_ratio: Decimal = Field(
        ge=0,
        le=1,
        max_digits=19,
        decimal_places=18,
    )
    minimum_blind_independence_domains: int = Field(gt=0)
    minimum_parallel_span_days: int = Field(gt=0)
    display_scale: int = Field(ge=0, le=18)
    rounding_mode: Literal[
        "ROUND_HALF_UP",
        "ROUND_HALF_EVEN",
        "ROUND_UP",
        "ROUND_DOWN",
    ]
    allowed_discrepancy_reason_codes: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def floating_point_is_forbidden(cls, value: Any) -> Any:
        return _reject_floats(value)

    @field_validator("expected_application_build_reference")
    @classmethod
    def immutable_build_reference(cls, value: str) -> str:
        if APPLICATION_BUILD_REFERENCE_PATTERN.fullmatch(value) is None:
            raise ValueError("Qualification profile build reference is not immutable")
        return value

    @field_validator("allowed_discrepancy_reason_codes")
    @classmethod
    def normalized_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(
            not item
            or item != item.strip()
            or len(item) > 100
            or any(
                not character.isupper() and not character.isdigit() and character != "_"
                for character in item
            )
            for item in value
        ):
            raise ValueError("Discrepancy reason codes must be unique uppercase identifiers")
        return value

    @field_validator("comparison_metric")
    @classmethod
    def normalized_comparison_metric(cls, value: str) -> str:
        if value != value.strip() or any(
            not character.isupper() and not character.isdigit() and character != "_"
            for character in value
        ):
            raise ValueError("Qualification comparison metric must be an uppercase identifier")
        return value

    @model_validator(mode="after")
    def all_modes_are_controlled(self) -> BusinessQualificationProfile:
        if set(self.mode_thresholds) != QUALIFICATION_MODES:
            raise ValueError("Qualification thresholds must define all three modes exactly")
        return self


class QualificationCasePlan(DomainModel):
    case_key: str = Field(min_length=1, max_length=128)
    mode: QualificationMode
    project_id: str = Field(min_length=1, max_length=64)
    snapshot_id: str = Field(min_length=1, max_length=64)
    historical_actual_id: str | None = Field(default=None, max_length=64)
    stratum: str = Field(min_length=1, max_length=200)

    @field_validator("case_key", "project_id", "snapshot_id", "stratum")
    @classmethod
    def normalized_text(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("Qualification case fields must not contain surrounding whitespace")
        return value

    @model_validator(mode="after")
    def reference_plan_matches_mode(self) -> QualificationCasePlan:
        if self.mode == "HISTORICAL" and not self.historical_actual_id:
            raise ValueError("Historical case requires its pre-existing verified actual ID")
        if self.mode != "HISTORICAL" and self.historical_actual_id is not None:
            raise ValueError("Blind/parallel cases cannot pre-bind a revealed reference")
        return self


class ExcludedQualificationCase(DomainModel):
    case_key: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=64)
    reason_code: str = Field(min_length=1, max_length=100)
    evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class BusinessQualificationDataset(DomainModel):
    schema_version: Literal["tenderguard.business-qualification-dataset/v1"]
    population_definition: str = Field(min_length=1, max_length=4000)
    population_evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    selection_method: str = Field(min_length=1, max_length=4000)
    selection_query_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    selection_cutoff_at: datetime
    population_size: int = Field(gt=0)
    cases: tuple[QualificationCasePlan, ...] = Field(min_length=1)
    exclusions: tuple[ExcludedQualificationCase, ...] = ()

    @field_validator("selection_cutoff_at")
    @classmethod
    def cutoff_is_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Qualification dataset cutoff must include a timezone")
        return value

    @model_validator(mode="after")
    def inventory_is_complete_and_unique(self) -> BusinessQualificationDataset:
        case_keys = [item.case_key for item in self.cases]
        snapshot_ids = [item.snapshot_id for item in self.cases]
        exclusion_keys = [item.case_key for item in self.exclusions]
        if (
            len(case_keys) != len(set(case_keys))
            or len(snapshot_ids) != len(set(snapshot_ids))
            or len(exclusion_keys) != len(set(exclusion_keys))
            or set(case_keys) & set(exclusion_keys)
        ):
            raise ValueError("Qualification dataset case inventory is not unique")
        if self.population_size != len(self.cases) + len(self.exclusions):
            raise ValueError("Selected and excluded cases do not cover the declared population")
        return self


class QualificationReferencePayload(DomainModel):
    schema_version: Literal["tenderguard.qualification-reference/v1"]
    case_key: str = Field(min_length=1, max_length=128)
    mode: Literal["BLIND", "PARALLEL"]
    amount: Decimal = Field(gt=0, max_digits=38, decimal_places=12)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    comparison_basis_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reference_kind: Literal["PROFESSIONAL_ESTIMATE", "PARALLEL_ESTIMATE"]
    professional_estimator_id: str = Field(min_length=1, max_length=200)
    independence_domain: str = Field(min_length=1, max_length=200)
    performed_at: datetime
    evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    prepared_by: str = Field(min_length=1, max_length=200)
    reviewed_by: str = Field(min_length=1, max_length=200)
    blinded_to_system_result: bool
    no_bid_authority: bool

    @model_validator(mode="before")
    @classmethod
    def floating_point_is_forbidden(cls, value: Any) -> Any:
        return _reject_floats(value)

    @field_validator("performed_at")
    @classmethod
    def performed_at_is_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Qualification reference timestamp must include a timezone")
        return value

    @model_validator(mode="after")
    def independent_reference_is_consistent(self) -> QualificationReferencePayload:
        if self.prepared_by == self.reviewed_by:
            raise ValueError("Professional reference requires four-eyes review")
        if self.mode == "BLIND" and (
            self.reference_kind != "PROFESSIONAL_ESTIMATE"
            or not self.blinded_to_system_result
            or not self.no_bid_authority
        ):
            raise ValueError("Blind reference lacks blindness/no-authority attestations")
        if self.mode == "PARALLEL" and (
            self.reference_kind != "PARALLEL_ESTIMATE" or not self.no_bid_authority
        ):
            raise ValueError("Parallel reference lacks no-bid-authority attestation")
        return self


class QualificationReferenceEvidenceDraft(DomainModel):
    case_key: str = Field(min_length=1, max_length=128)
    mode: Literal["BLIND", "PARALLEL"]
    amount: Decimal = Field(gt=0, max_digits=38, decimal_places=12)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    comparison_basis_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reference_kind: Literal["PROFESSIONAL_ESTIMATE", "PARALLEL_ESTIMATE"]
    professional_estimator_id: str = Field(min_length=1, max_length=200)
    independence_domain: str = Field(min_length=1, max_length=200)
    performed_at: datetime
    location: EvidenceLocation
    blinded_to_system_result: bool
    no_bid_authority: bool

    @model_validator(mode="before")
    @classmethod
    def floating_point_is_forbidden(cls, value: Any) -> Any:
        return _reject_floats(value)

    @field_validator("performed_at")
    @classmethod
    def performed_at_is_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Qualification reference timestamp must include a timezone")
        return value

    @model_validator(mode="after")
    def evidence_attestations_match_mode(self) -> QualificationReferenceEvidenceDraft:
        if self.mode == "BLIND" and (
            self.reference_kind != "PROFESSIONAL_ESTIMATE"
            or not self.blinded_to_system_result
            or not self.no_bid_authority
        ):
            raise ValueError("Blind reference lacks blindness/no-authority attestations")
        if self.mode == "PARALLEL" and (
            self.reference_kind != "PARALLEL_ESTIMATE" or not self.no_bid_authority
        ):
            raise ValueError("Parallel reference lacks no-bid-authority attestation")
        return self


class QualificationCaseMetric(DomainModel):
    case_id: str
    case_key: str
    mode: QualificationMode
    prediction_total: Decimal
    reference_total: Decimal
    currency: str
    signed_error: Decimal
    absolute_error: Decimal
    signed_percentage_display: Decimal
    absolute_percentage_display: Decimal
    exact_signed_ratio_numerator: int
    exact_signed_ratio_denominator: int
    material: bool


class QualificationMeasurement(DomainModel):
    case_id: str = Field(min_length=1, max_length=64)
    case_key: str = Field(min_length=1, max_length=128)
    mode: QualificationMode
    prediction_total: Decimal = Field(gt=0, max_digits=38, decimal_places=12)
    reference_total: Decimal = Field(gt=0, max_digits=38, decimal_places=12)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    independence_domain: str = Field(min_length=1, max_length=200)
    reference_performed_at: datetime

    @model_validator(mode="before")
    @classmethod
    def floating_point_is_forbidden(cls, value: Any) -> Any:
        return _reject_floats(value)

    @field_validator("reference_performed_at")
    @classmethod
    def reference_timestamp_is_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Qualification measurement timestamp must include a timezone")
        return value


class QualificationModeMetric(DomainModel):
    mode: QualificationMode
    case_count: int
    mean_absolute_percentage_display: Decimal
    absolute_bias_percentage_display: Decimal
    maximum_case_absolute_percentage_display: Decimal
    exact_mape_numerator: int
    exact_mape_denominator: int
    exact_bias_numerator: int
    exact_bias_denominator: int
    passed: bool
    failed_checks: tuple[str, ...]


class BusinessQualificationEvaluation(DomainModel):
    campaign_id: str
    profile_version_id: str
    dataset_version_id: str
    application_build_reference: str
    currency: str
    cases: tuple[QualificationCaseMetric, ...]
    modes: tuple[QualificationModeMetric, ...]
    exclusion_ratio_display: Decimal
    blind_independence_domains: int
    parallel_span_days: int
    metrics_passed: bool
    findings: tuple[str, ...]
    evaluated_at: datetime
    result_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("evaluated_at")
    @classmethod
    def evaluated_at_is_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Qualification evaluation timestamp must include a timezone")
        return value

    @model_validator(mode="after")
    def result_hash_is_self_verifying(self) -> BusinessQualificationEvaluation:
        if self.result_hash != content_hash(
            self.model_dump(mode="python", exclude={"result_hash"})
        ):
            raise ValueError("Qualification evaluation result hash does not verify")
        return self


def evaluate_business_qualification(
    *,
    campaign_id: str,
    profile_version_id: str,
    dataset_version_id: str,
    profile: BusinessQualificationProfile,
    measurements: tuple[QualificationMeasurement, ...],
    population_size: int,
    exclusion_count: int,
    evaluated_at: datetime,
) -> BusinessQualificationEvaluation:
    if not measurements:
        raise ValueError("Business qualification requires measurements")
    if population_size <= 0 or exclusion_count < 0:
        raise ValueError("Qualification population is invalid")
    if population_size != len(measurements) + exclusion_count:
        raise ValueError("Measurements and exclusions do not cover the population")
    case_ids = [item.case_id for item in measurements]
    case_keys = [item.case_key for item in measurements]
    if len(case_ids) != len(set(case_ids)) or len(case_keys) != len(set(case_keys)):
        raise ValueError("Qualification measurements are not unique")
    if any(item.currency != profile.currency for item in measurements):
        raise ValueError("Qualification measurement currency differs from the profile")

    quantum = Decimal(1).scaleb(-profile.display_scale)
    rounding = ROUNDING_MODES[profile.rounding_mode]

    def percentage_display(value: Fraction) -> Decimal:
        with localcontext() as context:
            context.prec = 200
            exact = Decimal(value.numerator) * Decimal(100) / Decimal(value.denominator)
            return exact.quantize(quantum, rounding=rounding)

    case_metrics: list[QualificationCaseMetric] = []
    ratios_by_mode: dict[str, list[Fraction]] = {mode: [] for mode in QUALIFICATION_MODES}
    signed_errors_by_mode: dict[str, list[Fraction]] = {mode: [] for mode in QUALIFICATION_MODES}
    for measurement in sorted(measurements, key=lambda item: item.case_key):
        signed_error = measurement.prediction_total - measurement.reference_total
        signed_ratio = Fraction(signed_error) / Fraction(measurement.reference_total)
        absolute_ratio = abs(signed_ratio)
        material_threshold = (
            Fraction(profile.mode_thresholds[measurement.mode].material_discrepancy_percentage)
            / 100
        )
        ratios_by_mode[measurement.mode].append(absolute_ratio)
        signed_errors_by_mode[measurement.mode].append(
            Fraction(signed_error) / Fraction(measurement.reference_total)
        )
        case_metrics.append(
            QualificationCaseMetric(
                case_id=measurement.case_id,
                case_key=measurement.case_key,
                mode=measurement.mode,
                prediction_total=measurement.prediction_total,
                reference_total=measurement.reference_total,
                currency=measurement.currency,
                signed_error=signed_error,
                absolute_error=abs(signed_error),
                signed_percentage_display=percentage_display(signed_ratio),
                absolute_percentage_display=percentage_display(absolute_ratio),
                exact_signed_ratio_numerator=signed_ratio.numerator,
                exact_signed_ratio_denominator=signed_ratio.denominator,
                material=absolute_ratio >= material_threshold,
            )
        )

    findings: list[str] = []
    mode_metrics: list[QualificationModeMetric] = []
    for mode in sorted(QUALIFICATION_MODES):
        thresholds = profile.mode_thresholds[mode]  # type: ignore[index]
        absolute_ratios = ratios_by_mode[mode]
        signed_ratios = signed_errors_by_mode[mode]
        failed_checks: list[str] = []
        if len(absolute_ratios) < thresholds.minimum_cases:
            failed_checks.append("MINIMUM_CASES")
        if absolute_ratios:
            maximum = max(absolute_ratios)
            mape = sum(absolute_ratios, start=Fraction(0)) / len(absolute_ratios)
            bias = abs(sum(signed_ratios, start=Fraction(0)) / len(signed_ratios))
        else:
            maximum = Fraction(0)
            mape = Fraction(0)
            bias = Fraction(0)
        if maximum > Fraction(thresholds.maximum_case_absolute_percentage_error) / 100:
            failed_checks.append("MAXIMUM_CASE_ABSOLUTE_PERCENTAGE_ERROR")
        if mape > Fraction(thresholds.maximum_mean_absolute_percentage_error) / 100:
            failed_checks.append("MAXIMUM_MEAN_ABSOLUTE_PERCENTAGE_ERROR")
        if bias > Fraction(thresholds.maximum_absolute_bias_percentage) / 100:
            failed_checks.append("MAXIMUM_ABSOLUTE_BIAS_PERCENTAGE")
        mode_metrics.append(
            QualificationModeMetric(
                mode=mode,
                case_count=len(absolute_ratios),
                mean_absolute_percentage_display=percentage_display(mape),
                absolute_bias_percentage_display=percentage_display(bias),
                maximum_case_absolute_percentage_display=percentage_display(maximum),
                exact_mape_numerator=mape.numerator,
                exact_mape_denominator=mape.denominator,
                exact_bias_numerator=bias.numerator,
                exact_bias_denominator=bias.denominator,
                passed=not failed_checks,
                failed_checks=tuple(failed_checks),
            )
        )
        findings.extend(f"{mode}:{check}" for check in failed_checks)

    exclusion_ratio = Fraction(exclusion_count, population_size)
    if exclusion_ratio > Fraction(profile.maximum_exclusion_ratio):
        findings.append("MAXIMUM_EXCLUSION_RATIO")
    blind_domains = len({item.independence_domain for item in measurements if item.mode == "BLIND"})
    if blind_domains < profile.minimum_blind_independence_domains:
        findings.append("MINIMUM_BLIND_INDEPENDENCE_DOMAINS")
    parallel_dates = sorted(
        item.reference_performed_at.date() for item in measurements if item.mode == "PARALLEL"
    )
    parallel_span_days = (parallel_dates[-1] - parallel_dates[0]).days + 1 if parallel_dates else 0
    if parallel_span_days < profile.minimum_parallel_span_days:
        findings.append("MINIMUM_PARALLEL_SPAN_DAYS")

    body = {
        "campaign_id": campaign_id,
        "profile_version_id": profile_version_id,
        "dataset_version_id": dataset_version_id,
        "application_build_reference": profile.expected_application_build_reference,
        "currency": profile.currency,
        "cases": tuple(case_metrics),
        "modes": tuple(mode_metrics),
        "exclusion_ratio_display": percentage_display(exclusion_ratio),
        "blind_independence_domains": blind_domains,
        "parallel_span_days": parallel_span_days,
        "metrics_passed": not findings,
        "findings": tuple(findings),
        "evaluated_at": evaluated_at,
    }
    return BusinessQualificationEvaluation.model_validate(
        {
            **body,
            "result_hash": content_hash(body),
        }
    )
