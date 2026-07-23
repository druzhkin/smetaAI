from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import Field, model_validator

from tenderguard.domain.calculation import (
    AtomicCostInput,
    CalculationPolicy,
    calculate_primary,
    validate_independently,
)
from tenderguard.domain.models import (
    CalculationResult,
    DomainModel,
    IndependentValidationResult,
)


class ScenarioOverride(DomainModel):
    cost_input_id: str
    quantity: Decimal | None = Field(default=None, ge=0)
    unit_rate: Decimal | None = Field(default=None, ge=0)
    factor_values: dict[str, Decimal] = Field(default_factory=dict)
    evidence_or_assumption_id: str
    reason: str

    @model_validator(mode="after")
    def changes_something(self) -> ScenarioOverride:
        if self.quantity is None and self.unit_rate is None and not self.factor_values:
            raise ValueError("Scenario override does not change any input")
        if any(value <= 0 for value in self.factor_values.values()):
            raise ValueError("Scenario factors must be positive")
        return self


class ScenarioDefinition(DomainModel):
    scenario_id: str
    scenario_version: str
    name: str
    overrides: tuple[ScenarioOverride, ...]

    @model_validator(mode="after")
    def override_targets_are_unique(self) -> ScenarioDefinition:
        targets = [item.cost_input_id for item in self.overrides]
        if len(targets) != len(set(targets)):
            raise ValueError("Scenario contains duplicate cost-input overrides")
        return self


class ScenarioResult(DomainModel):
    scenario_id: str
    primary: CalculationResult
    independent: IndependentValidationResult


def calculate_scenario(
    base_inputs: tuple[AtomicCostInput, ...],
    scenario: ScenarioDefinition,
    policy: CalculationPolicy,
    *,
    calculated_at: datetime,
) -> ScenarioResult:
    overrides = {item.cost_input_id: item for item in scenario.overrides}
    unknown = overrides.keys() - {item.cost_input_id for item in base_inputs}
    if unknown:
        raise ValueError(f"Scenario references unknown cost inputs: {sorted(unknown)}")
    scenario_inputs: list[AtomicCostInput] = []
    for item in base_inputs:
        override = overrides.get(item.cost_input_id)
        if override is None:
            scenario_inputs.append(item)
            continue
        factors = tuple(
            factor.model_copy(
                update={
                    "value": override.factor_values.get(factor.factor_id, factor.value),
                    "evidence_or_rule_id": (
                        override.evidence_or_assumption_id
                        if factor.factor_id in override.factor_values
                        else factor.evidence_or_rule_id
                    ),
                }
            )
            for factor in item.factors
        )
        unknown_factors = override.factor_values.keys() - {
            factor.factor_id for factor in item.factors
        }
        if unknown_factors:
            raise ValueError(f"Scenario references unknown factors: {sorted(unknown_factors)}")
        scenario_inputs.append(
            item.model_copy(
                update={
                    "quantity": (
                        override.quantity if override.quantity is not None else item.quantity
                    ),
                    "unit_rate": (
                        override.unit_rate if override.unit_rate is not None else item.unit_rate
                    ),
                    "factors": factors,
                }
            )
        )
    inputs = tuple(scenario_inputs)
    primary = calculate_primary(
        inputs,
        policy,
        engine_version=f"primary:{scenario.scenario_version}",
        calculated_at=calculated_at,
    )
    independent = validate_independently(
        inputs,
        primary,
        policy,
        validator_version=f"independent:{scenario.scenario_version}",
        validated_at=calculated_at,
    )
    return ScenarioResult(
        scenario_id=scenario.scenario_id,
        primary=primary,
        independent=independent,
    )
