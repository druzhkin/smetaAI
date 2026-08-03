from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from tenderguard.application.actuals import (
    ActualDecisionCommand,
    ActualRecordDraft,
    CalibrationDecisionCommand,
    CompareActualCommand,
    VarianceDecisionCommand,
)
from tenderguard.application.risks import RiskItemDecisionCommand
from tenderguard.domain.actuals import (
    ActualEvidenceValue,
    ActualFact,
    ActualMetricDefinition,
    ActualsPolicyDefinition,
    ForecastFact,
    build_calibration_example,
    compare_forecast_to_actual,
)
from tenderguard.domain.contract import (
    ContractAssessment,
    ContractRequirementsPolicy,
    ContractTerm,
    validate_contract,
)
from tenderguard.domain.enums import (
    ActorRole,
    ApprovalDecision,
    ContractTermKind,
    VarianceReason,
    VerificationStatus,
)
from tenderguard.domain.risk import (
    RiskItem,
    RiskModelDefinition,
    RiskPolicy,
    calculate_risk_reserve,
)


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


def test_contract_policy_is_closed_and_has_no_empty_bypass() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        ContractRequirementsPolicy(
            required_term_kinds=frozenset(),
            evidence_field_names={},
        )
    with pytest.raises(ValueError, match="subset"):
        ContractRequirementsPolicy(
            required_term_kinds=frozenset({ContractTermKind.PENALTIES}),
            independently_verified_term_kinds=frozenset({ContractTermKind.RETENTION}),
            evidence_field_names={
                ContractTermKind.PENALTIES: "contract_penalties",
            },
        )
    with pytest.raises(ValueError, match="exactly one evidence field"):
        ContractRequirementsPolicy(
            required_term_kinds=frozenset({ContractTermKind.PENALTIES}),
            evidence_field_names={},
        )
    with pytest.raises(ValueError, match="REVIEWER or TECHNICAL_EXPERT"):
        ContractRequirementsPolicy(
            required_term_kinds=frozenset({ContractTermKind.PENALTIES}),
            evidence_field_names={
                ContractTermKind.PENALTIES: "contract_penalties",
            },
            review_role=ActorRole.ESTIMATOR,
        )


def test_correlation_version_identifier_does_not_fake_an_executable_engine() -> None:
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
            correlation_model_version_id="correlation-v1",
            rounding_scale=2,
            rounding_mode="ROUND_HALF_UP",
        ),
    )
    assert not calculation.passed
    assert calculation.findings[0].code.value == "RISK_MODEL_INCOMPLETE"


def test_risk_model_requires_closed_keys_evidence_and_review_role() -> None:
    base = {
        "policy": {
            "method": "THREE_POINT_EXPECTED_VALUE",
            "currency": "RUB",
            "rounding_scale": 2,
            "rounding_mode": "ROUND_HALF_UP",
        },
        "risk_keys": ["supplier-delay"],
        "required_risk_keys": ["supplier-delay"],
        "minimum_risk_items": 1,
        "independently_verified_risk_keys": [],
        "evidence_field_names": {
            "supplier-delay": "risk_supplier_delay",
        },
        "review_role": "REVIEWER",
        "reserve_unit": "project",
        "reserve_cost_component": {
            "line_id": "boq-risk",
            "semantic_key": "risk-reserve",
        },
    }
    assert RiskModelDefinition.model_validate(base).review_role is ActorRole.REVIEWER
    with pytest.raises(ValueError, match="exactly one evidence field"):
        RiskModelDefinition.model_validate(
            {
                **base,
                "evidence_field_names": {},
            }
        )
    with pytest.raises(ValueError, match="must be required"):
        RiskModelDefinition.model_validate(
            {
                **base,
                "risk_keys": ["supplier-delay", "currency"],
                "independently_verified_risk_keys": ["currency"],
                "evidence_field_names": {
                    "supplier-delay": "risk_supplier_delay",
                    "currency": "risk_currency",
                },
            }
        )
    with pytest.raises(ValueError, match="REVIEWER or TECHNICAL_EXPERT"):
        RiskModelDefinition.model_validate(
            {
                **base,
                "review_role": "ESTIMATOR",
            }
        )


