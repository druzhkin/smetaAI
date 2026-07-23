from datetime import date
from decimal import Decimal

import pytest

from tenderguard.domain.actuals import (
    ActualFact,
    ForecastFact,
    build_calibration_example,
    compare_forecast_to_actual,
)
from tenderguard.domain.contract import (
    ContractAssessment,
    ContractTerm,
    validate_contract,
)
from tenderguard.domain.enums import (
    ContractTermKind,
    VarianceReason,
    VerificationStatus,
)
from tenderguard.domain.risk import RiskItem, RiskPolicy, calculate_risk_reserve


def test_contract_cost_cannot_be_separated_from_unresolved_terms() -> None:
    assessment = ContractAssessment(
        assessment_version="contract-v1",
        terms=(
            ContractTerm(
                term_id="term-penalty",
                kind=ContractTermKind.PENALTIES,
                value="0.1% per day",
                observation_ids=("obs-1",),
                verified=True,
                cost_impact_resolved=False,
            ),
        ),
        required_term_kinds=frozenset({ContractTermKind.PENALTIES, ContractTermKind.RETENTION}),
    )
    codes = {item.code.value for item in validate_contract(assessment)}
    assert "CONTRACT_COST_IMPACT_UNRESOLVED" in codes
    assert "CONTRACT_TERM_MISSING" in codes


def test_correlated_risk_requires_versioned_correlation_model() -> None:
    calculation = calculate_risk_reserve(
        (
            RiskItem(
                risk_id="risk-1",
                description="Currency and supplier exposure",
                probability=Decimal("0.3"),
                impact_min=Decimal("100"),
                impact_most_likely=Decimal("200"),
                impact_max=Decimal("400"),
                currency="RUB",
                observation_ids=("obs-1",),
                status=VerificationStatus.VERIFIED,
                correlated=True,
                correlation_group="imports",
            ),
        ),
        RiskPolicy(
            policy_version="risk-v1",
            method="THREE_POINT_EXPECTED_VALUE",
            currency="RUB",
            rounding_scale=2,
            rounding_mode="ROUND_HALF_UP",
        ),
    )
    assert not calculation.passed
    assert calculation.findings[0].code.value == "RISK_MODEL_INCOMPLETE"


def test_calibration_uses_verified_actual_not_the_system_forecast() -> None:
    forecast = ForecastFact(
        forecast_id="forecast-1",
        project_id="project-1",
        entity_id="pipe-1",
        metric="purchase_price",
        value=Decimal("100"),
        unit="RUB/m",
        snapshot_id="snapshot-1",
    )
    unverified = ActualFact(
        actual_id="actual-1",
        project_id="project-1",
        entity_id="pipe-1",
        metric="purchase_price",
        value=Decimal("112"),
        unit="RUB/m",
        occurred_on=date(2026, 8, 1),
        source_observation_id="invoice-observation-1",
        verified=False,
    )
    with pytest.raises(ValueError, match="ACTUAL_NOT_VERIFIED"):
        compare_forecast_to_actual(
            forecast,
            unverified,
            reason=VarianceReason.PRICE_CHANGE,
            reason_detail="Supplier repriced",
            classified_by="controller-1",
        )

    actual = unverified.model_copy(update={"verified": True, "verified_by": "controller-1"})
    variance = compare_forecast_to_actual(
        forecast,
        actual,
        reason=VarianceReason.PRICE_CHANGE,
        reason_detail="Supplier repriced",
        classified_by="controller-1",
    )
    example = build_calibration_example(forecast, actual, variance)
    assert example.target_value == Decimal("112")
    assert example.features_snapshot_id == "snapshot-1"
