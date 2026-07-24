from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import (
    ROUND_DOWN,
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    ROUND_UP,
    Decimal,
)
from itertools import pairwise
from typing import Any, Literal

from pydantic import Field, model_validator

from tenderguard.domain.common import content_hash
from tenderguard.domain.enums import (
    CommercialCostModelKind,
    ContractCashFlowKind,
    ContractTermKind,
    FindingCode,
    GuaranteeKind,
    LogisticsAncillaryKind,
    LogisticsHandlingKind,
    MobilisationComponentKind,
    Severity,
)
from tenderguard.domain.models import DomainModel, ValidationFinding

_ROUNDING_MODES = {
    "ROUND_HALF_UP": ROUND_HALF_UP,
    "ROUND_HALF_EVEN": ROUND_HALF_EVEN,
    "ROUND_UP": ROUND_UP,
    "ROUND_DOWN": ROUND_DOWN,
}

LogisticsSection = Literal["TRANSPORT", "HANDLING", "STORAGE", "ANCILLARY"]


class TransportLeg(DomainModel):
    component_id: str = Field(min_length=1, max_length=128)
    mode: str = Field(min_length=1, max_length=100)
    origin: str = Field(min_length=1, max_length=500)
    destination: str = Field(min_length=1, max_length=500)
    distance_km: Decimal = Field(gt=0)
    charged_distance_factor: Decimal = Field(gt=0)
    cargo_mass_tonnes: Decimal | None = Field(default=None, gt=0)
    vehicle_mass_capacity_tonnes: Decimal | None = Field(default=None, gt=0)
    cargo_volume_m3: Decimal | None = Field(default=None, gt=0)
    vehicle_volume_capacity_m3: Decimal | None = Field(default=None, gt=0)
    cargo_units: Decimal | None = Field(default=None, gt=0)
    vehicle_unit_capacity: Decimal | None = Field(default=None, gt=0)
    fixed_cost_per_trip: Decimal = Field(ge=0)
    rate_per_vehicle_km: Decimal = Field(ge=0)
    toll_per_trip: Decimal = Field(ge=0)
    route_observation_ids: tuple[str, ...] = Field(min_length=1)
    cargo_observation_ids: tuple[str, ...] = Field(min_length=1)
    rate_observation_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def capacity_dimensions_are_complete(self) -> TransportLeg:
        dimensions = (
            (self.cargo_mass_tonnes, self.vehicle_mass_capacity_tonnes, "mass"),
            (self.cargo_volume_m3, self.vehicle_volume_capacity_m3, "volume"),
            (self.cargo_units, self.vehicle_unit_capacity, "units"),
        )
        supplied = 0
        for cargo, capacity, name in dimensions:
            if cargo is not None:
                supplied += 1
                if capacity is None:
                    raise ValueError(f"Transport {name} requires a vehicle capacity")
            elif capacity is not None:
                raise ValueError(f"Transport {name} capacity has no cargo quantity")
        if supplied == 0:
            raise ValueError("Transport leg requires at least one cargo/capacity dimension")
        if (
            self.fixed_cost_per_trip == 0
            and self.rate_per_vehicle_km == 0
            and self.toll_per_trip == 0
        ):
            raise ValueError("Transport leg has no priced cost element")
        return self

    @property
    def observation_ids(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                self.route_observation_ids + self.cargo_observation_ids + self.rate_observation_ids
            )
        )


class HandlingCost(DomainModel):
    component_id: str = Field(min_length=1, max_length=128)
    kind: LogisticsHandlingKind
    quantity: Decimal = Field(gt=0)
    unit: str = Field(min_length=1, max_length=64)
    operation_count: int = Field(gt=0)
    unit_rate: Decimal = Field(ge=0)
    observation_ids: tuple[str, ...] = Field(min_length=1)


class StorageCost(DomainModel):
    component_id: str = Field(min_length=1, max_length=128)
    quantity: Decimal = Field(gt=0)
    unit: str = Field(min_length=1, max_length=64)
    duration_days: Decimal = Field(gt=0)
    rate_per_unit_day: Decimal = Field(ge=0)
    observation_ids: tuple[str, ...] = Field(min_length=1)