def test_risk_review_rejects_ambiguous_changes_requested_state() -> None:
    with pytest.raises(ValueError, match="APPROVED or REJECTED"):
        RiskItemDecisionCommand(
            decision=ApprovalDecision.CHANGES_REQUESTED,
            expected_risk_updated_at=datetime(2026, 7, 23, tzinfo=UTC),
            expected_task_updated_at=datetime(2026, 7, 23, tzinfo=UTC),
        )


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

    actual = unverified.model_copy(
        update={
            "verified": True,
            "verified_by": "controller-1",
            "status": VerificationStatus.VERIFIED,
        }
    )
    variance = compare_forecast_to_actual(
        forecast,
        actual,
        reason=VarianceReason.PRICE_CHANGE,
        reason_detail="Supplier repriced",
        classified_by="controller-1",
    )
    reviewed_variance = variance.model_copy(
        update={
            "status": VerificationStatus.VERIFIED,
            "reviewed_by": "independent-reviewer",
        }
    )
    example = build_calibration_example(forecast, actual, reviewed_variance)
    assert example.target_value == Decimal("112")
    assert example.features_snapshot_id == "snapshot-1"


def _actual_metric(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "metric": "purchase_price",
        "entity_type": "COST_INPUT",
        "evidence_field_name": "actual_purchase_price",
        "forecast_basis": "ATOMIC_UNIT_RATE",
        "allowed_units": ["RUB/m"],
        "allowed_source_classes": ["SUPPLIER_INVOICE"],
    }
    value.update(overrides)
    return value


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"metric": " purchase_price"}, "normalized"),
        ({"allowed_units": ["RUB/m", "RUB/m"]}, "unique and normalized"),
        (
            {"allowed_source_classes": ["SUPPLIER_INVOICE", "SUPPLIER_INVOICE"]},
            "unique",
        ),
        ({"entity_type": "PROJECT"}, "Atomic forecast basis"),
        (
            {
                "entity_type": "COST_INPUT",
                "forecast_basis": "PROJECT_COST_TOTAL",
            },
            "Project total forecast basis",
        ),
    ],
)
def test_actual_metric_policy_rejects_ambiguous_bases(
    overrides: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        ActualMetricDefinition.model_validate(_actual_metric(**overrides))


def _actuals_policy(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "metric_definitions": [_actual_metric()],
        "required_metric_keys": ["purchase_price"],
        "independently_verified_metric_keys": [],
        "record_roles": ["PROCUREMENT"],
        "actual_review_role": "AUDITOR",
        "variance_classifier_roles": ["REVIEWER"],
        "variance_review_role": "TECHNICAL_EXPERT",
        "calibration_approval_role": "METHODOLOGY_OWNER",
        "project_outcome_field_name": "project_execution_status",
        "eligible_project_outcomes": ["COMPLETED"],
        "relative_variance_scale": 6,
        "relative_variance_rounding_mode": "ROUND_HALF_EVEN",
    }
    value.update(overrides)
    return value


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"required_metric_keys": ["purchase_price", "purchase_price"]}, "unique"),
        ({"record_roles": ["PROCUREMENT", "PROCUREMENT"]}, "roles must be unique"),
        ({"project_outcome_field_name": " completed"}, "normalized"),
        ({"eligible_project_outcomes": ["COMPLETED", "COMPLETED"]}, "unique"),
        (
            {"metric_definitions": [_actual_metric(), _actual_metric()]},
            "metrics and evidence fields must be unique",
        ),
        ({"required_metric_keys": ["unknown"]}, "must be declared"),
        ({"independently_verified_metric_keys": ["unknown"]}, "must be declared"),
        ({"record_roles": ["APPROVER"]}, "unsupported role"),
        ({"actual_review_role": "ESTIMATOR"}, "review role is unsupported"),
        ({"variance_classifier_roles": ["ESTIMATOR"]}, "classifier role is unsupported"),
        ({"variance_review_role": "ESTIMATOR"}, "review role is unsupported"),
        ({"calibration_approval_role": "REVIEWER"}, "METHODOLOGY_OWNER"),
        ({"relative_variance_rounding_mode": "ROUND_DOWN"}, "rounding mode"),
    ],
)
def test_actuals_policy_rejects_open_or_unsegregated_rules(
    overrides: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        ActualsPolicyDefinition.model_validate(_actuals_policy(**overrides))


def test_actuals_policy_rejects_unknown_metric_and_denormalized_evidence() -> None:
    policy = ActualsPolicyDefinition.model_validate(_actuals_policy())
    with pytest.raises(ValueError, match="outside the approved actuals policy"):
        policy.metric("unknown")
    with pytest.raises(ValueError, match="normalized"):
        ActualEvidenceValue(
            actual_key=" invoice-1",
            entity_type="COST_INPUT",
            entity_id="cost-input-1",
            metric="purchase_price",
            value=Decimal("112"),
            unit="RUB/m",
            source_class="SUPPLIER_INVOICE",
            occurred_on=date(2026, 8, 1),
        )


def test_actuals_commands_reject_ambiguous_decisions_and_naive_timestamps() -> None:
    aware = datetime(2026, 7, 23, tzinfo=UTC)
    naive = datetime(2026, 7, 23)
    with pytest.raises(ValueError, match="normalized"):
        ActualRecordDraft(
            metric=" purchase_price",
            source_observation_id="observation-1",
            expected_observation_created_at=aware,
        )
    with pytest.raises(ValueError, match="timezone"):
        ActualRecordDraft(
            metric="purchase_price",
            source_observation_id="observation-1",
            expected_observation_created_at=naive,
        )
    with pytest.raises(ValueError, match="APPROVED or REJECTED"):
        ActualDecisionCommand(
            decision=ApprovalDecision.CHANGES_REQUESTED,
            expected_actual_created_at=aware,
            expected_task_updated_at=aware,
        )
    with pytest.raises(ValueError, match="timezone"):
        ActualDecisionCommand(
            decision=ApprovalDecision.APPROVED,
            expected_actual_created_at=naive,
            expected_task_updated_at=naive,
        )
    with pytest.raises(ValueError, match="reason detail"):
        CompareActualCommand(
            forecast_id="forecast-1",
            released_by_decision_id="release-1",
            reason=VarianceReason.PRICE_CHANGE,
            reason_detail=" ",
            expected_actual_created_at=aware,
            actuals_policy_version_id="actuals-policy-v1",
        )
    with pytest.raises(ValueError, match="timezone"):
        CompareActualCommand(
            forecast_id="forecast-1",
            released_by_decision_id="release-1",
            reason=VarianceReason.PRICE_CHANGE,
            reason_detail="Supplier repriced",
            expected_actual_created_at=naive,
            actuals_policy_version_id="actuals-policy-v1",
        )
    with pytest.raises(ValueError, match="APPROVED or REJECTED"):
        VarianceDecisionCommand(
            decision=ApprovalDecision.CHANGES_REQUESTED,
            expected_variance_created_at=aware,
            expected_task_updated_at=aware,
        )
    with pytest.raises(ValueError, match="timezone"):
        VarianceDecisionCommand(
            decision=ApprovalDecision.APPROVED,
            expected_variance_created_at=naive,
            expected_task_updated_at=naive,
        )
    with pytest.raises(ValueError, match="APPROVED or REJECTED"):
        CalibrationDecisionCommand(
            decision=ApprovalDecision.CHANGES_REQUESTED,
            expected_example_created_at=aware,
            expected_task_updated_at=aware,
        )
    with pytest.raises(ValueError, match="timezone"):
        CalibrationDecisionCommand(
            decision=ApprovalDecision.APPROVED,
            expected_example_created_at=naive,
            expected_task_updated_at=naive,
        )


def test_forecast_actual_comparison_rejects_mismatched_or_uncontrolled_inputs() -> None:
    forecast = ForecastFact(
        forecast_id="forecast-1",
        project_id="project-1",
        entity_id="cost-input-1",
        metric="purchase_price",
        value=Decimal("100"),
        unit="RUB/m",
        snapshot_id="snapshot-1",
    )
    actual = ActualFact(
        actual_id="actual-1",
        project_id="project-1",
        entity_id="cost-input-1",
        metric="purchase_price",
        value=Decimal("112"),
        unit="RUB/m",
        occurred_on=date(2026, 8, 1),
        source_observation_id="observation-1",
        verified=True,
        verified_by="actual-reviewer",
        status=VerificationStatus.VERIFIED,
    )
    with pytest.raises(ValueError, match="different entities"):
        compare_forecast_to_actual(
            forecast,
            actual.model_copy(update={"entity_id": "cost-input-2"}),
            reason=VarianceReason.PRICE_CHANGE,
            reason_detail="Supplier repriced",
            classified_by="classifier",
        )
    with pytest.raises(ValueError, match="units differ"):
        compare_forecast_to_actual(
            forecast,
            actual.model_copy(update={"unit": "RUB/kg"}),
            reason=VarianceReason.PRICE_CHANGE,
            reason_detail="Supplier repriced",
            classified_by="classifier",
        )
    with pytest.raises(ValueError, match="reason detail"):
        compare_forecast_to_actual(
            forecast,
            actual,
            reason=VarianceReason.PRICE_CHANGE,
            reason_detail=" ",
            classified_by="classifier",
        )
    with pytest.raises(ValueError, match="rounding mode"):
        compare_forecast_to_actual(
            forecast,
            actual,
            reason=VarianceReason.PRICE_CHANGE,
            reason_detail="Supplier repriced",
            classified_by="classifier",
            relative_scale=6,
            relative_rounding="ROUND_DOWN",
        )
    with pytest.raises(ValueError, match="ACTUAL_NOT_VERIFIED"):
        compare_forecast_to_actual(
            forecast,
            actual.model_copy(update={"status": VerificationStatus.IN_REVIEW}),
            reason=VarianceReason.PRICE_CHANGE,
            reason_detail="Supplier repriced",
            classified_by="classifier",
        )
    zero_forecast = forecast.model_copy(update={"value": Decimal("0")})
    zero_variance = compare_forecast_to_actual(
        zero_forecast,
        actual,
        reason=VarianceReason.PRICE_CHANGE,
        reason_detail="No released forecast basis",
        classified_by="classifier",
        relative_scale=6,
    )
    assert zero_variance.relative_variance is None


def test_calibration_rejects_unverified_or_mismatched_inputs() -> None:
    forecast = ForecastFact(
        forecast_id="forecast-1",
        project_id="project-1",
        entity_id="cost-input-1",
        metric="purchase_price",
        value=Decimal("100"),
        unit="RUB/m",
        snapshot_id="snapshot-1",
    )
    actual = ActualFact(
        actual_id="actual-1",
        project_id="project-1",
        entity_id="cost-input-1",
        metric="purchase_price",
        value=Decimal("112"),
        unit="RUB/m",
        occurred_on=date(2026, 8, 1),
        source_observation_id="observation-1",
        verified=True,
        verified_by="actual-reviewer",
        status=VerificationStatus.VERIFIED,
    )
    variance = compare_forecast_to_actual(
        forecast,
        actual,
        reason=VarianceReason.PRICE_CHANGE,
        reason_detail="Supplier repriced",
        classified_by="classifier",
    )
    reviewed = variance.model_copy(
        update={
            "status": VerificationStatus.VERIFIED,
            "reviewed_by": "variance-reviewer",
        }
    )
    with pytest.raises(ValueError, match="verified actual"):
        build_calibration_example(
            forecast,
            actual.model_copy(update={"verified": False, "verified_by": None}),
            reviewed,
        )
    with pytest.raises(ValueError, match="verified actual"):
        build_calibration_example(
            forecast,
            actual.model_copy(update={"status": VerificationStatus.IN_REVIEW}),
            reviewed,
        )
    with pytest.raises(ValueError, match="approved variances"):
        build_calibration_example(forecast, actual, variance)
    with pytest.raises(ValueError, match="approved variances"):
        build_calibration_example(
            forecast,
            actual,
            reviewed.model_copy(update={"reviewed_by": reviewed.classified_by}),
        )
    with pytest.raises(ValueError, match="does not link"):
        build_calibration_example(
            forecast,
            actual,
            reviewed.model_copy(update={"forecast_id": "forecast-2"}),
        )
