from datetime import date
from decimal import Decimal

import pytest

from tenderguard.domain.commercial_costs import (
    CommercialCostModelInput,
    CommercialCostPolicy,
    ContractCashFlow,
    ContractFinancePlan,
    FundingRatePeriod,
    GuaranteeFee,
    HandlingCost,
    LogisticsAncillaryCost,
    LogisticsPlan,
    MobilisationCost,
    MobilisationPlan,
    StorageCost,
    TransportLeg,
    evaluate_commercial_cost,
)
from tenderguard.domain.enums import (
    CommercialCostModelKind,
    ContractCashFlowKind,
    ContractTermKind,
    GuaranteeKind,
    LogisticsAncillaryKind,
    LogisticsHandlingKind,
    MobilisationComponentKind,
)


def policy(
    *,
    required_model_kinds: frozenset[CommercialCostModelKind] | None = None,
) -> CommercialCostPolicy:
    return CommercialCostPolicy(
        policy_version="commercial-cost-policy-v1",
        currency="RUB",
        line_rounding_scale=2,
        total_rounding_scale=2,
        rounding_mode="ROUND_HALF_UP",
        independent_tolerance=Decimal("0.00"),
        day_count_basis=365,
        max_finance_horizon_days=3650,
        max_model_components=1000,
        allow_zero_total=False,
        required_model_kinds=required_model_kinds or frozenset(),
        required_logistics_sections=frozenset({"TRANSPORT", "HANDLING", "STORAGE", "ANCILLARY"}),
        required_mobilisation_kinds=frozenset(
            {
                MobilisationComponentKind.PLANT_OUTBOUND,
                MobilisationComponentKind.ACCOMMODATION,
            }
        ),
        required_cash_flow_kinds=frozenset(
            {
                ContractCashFlowKind.DIRECT_COST,
                ContractCashFlowKind.CUSTOMER_PAYMENT,
            }
        ),
        required_contract_term_kinds=frozenset(
            {
                ContractTermKind.ADVANCE,
                ContractTermKind.RETENTION,
            }
        ),
    )


def logistics_model() -> CommercialCostModelInput:
    return CommercialCostModelInput(
        model_kind=CommercialCostModelKind.LOGISTICS,
        currency="RUB",
        target_line_id="line-logistics",
        target_semantic_key="project-logistics",
        logistics=LogisticsPlan(
            transport_legs=(
                TransportLeg(
                    component_id="leg-1",
                    mode="ROAD",
                    origin="Factory",
                    destination="Site",
                    distance_km=Decimal("100"),
                    charged_distance_factor=Decimal("2"),
                    cargo_mass_tonnes=Decimal("21"),
                    vehicle_mass_capacity_tonnes=Decimal("10"),
                    fixed_cost_per_trip=Decimal("1000"),
                    rate_per_vehicle_km=Decimal("10"),
                    toll_per_trip=Decimal("100"),
                    route_observation_ids=("obs-route",),
                    cargo_observation_ids=("obs-cargo",),
                    rate_observation_ids=("obs-rate",),
                ),
            ),
            handling_costs=(
                HandlingCost(
                    component_id="handling-1",
                    kind=LogisticsHandlingKind.TRANSSHIPMENT,
                    quantity=Decimal("21"),
                    unit="t",
                    operation_count=2,
                    unit_rate=Decimal("50"),
                    observation_ids=("obs-handling",),
                ),
            ),
            storage_costs=(
                StorageCost(
                    component_id="storage-1",
                    quantity=Decimal("21"),
                    unit="t",
                    duration_days=Decimal("3"),
                    rate_per_unit_day=Decimal("10"),
                    observation_ids=("obs-storage",),
                ),
            ),
            ancillary_costs=(
                LogisticsAncillaryCost(
                    component_id="permit-1",
                    kind=LogisticsAncillaryKind.PERMIT,
                    quantity=Decimal("1"),
                    unit="lot",
                    unit_rate=Decimal("500"),
                    observation_ids=("obs-permit",),
                ),
            ),
        ),
    )


def test_detailed_logistics_and_mobilisation_recalculate_independently() -> None:
    logistics = evaluate_commercial_cost(
        logistics_model(),
        policy(),
        engine_version="commercial-primary-v1",
        validator_version="commercial-independent-v1",
    )
    assert logistics.primary.total == Decimal("12530.00")
    assert logistics.primary.lines[0].details["trip_count"] == 3
    assert logistics.independent.passed
    assert logistics.independent.difference == Decimal("0.00")

    mobilisation_model = CommercialCostModelInput(
        model_kind=CommercialCostModelKind.MOBILISATION,
        currency="RUB",
        target_line_id="line-mobilisation",
        target_semantic_key="project-mobilisation",
        mobilisation=MobilisationPlan(
            components=(
                MobilisationCost(
                    component_id="plant-outbound",
                    kind=MobilisationComponentKind.PLANT_OUTBOUND,
                    description="Move two plant units in two convoys",
                    quantity=Decimal("2"),
                    unit="plant",
                    occurrence_count=Decimal("2"),
                    duration_days=Decimal("1"),
                    unit_rate=Decimal("1000"),
                    observation_ids=("obs-plant-move",),
                ),
                MobilisationCost(
                    component_id="accommodation",
                    kind=MobilisationComponentKind.ACCOMMODATION,
                    description="Initial mobilisation accommodation",
                    quantity=Decimal("10"),
                    unit="person",
                    occurrence_count=Decimal("1"),
                    duration_days=Decimal("5"),
                    unit_rate=Decimal("100"),
                    observation_ids=("obs-accommodation",),
                ),
            )
        ),
    )
    mobilisation = evaluate_commercial_cost(
        mobilisation_model,
        policy(),
        engine_version="commercial-primary-v1",
        validator_version="commercial-independent-v1",
    )
    assert mobilisation.primary.total == Decimal("9000.00")
    assert mobilisation.independent.passed


