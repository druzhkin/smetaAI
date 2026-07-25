from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from tenderguard.domain.business_qualification import (
    BusinessQualificationDataset,
    BusinessQualificationEvaluation,
    BusinessQualificationProfile,
    QualificationMeasurement,
    evaluate_business_qualification,
)

BUILD_REFERENCE = "git:" + ("a" * 40)
COMPARISON_BASIS_HASH = "b" * 64


def _profile(
    *,
    maximum_case_error: str = "10",
    material_discrepancy: str = "5",
) -> BusinessQualificationProfile:
    threshold = {
        "minimum_cases": 1,
        "maximum_case_absolute_percentage_error": maximum_case_error,
        "maximum_mean_absolute_percentage_error": "10",
        "maximum_absolute_bias_percentage": "10",
        "material_discrepancy_percentage": material_discrepancy,
    }
    return BusinessQualificationProfile.model_validate(
        {
            "schema_version": "tenderguard.business-qualification-profile/v1",
            "expected_application_build_reference": BUILD_REFERENCE,
            "currency": "RUB",
            "comparison_metric": "PROJECT_TOTAL_COST",
            "comparison_basis_hash": COMPARISON_BASIS_HASH,
            "mode_thresholds": {
                "HISTORICAL": threshold,
                "BLIND": threshold,
                "PARALLEL": threshold,
            },
            "maximum_exclusion_ratio": "0.25",
            "minimum_blind_independence_domains": 1,
            "minimum_parallel_span_days": 1,
            "display_scale": 4,
            "rounding_mode": "ROUND_HALF_UP",
            "allowed_discrepancy_reason_codes": ["SCOPE_VARIANCE"],
        }
    )


def _measurement(
    *,
    case_id: str,
    mode: str,
    prediction: str,
    reference: str = "100",
    domain: str = "domain-a",
    day: int = 1,
) -> QualificationMeasurement:
    return QualificationMeasurement.model_validate(
        {
            "case_id": case_id,
            "case_key": f"key-{case_id}",
            "mode": mode,
            "prediction_total": prediction,
            "reference_total": reference,
            "currency": "RUB",
            "independence_domain": domain,
            "reference_performed_at": datetime(2026, 1, day, tzinfo=UTC),
        }
    )


def test_business_qualification_uses_exact_unrounded_boundaries() -> None:
    evaluation = evaluate_business_qualification(
        campaign_id="campaign-1",
        profile_version_id="profile-1",
        dataset_version_id="dataset-1",
        profile=_profile(maximum_case_error="10"),
        measurements=(
            _measurement(case_id="historical", mode="HISTORICAL", prediction="110"),
            _measurement(case_id="blind", mode="BLIND", prediction="90"),
            _measurement(case_id="parallel", mode="PARALLEL", prediction="110"),
        ),
        population_size=3,
        exclusion_count=0,
        evaluated_at=datetime(2026, 2, 1, tzinfo=UTC),
    )

    assert evaluation.metrics_passed is True
    assert all(metric.passed for metric in evaluation.modes)
    assert all(
        metric.absolute_percentage_display == Decimal("10.0000") for metric in evaluation.cases
    )
    assert all(metric.material for metric in evaluation.cases)
    assert (
        BusinessQualificationEvaluation.model_validate(evaluation.model_dump(mode="json"))
        == evaluation
    )


def test_business_qualification_does_not_hide_failure_with_display_rounding() -> None:
    evaluation = evaluate_business_qualification(
        campaign_id="campaign-1",
        profile_version_id="profile-1",
        dataset_version_id="dataset-1",
        profile=_profile(maximum_case_error="10"),
        measurements=(
            _measurement(
                case_id="historical",
                mode="HISTORICAL",
                prediction="110.00001",
            ),
            _measurement(case_id="blind", mode="BLIND", prediction="100"),
            _measurement(case_id="parallel", mode="PARALLEL", prediction="100"),
        ),
        population_size=3,
        exclusion_count=0,
        evaluated_at=datetime(2026, 2, 1, tzinfo=UTC),
    )

    assert evaluation.metrics_passed is False
    historical_case = next(metric for metric in evaluation.cases if metric.mode == "HISTORICAL")
    assert historical_case.absolute_percentage_display == Decimal("10.0000")
    historical = next(metric for metric in evaluation.modes if metric.mode == "HISTORICAL")
    assert "MAXIMUM_CASE_ABSOLUTE_PERCENTAGE_ERROR" in historical.failed_checks


def test_business_qualification_formats_large_exact_ratios_without_context_overflow() -> None:
    evaluation = evaluate_business_qualification(
        campaign_id="campaign-large-ratio",
        profile_version_id="profile-1",
        dataset_version_id="dataset-1",
        profile=_profile(),
        measurements=(
            _measurement(
                case_id="historical",
                mode="HISTORICAL",
                prediction="99999999999999999999999999",
                reference="0.000000000001",
            ),
            _measurement(case_id="blind", mode="BLIND", prediction="100"),
            _measurement(case_id="parallel", mode="PARALLEL", prediction="100"),
        ),
        population_size=3,
        exclusion_count=0,
        evaluated_at=datetime(2026, 2, 1, tzinfo=UTC),
    )

    historical = next(metric for metric in evaluation.cases if metric.mode == "HISTORICAL")
    assert historical.absolute_percentage_display.is_finite()
    assert evaluation.metrics_passed is False


def test_business_qualification_result_hash_is_self_verifying() -> None:
    evaluation = evaluate_business_qualification(
        campaign_id="campaign-1",
        profile_version_id="profile-1",
        dataset_version_id="dataset-1",
        profile=_profile(),
        measurements=(
            _measurement(case_id="historical", mode="HISTORICAL", prediction="100"),
            _measurement(case_id="blind", mode="BLIND", prediction="100"),
            _measurement(case_id="parallel", mode="PARALLEL", prediction="100"),
        ),
        population_size=3,
        exclusion_count=0,
        evaluated_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    tampered = evaluation.model_dump(mode="json")
    tampered["metrics_passed"] = False

    with pytest.raises(ValidationError, match="result hash does not verify"):
        BusinessQualificationEvaluation.model_validate(tampered)


def test_business_qualification_rejects_float_thresholds_and_incomplete_inventory() -> None:
    raw_profile = _profile().model_dump(mode="json")
    raw_profile["maximum_exclusion_ratio"] = 0.1
    with pytest.raises(ValidationError, match="Floating-point"):
        BusinessQualificationProfile.model_validate(raw_profile)

    with pytest.raises(ValidationError, match="do not cover"):
        BusinessQualificationDataset.model_validate(
            {
                "schema_version": "tenderguard.business-qualification-dataset/v1",
                "population_definition": "Closed population",
                "population_evidence_hash": "1" * 64,
                "selection_method": "Deterministic stratified selection",
                "selection_query_hash": "2" * 64,
                "selection_cutoff_at": datetime(2026, 1, 1, tzinfo=UTC),
                "population_size": 2,
                "cases": [
                    {
                        "case_key": "case-1",
                        "mode": "HISTORICAL",
                        "project_id": "project-1",
                        "snapshot_id": "snapshot-1",
                        "historical_actual_id": "actual-1",
                        "stratum": "water",
                    }
                ],
            }
        )