class LogisticsAncillaryCost(DomainModel):
    component_id: str = Field(min_length=1, max_length=128)
    kind: LogisticsAncillaryKind
    quantity: Decimal = Field(gt=0)
    unit: str = Field(min_length=1, max_length=64)
    unit_rate: Decimal = Field(ge=0)
    observation_ids: tuple[str, ...] = Field(min_length=1)


class LogisticsPlan(DomainModel):
    transport_legs: tuple[TransportLeg, ...] = ()
    handling_costs: tuple[HandlingCost, ...] = ()
    storage_costs: tuple[StorageCost, ...] = ()
    ancillary_costs: tuple[LogisticsAncillaryCost, ...] = ()

    @model_validator(mode="after")
    def components_are_nonempty_and_unique(self) -> LogisticsPlan:
        components = (
            self.transport_legs + self.handling_costs + self.storage_costs + self.ancillary_costs
        )
        if not components:
            raise ValueError("Logistics plan requires at least one detailed component")
        identifiers = [item.component_id for item in components]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Logistics component identifiers must be unique")
        return self


class MobilisationCost(DomainModel):
    component_id: str = Field(min_length=1, max_length=128)
    kind: MobilisationComponentKind
    description: str = Field(min_length=1, max_length=1000)
    quantity: Decimal = Field(gt=0)
    unit: str = Field(min_length=1, max_length=64)
    occurrence_count: Decimal = Field(gt=0)
    duration_days: Decimal = Field(gt=0)
    unit_rate: Decimal = Field(ge=0)
    observation_ids: tuple[str, ...] = Field(min_length=1)