def test_contract_finance_uses_dated_cash_flow_and_piecewise_daily_validation() -> None:
    model = CommercialCostModelInput(
        model_kind=CommercialCostModelKind.CONTRACT_FINANCE,
        currency="RUB",
        target_line_id="line-finance",
        target_semantic_key="contract-finance",
        related_contract_term_ids=("term-advance", "term-retention"),
        contract_finance=ContractFinancePlan(
            valuation_start=date(2026, 1, 1),
            valuation_end=date(2026, 1, 11),
            cash_flows=(
                ContractCashFlow(
                    cash_flow_id="direct-cost",
                    kind=ContractCashFlowKind.DIRECT_COST,
                    cash_date=date(2026, 1, 1),
                    amount=Decimal("-1000"),
                    observation_ids=("obs-direct-cost",),
                ),
                ContractCashFlow(
                    cash_flow_id="advance",
                    kind=ContractCashFlowKind.ADVANCE_RECEIPT,
                    cash_date=date(2026, 1, 3),
                    amount=Decimal("400"),
                    observation_ids=("obs-advance",),
                    contract_term_ids=("term-advance",),
                ),
                ContractCashFlow(
                    cash_flow_id="customer-payment",
                    kind=ContractCashFlowKind.CUSTOMER_PAYMENT,
                    cash_date=date(2026, 1, 10),
                    amount=Decimal("600"),
                    observation_ids=("obs-payment",),
                    contract_term_ids=("term-retention",),
                ),
            ),
            funding_rate_periods=(
                FundingRatePeriod(
                    rate_period_id="funding-rate",
                    starts_on=date(2026, 1, 1),
                    ends_on=date(2026, 1, 11),
                    annual_rate=Decimal("0.365"),
                    observation_ids=("obs-funding-rate",),
                ),
            ),
            guarantee_fees=(
                GuaranteeFee(
                    guarantee_id="performance-guarantee",
                    kind=GuaranteeKind.PERFORMANCE_SECURITY,
                    notional_amount=Decimal("1000"),
                    annual_rate=Decimal("0.365"),
                    starts_on=date(2026, 1, 1),
                    ends_on=date(2026, 1, 11),
                    observation_ids=("obs-guarantee",),
                ),
            ),
        ),
    )
    result = evaluate_commercial_cost(
        model,
        policy(),
        engine_version="commercial-primary-v1",
        validator_version="commercial-independent-v1",
    )
    assert result.primary.total == Decimal("16.20")
    assert result.independent.independently_calculated_total == Decimal("16.20")
    assert result.independent.passed


def test_commercial_cost_fails_closed_on_missing_scope_or_funding_rate() -> None:
    with pytest.raises(ValueError, match="component-count limit"):
        evaluate_commercial_cost(
            logistics_model(),
            policy().model_copy(update={"max_model_components": 1}),
            engine_version="commercial-primary-v1",
            validator_version="commercial-independent-v1",
        )

    incomplete_policy = policy().model_copy(
        update={"required_logistics_sections": frozenset({"TRANSPORT", "STORAGE"})}
    )
    transport_only = logistics_model().model_copy(
        update={
            "logistics": LogisticsPlan(
                transport_legs=logistics_model().logistics.transport_legs  # type: ignore[union-attr]
            )
        }
    )
    incomplete = evaluate_commercial_cost(
        transport_only,
        incomplete_policy,
        engine_version="commercial-primary-v1",
        validator_version="commercial-independent-v1",
    )
    assert not incomplete.independent.passed
    assert "LOGISTICS:STORAGE" in incomplete.independent.findings[0].entity_ids

    finance = CommercialCostModelInput(
        model_kind=CommercialCostModelKind.CONTRACT_FINANCE,
        currency="RUB",
        target_line_id="line-finance",
        target_semantic_key="contract-finance",
        contract_finance=ContractFinancePlan(
            valuation_start=date(2026, 1, 1),
            valuation_end=date(2026, 1, 2),
            cash_flows=(
                ContractCashFlow(
                    cash_flow_id="outflow",
                    kind=ContractCashFlowKind.DIRECT_COST,
                    cash_date=date(2026, 1, 1),
                    amount=Decimal("-1"),
                    observation_ids=("obs-outflow",),
                ),
            ),
        ),
    )
    with pytest.raises(ValueError, match="funding-rate period"):
        evaluate_commercial_cost(
            finance,
            policy(),
            engine_version="commercial-primary-v1",
            validator_version="commercial-independent-v1",
        )
    with pytest.raises(ValueError, match="finance horizon"):
        evaluate_commercial_cost(
            finance.model_copy(
                update={
                    "contract_finance": finance.contract_finance.model_copy(
                        update={"valuation_end": date(2026, 1, 3)}
                    )
                }
            ),
            policy().model_copy(update={"max_finance_horizon_days": 1}),
            engine_version="commercial-primary-v1",
            validator_version="commercial-independent-v1",
        )