class MobilisationPlan(DomainModel):
    components: tuple[MobilisationCost, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def component_ids_are_unique(self) -> MobilisationPlan:
        identifiers = [item.component_id for item in self.components]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Mobilisation component identifiers must be unique")
        return self


class ContractCashFlow(DomainModel):
    cash_flow_id: str = Field(min_length=1, max_length=128)
    kind: ContractCashFlowKind
    cash_date: date
    amount: Decimal
    observation_ids: tuple[str, ...] = Field(min_length=1)
    contract_term_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def amount_is_not_zero(self) -> ContractCashFlow:
        if self.amount == 0:
            raise ValueError("Contract cash-flow amount cannot be zero")
        return self


class FundingRatePeriod(DomainModel):
    rate_period_id: str = Field(min_length=1, max_length=128)
    starts_on: date
    ends_on: date
    annual_rate: Decimal = Field(ge=0)
    observation_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def period_is_positive(self) -> FundingRatePeriod:
        if self.ends_on <= self.starts_on:
            raise ValueError("Funding rate period must have positive duration")
        return self


class GuaranteeFee(DomainModel):
    guarantee_id: str = Field(min_length=1, max_length=128)
    kind: GuaranteeKind
    notional_amount: Decimal = Field(gt=0)
    annual_rate: Decimal = Field(ge=0)
    starts_on: date
    ends_on: date
    observation_ids: tuple[str, ...] = Field(min_length=1)
    contract_term_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def period_is_positive(self) -> GuaranteeFee:
        if self.ends_on <= self.starts_on:
            raise ValueError("Guarantee fee period must have positive duration")
        return self


class ContractFinancePlan(DomainModel):
    valuation_start: date
    valuation_end: date
    cash_flows: tuple[ContractCashFlow, ...] = Field(min_length=1)
    funding_rate_periods: tuple[FundingRatePeriod, ...] = ()
    guarantee_fees: tuple[GuaranteeFee, ...] = ()

    @model_validator(mode="after")
    def timeline_is_valid(self) -> ContractFinancePlan:
        if self.valuation_end <= self.valuation_start:
            raise ValueError("Contract finance horizon must have positive duration")
        cash_flow_ids = [item.cash_flow_id for item in self.cash_flows]
        if len(cash_flow_ids) != len(set(cash_flow_ids)):
            raise ValueError("Contract cash-flow identifiers must be unique")
        period_ids = [item.rate_period_id for item in self.funding_rate_periods]
        if len(period_ids) != len(set(period_ids)):
            raise ValueError("Funding rate-period identifiers must be unique")
        guarantee_ids = [item.guarantee_id for item in self.guarantee_fees]
        if len(guarantee_ids) != len(set(guarantee_ids)):
            raise ValueError("Guarantee identifiers must be unique")
        if any(
            item.cash_date < self.valuation_start or item.cash_date > self.valuation_end
            for item in self.cash_flows
        ):
            raise ValueError("Contract cash flow lies outside the valuation horizon")
        if any(
            item.starts_on < self.valuation_start or item.ends_on > self.valuation_end
            for item in self.funding_rate_periods
        ):
            raise ValueError("Funding rate period lies outside the valuation horizon")
        if any(
            item.starts_on < self.valuation_start or item.ends_on > self.valuation_end
            for item in self.guarantee_fees
        ):
            raise ValueError("Guarantee period lies outside the valuation horizon")
        ordered_periods = sorted(
            self.funding_rate_periods,
            key=lambda item: (item.starts_on, item.ends_on, item.rate_period_id),
        )
        if any(left.ends_on > right.starts_on for left, right in pairwise(ordered_periods)):
            raise ValueError("Funding rate periods overlap")
        return self


class CommercialCostModelInput(DomainModel):
    model_kind: CommercialCostModelKind
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    target_line_id: str = Field(min_length=1, max_length=128)
    target_semantic_key: str = Field(min_length=1, max_length=200)
    related_contract_term_ids: tuple[str, ...] = ()
    logistics: LogisticsPlan | None = None
    mobilisation: MobilisationPlan | None = None
    contract_finance: ContractFinancePlan | None = None

    @model_validator(mode="after")
    def exactly_one_matching_payload_exists(self) -> CommercialCostModelInput:
        payloads = {
            CommercialCostModelKind.LOGISTICS: self.logistics,
            CommercialCostModelKind.MOBILISATION: self.mobilisation,
            CommercialCostModelKind.CONTRACT_FINANCE: self.contract_finance,
        }
        if payloads[self.model_kind] is None:
            raise ValueError("Commercial cost model payload does not match its kind")
        if sum(value is not None for value in payloads.values()) != 1:
            raise ValueError("Commercial cost model must contain exactly one typed payload")
        if len(self.related_contract_term_ids) != len(set(self.related_contract_term_ids)):
            raise ValueError("Related contract term identifiers must be unique")
        return self

    @property
    def observation_ids(self) -> tuple[str, ...]:
        identifiers: list[str] = []
        if self.logistics is not None:
            for transport_leg in self.logistics.transport_legs:
                identifiers.extend(transport_leg.observation_ids)
            for logistics_cost in (
                self.logistics.handling_costs
                + self.logistics.storage_costs
                + self.logistics.ancillary_costs
            ):
                identifiers.extend(logistics_cost.observation_ids)
        if self.mobilisation is not None:
            for mobilisation_cost in self.mobilisation.components:
                identifiers.extend(mobilisation_cost.observation_ids)
        if self.contract_finance is not None:
            for cash_flow in self.contract_finance.cash_flows:
                identifiers.extend(cash_flow.observation_ids)
            for rate_period in self.contract_finance.funding_rate_periods:
                identifiers.extend(rate_period.observation_ids)
            for guarantee_fee in self.contract_finance.guarantee_fees:
                identifiers.extend(guarantee_fee.observation_ids)
        return tuple(dict.fromkeys(identifiers))


class CommercialCostPolicy(DomainModel):
    policy_version: str
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    line_rounding_scale: int = Field(ge=0, le=8)
    total_rounding_scale: int = Field(ge=0, le=8)
    rounding_mode: str
    independent_tolerance: Decimal = Field(ge=0)
    day_count_basis: Literal[360, 365, 366]
    max_finance_horizon_days: int = Field(gt=0)
    max_model_components: int = Field(gt=0)
    allow_zero_total: bool
    required_model_kinds: frozenset[CommercialCostModelKind]
    required_logistics_sections: frozenset[LogisticsSection]
    required_mobilisation_kinds: frozenset[MobilisationComponentKind]
    required_cash_flow_kinds: frozenset[ContractCashFlowKind]
    required_contract_term_kinds: frozenset[ContractTermKind]

    @model_validator(mode="after")
    def rounding_mode_is_supported(self) -> CommercialCostPolicy:
        if self.rounding_mode not in _ROUNDING_MODES:
            raise ValueError(f"Unsupported commercial cost rounding mode: {self.rounding_mode}")
        return self


class CommercialCostLineResult(DomainModel):
    component_id: str
    component_type: str
    amount: Decimal
    currency: str
    details: dict[str, Any] = Field(default_factory=dict)


class CommercialCostCalculation(DomainModel):
    engine_version: str
    model_kind: CommercialCostModelKind
    currency: str
    lines: tuple[CommercialCostLineResult, ...]
    total: Decimal


class CommercialCostValidation(DomainModel):
    validator_version: str
    passed: bool
    independently_calculated_total: Decimal
    primary_total: Decimal
    difference: Decimal
    tolerance: Decimal
    findings: tuple[ValidationFinding, ...]


class CommercialCostEvaluation(DomainModel):
    input_hash: str
    primary: CommercialCostCalculation
    independent: CommercialCostValidation


def evaluate_commercial_cost(
    model: CommercialCostModelInput,
    policy: CommercialCostPolicy,
    *,
    engine_version: str,
    validator_version: str,
) -> CommercialCostEvaluation:
    if model.currency != policy.currency:
        raise ValueError("Commercial cost model currency differs from controlled policy")
    component_count = _component_count(model)
    if component_count > policy.max_model_components:
        raise ValueError("Commercial cost model exceeds the controlled component-count limit")
    if (
        model.contract_finance is not None
        and (model.contract_finance.valuation_end - model.contract_finance.valuation_start).days
        > policy.max_finance_horizon_days
    ):
        raise ValueError("Contract finance horizon exceeds the controlled operational limit")
    primary = _calculate_primary(model, policy, engine_version=engine_version)
    independent_total = _calculate_independently(model, policy)
    difference = abs(primary.total - independent_total)
    findings = list(_completeness_findings(model, policy))
    if primary.total == 0 and not policy.allow_zero_total:
        findings.append(
            ValidationFinding(
                code=FindingCode.COST_WITHOUT_BASIS,
                severity=Severity.BLOCKER,
                message="Controlled commercial model policy forbids a zero total",
                entity_ids=(model.target_line_id, model.target_semantic_key),
            )
        )
    if difference > policy.independent_tolerance:
        findings.append(
            ValidationFinding(
                code=FindingCode.INDEPENDENT_RECALCULATION_MISMATCH,
                severity=Severity.BLOCKER,
                message="Independent commercial cost recalculation differs from primary",
                details={
                    "primary_total": primary.total,
                    "independent_total": independent_total,
                    "difference": difference,
                    "tolerance": policy.independent_tolerance,
                },
            )
        )
    independent = CommercialCostValidation(
        validator_version=validator_version,
        passed=not any(item.severity is Severity.BLOCKER for item in findings),
        independently_calculated_total=independent_total,
        primary_total=primary.total,
        difference=difference,
        tolerance=policy.independent_tolerance,
        findings=tuple(findings),
    )
    return CommercialCostEvaluation(
        input_hash=content_hash({"model": model, "policy": policy}),
        primary=primary,
        independent=independent,
    )


def _calculate_primary(
    model: CommercialCostModelInput,
    policy: CommercialCostPolicy,
    *,
    engine_version: str,
) -> CommercialCostCalculation:
    lines: list[CommercialCostLineResult] = []
    if model.logistics is not None:
        lines.extend(_primary_logistics(model.logistics, policy))
    elif model.mobilisation is not None:
        lines.extend(_primary_mobilisation(model.mobilisation, policy))
    elif model.contract_finance is not None:
        lines.extend(_primary_contract_finance(model.contract_finance, policy))
    else:  # pragma: no cover - guarded by model validation
        raise ValueError("Commercial cost model has no typed payload")
    total = _round_total(
        sum((item.amount for item in lines), start=Decimal("0")),
        policy,
    )
    return CommercialCostCalculation(
        engine_version=engine_version,
        model_kind=model.model_kind,
        currency=model.currency,
        lines=tuple(lines),
        total=total,
    )


def _primary_logistics(
    plan: LogisticsPlan,
    policy: CommercialCostPolicy,
) -> list[CommercialCostLineResult]:
    lines: list[CommercialCostLineResult] = []
    for leg in sorted(plan.transport_legs, key=lambda item: item.component_id):
        trips = _trip_count(leg)
        amount = Decimal(trips) * (
            leg.fixed_cost_per_trip
            + leg.distance_km * leg.charged_distance_factor * leg.rate_per_vehicle_km
            + leg.toll_per_trip
        )
        lines.append(
            _line(
                leg.component_id,
                "TRANSPORT",
                amount,
                policy,
                {
                    "trip_count": trips,
                    "distance_km": leg.distance_km,
                    "charged_distance_factor": leg.charged_distance_factor,
                    "mode": leg.mode,
                    "origin": leg.origin,
                    "destination": leg.destination,
                },
            )
        )
    for handling in sorted(plan.handling_costs, key=lambda value: value.component_id):
        lines.append(
            _line(
                handling.component_id,
                f"HANDLING:{handling.kind.value}",
                handling.quantity * Decimal(handling.operation_count) * handling.unit_rate,
                policy,
                {
                    "quantity": handling.quantity,
                    "operation_count": handling.operation_count,
                },
            )
        )
    for storage in sorted(plan.storage_costs, key=lambda value: value.component_id):
        lines.append(
            _line(
                storage.component_id,
                "STORAGE",
                storage.quantity * storage.duration_days * storage.rate_per_unit_day,
                policy,
                {
                    "quantity": storage.quantity,
                    "duration_days": storage.duration_days,
                },
            )
        )
    for ancillary in sorted(plan.ancillary_costs, key=lambda value: value.component_id):
        lines.append(
            _line(
                ancillary.component_id,
                f"ANCILLARY:{ancillary.kind.value}",
                ancillary.quantity * ancillary.unit_rate,
                policy,
                {"quantity": ancillary.quantity},
            )
        )
    return lines


def _primary_mobilisation(
    plan: MobilisationPlan,
    policy: CommercialCostPolicy,
) -> list[CommercialCostLineResult]:
    return [
        _line(
            item.component_id,
            item.kind.value,
            item.quantity * item.occurrence_count * item.duration_days * item.unit_rate,
            policy,
            {
                "quantity": item.quantity,
                "occurrence_count": item.occurrence_count,
                "duration_days": item.duration_days,
            },
        )
        for item in sorted(plan.components, key=lambda value: value.component_id)
    ]


def _primary_contract_finance(
    plan: ContractFinancePlan,
    policy: CommercialCostPolicy,
) -> list[CommercialCostLineResult]:
    raw_interest = Decimal("0")
    events: dict[date, Decimal] = defaultdict(Decimal)
    for item in plan.cash_flows:
        events[item.cash_date] += item.amount
    boundaries = {
        plan.valuation_start,
        plan.valuation_end,
        *events.keys(),
        *(item.starts_on for item in plan.funding_rate_periods),
        *(item.ends_on for item in plan.funding_rate_periods),
    }
    ordered = sorted(boundaries)
    balance = Decimal("0")
    for start, end in pairwise(ordered):
        balance += events.get(start, Decimal("0"))
        days = (end - start).days
        if days <= 0 or balance >= 0:
            continue
        rate = _funding_rate_for_interval(plan, start, end)
        raw_interest += -balance * rate * Decimal(days) / Decimal(policy.day_count_basis)
    lines: list[CommercialCostLineResult] = []
    if raw_interest != 0:
        lines.append(
            _line(
                "working-capital-interest",
                "WORKING_CAPITAL_INTEREST",
                raw_interest,
                policy,
                {
                    "valuation_start": plan.valuation_start,
                    "valuation_end": plan.valuation_end,
                    "day_count_basis": policy.day_count_basis,
                },
            )
        )
    for fee in sorted(plan.guarantee_fees, key=lambda item: item.guarantee_id):
        duration_days = Decimal((fee.ends_on - fee.starts_on).days)
        amount = (
            fee.notional_amount * fee.annual_rate * duration_days / Decimal(policy.day_count_basis)
        )
        lines.append(
            _line(
                fee.guarantee_id,
                f"GUARANTEE:{fee.kind.value}",
                amount,
                policy,
                {
                    "notional_amount": fee.notional_amount,
                    "annual_rate": fee.annual_rate,
                    "duration_days": duration_days,
                    "day_count_basis": policy.day_count_basis,
                },
            )
        )
    return lines


def _calculate_independently(
    model: CommercialCostModelInput,
    policy: CommercialCostPolicy,
) -> Decimal:
    amounts: list[Decimal] = []
    if model.logistics is not None:
        for leg in model.logistics.transport_legs:
            trips = _independent_trip_count(leg)
            raw = Decimal(trips) * leg.fixed_cost_per_trip
            raw += (
                Decimal(trips)
                * leg.distance_km
                * leg.charged_distance_factor
                * leg.rate_per_vehicle_km
            )
            raw += Decimal(trips) * leg.toll_per_trip
            amounts.append(_round_line(raw, policy))
        amounts.extend(
            _round_line(item.unit_rate * item.quantity * item.operation_count, policy)
            for item in model.logistics.handling_costs
        )
        amounts.extend(
            _round_line(
                item.rate_per_unit_day * item.duration_days * item.quantity,
                policy,
            )
            for item in model.logistics.storage_costs
        )
        amounts.extend(
            _round_line(item.unit_rate * item.quantity, policy)
            for item in model.logistics.ancillary_costs
        )
    elif model.mobilisation is not None:
        amounts.extend(
            _round_line(
                item.unit_rate * item.duration_days * item.occurrence_count * item.quantity,
                policy,
            )
            for item in model.mobilisation.components
        )
    elif model.contract_finance is not None:
        plan = model.contract_finance
        events: dict[date, Decimal] = defaultdict(Decimal)
        for item in plan.cash_flows:
            events[item.cash_date] += item.amount
        balance = Decimal("0")
        daily_interest = Decimal("0")
        current = plan.valuation_start
        while current < plan.valuation_end:
            balance += events.get(current, Decimal("0"))
            if balance < 0:
                rate = _funding_rate_for_day(plan, current)
                daily_interest += -balance * rate / Decimal(policy.day_count_basis)
            current += timedelta(days=1)
        if daily_interest != 0:
            amounts.append(_round_line(daily_interest, policy))
        for fee in plan.guarantee_fees:
            daily_fee = Decimal("0")
            current = fee.starts_on
            while current < fee.ends_on:
                daily_fee += fee.notional_amount * fee.annual_rate / Decimal(policy.day_count_basis)
                current += timedelta(days=1)
            amounts.append(_round_line(daily_fee, policy))
    return _round_total(sum(amounts, start=Decimal("0")), policy)


def _completeness_findings(
    model: CommercialCostModelInput,
    policy: CommercialCostPolicy,
) -> tuple[ValidationFinding, ...]:
    missing: list[str] = []
    if model.logistics is not None:
        present_sections: set[LogisticsSection] = set()
        if model.logistics.transport_legs:
            present_sections.add("TRANSPORT")
        if model.logistics.handling_costs:
            present_sections.add("HANDLING")
        if model.logistics.storage_costs:
            present_sections.add("STORAGE")
        if model.logistics.ancillary_costs:
            present_sections.add("ANCILLARY")
        missing.extend(
            f"LOGISTICS:{item}"
            for item in sorted(policy.required_logistics_sections)
            if item not in present_sections
        )
    elif model.mobilisation is not None:
        mobilisation_present = {item.kind for item in model.mobilisation.components}
        missing.extend(
            f"MOBILISATION:{item.value}"
            for item in sorted(
                policy.required_mobilisation_kinds - mobilisation_present,
                key=lambda value: value.value,
            )
        )
    elif model.contract_finance is not None:
        cash_flow_present = {item.kind for item in model.contract_finance.cash_flows}
        missing.extend(
            f"CONTRACT_FINANCE:{item.value}"
            for item in sorted(
                policy.required_cash_flow_kinds - cash_flow_present,
                key=lambda value: value.value,
            )
        )
    if not missing:
        return ()
    return (
        ValidationFinding(
            code=FindingCode.COST_WITHOUT_BASIS,
            severity=Severity.BLOCKER,
            message="Controlled commercial cost model is incomplete",
            entity_ids=tuple(missing),
        ),
    )


def _trip_count(leg: TransportLeg) -> int:
    ratios = [
        cargo / capacity
        for cargo, capacity in (
            (leg.cargo_mass_tonnes, leg.vehicle_mass_capacity_tonnes),
            (leg.cargo_volume_m3, leg.vehicle_volume_capacity_m3),
            (leg.cargo_units, leg.vehicle_unit_capacity),
        )
        if cargo is not None and capacity is not None
    ]
    return max(int(value.to_integral_value(rounding=ROUND_UP)) for value in ratios)


def _component_count(model: CommercialCostModelInput) -> int:
    if model.logistics is not None:
        return len(
            model.logistics.transport_legs
            + model.logistics.handling_costs
            + model.logistics.storage_costs
            + model.logistics.ancillary_costs
        )
    if model.mobilisation is not None:
        return len(model.mobilisation.components)
    if model.contract_finance is not None:
        return (
            len(model.contract_finance.cash_flows)
            + len(model.contract_finance.funding_rate_periods)
            + len(model.contract_finance.guarantee_fees)
        )
    return 0


def _independent_trip_count(leg: TransportLeg) -> int:
    trips = 1
    for cargo, capacity in (
        (leg.cargo_mass_tonnes, leg.vehicle_mass_capacity_tonnes),
        (leg.cargo_volume_m3, leg.vehicle_volume_capacity_m3),
        (leg.cargo_units, leg.vehicle_unit_capacity),
    ):
        if cargo is None or capacity is None:
            continue
        quotient = cargo / capacity
        whole = int(quotient)
        candidate = whole if quotient == Decimal(whole) else whole + 1
        trips = max(trips, candidate)
    return trips


def _funding_rate_for_interval(
    plan: ContractFinancePlan,
    start: date,
    end: date,
) -> Decimal:
    matches = [
        item
        for item in plan.funding_rate_periods
        if item.starts_on <= start and item.ends_on >= end
    ]
    if len(matches) != 1:
        raise ValueError("Negative cash balance is not covered by exactly one funding-rate period")
    return matches[0].annual_rate


def _funding_rate_for_day(plan: ContractFinancePlan, day: date) -> Decimal:
    matches = [item for item in plan.funding_rate_periods if item.starts_on <= day < item.ends_on]
    if len(matches) != 1:
        raise ValueError("Negative cash balance is not covered by exactly one funding-rate period")
    return matches[0].annual_rate


def _line(
    component_id: str,
    component_type: str,
    amount: Decimal,
    policy: CommercialCostPolicy,
    details: dict[str, Any],
) -> CommercialCostLineResult:
    return CommercialCostLineResult(
        component_id=component_id,
        component_type=component_type,
        amount=_round_line(amount, policy),
        currency=policy.currency,
        details=details,
    )


def _round_line(value: Decimal, policy: CommercialCostPolicy) -> Decimal:
    return value.quantize(
        Decimal(1).scaleb(-policy.line_rounding_scale),
        rounding=_ROUNDING_MODES[policy.rounding_mode],
    )


def _round_total(value: Decimal, policy: CommercialCostPolicy) -> Decimal:
    return value.quantize(
        Decimal(1).scaleb(-policy.total_rounding_scale),
        rounding=_ROUNDING_MODES[policy.rounding_mode],
    )
