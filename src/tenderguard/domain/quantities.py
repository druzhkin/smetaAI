from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from pydantic import Field, model_validator

from tenderguard.domain.enums import FindingCode, QuantityOperation, Severity
from tenderguard.domain.models import DomainModel, QuantityRecord, ValidationFinding


class QuantityFormulaDefinition(DomainModel):
    formula_id: str
    formula_version: str
    operation: QuantityOperation
    inputs: dict[str, Decimal]
    output_unit: str
    display_formula: str

    @model_validator(mode="after")
    def required_inputs_exist(self) -> QuantityFormulaDefinition:
        required: dict[QuantityOperation, frozenset[str]] = {
            QuantityOperation.SUM: frozenset(),
            QuantityOperation.PRODUCT: frozenset(),
            QuantityOperation.RECTANGULAR_VOLUME: frozenset({"length", "width", "height"}),
            QuantityOperation.CYLINDER_VOLUME: frozenset({"diameter", "length", "pi"}),
        }
        missing = required[self.operation] - self.inputs.keys()
        if missing:
            raise ValueError(f"Formula inputs are missing: {sorted(missing)}")
        if not self.inputs:
            raise ValueError("Quantity formula requires at least one input")
        return self


class QuantityValidationPolicy(DomainModel):
    policy_version: str
    absolute_tolerance: Decimal | None = Field(default=None, ge=0)
    relative_tolerance: Decimal | None = Field(default=None, ge=0)
    allow_zero: bool
    allow_negative: bool
    historical_min: Decimal | None = None
    historical_max: Decimal | None = None
    historical_benchmark_version_id: str | None = None

    @model_validator(mode="after")
    def historical_bounds_are_versioned(self) -> QuantityValidationPolicy:
        if (self.historical_min is not None or self.historical_max is not None) and not (
            self.historical_benchmark_version_id
        ):
            raise ValueError("Historical bounds require a versioned benchmark")
        if (
            self.historical_min is not None
            and self.historical_max is not None
            and self.historical_min > self.historical_max
        ):
            raise ValueError("Historical minimum exceeds maximum")
        return self


class QuantityValidationResult(DomainModel):
    quantity_id: str
    recalculated_value: Decimal | None
    passed: bool
    findings: tuple[ValidationFinding, ...]


class QuantityBalance(DomainModel):
    balance_id: str
    parent_quantity_id: str
    parent_value: Decimal
    child_values: dict[str, Decimal]
    unit: str


def recalculate_formula(formula: QuantityFormulaDefinition) -> Decimal:
    values = tuple(formula.inputs.values())
    if formula.operation is QuantityOperation.SUM:
        return sum(values, start=Decimal("0"))
    if formula.operation is QuantityOperation.PRODUCT:
        result = Decimal("1")
        for value in values:
            result *= value
        return result
    if formula.operation is QuantityOperation.RECTANGULAR_VOLUME:
        return formula.inputs["length"] * formula.inputs["width"] * formula.inputs["height"]
    if formula.operation is QuantityOperation.CYLINDER_VOLUME:
        radius = formula.inputs["diameter"] / Decimal("2")
        return formula.inputs["pi"] * radius * radius * formula.inputs["length"]
    raise ValueError(f"Unsupported quantity operation: {formula.operation}")


def _within_tolerance(
    actual: Decimal,
    expected: Decimal,
    policy: QuantityValidationPolicy,
) -> bool | None:
    if policy.absolute_tolerance is None or policy.relative_tolerance is None:
        return None
    difference = abs(actual - expected)
    relative_limit = abs(expected) * policy.relative_tolerance
    return difference <= max(policy.absolute_tolerance, relative_limit)


def validate_quantity(
    quantity: QuantityRecord,
    *,
    formula: QuantityFormulaDefinition | None,
    policy: QuantityValidationPolicy,
) -> QuantityValidationResult:
    findings: list[ValidationFinding] = []
    recalculated: Decimal | None = None
    if policy.absolute_tolerance is None or policy.relative_tolerance is None:
        findings.append(
            ValidationFinding(
                code=FindingCode.QUANTITY_THRESHOLD_UNCONFIGURED,
                severity=Severity.BLOCKER,
                message="Quantity tolerances are not configured by methodology",
                entity_ids=(quantity.quantity_id,),
            )
        )
    if quantity.value < 0 and not policy.allow_negative:
        findings.append(
            ValidationFinding(
                code=FindingCode.QUANTITY_SIGN_INVALID,
                severity=Severity.BLOCKER,
                message="Negative quantity is not allowed by the approved policy",
                entity_ids=(quantity.quantity_id,),
            )
        )
    if quantity.value == 0 and not policy.allow_zero:
        findings.append(
            ValidationFinding(
                code=FindingCode.QUANTITY_SIGN_INVALID,
                severity=Severity.BLOCKER,
                message="Zero quantity is not allowed by the approved policy",
                entity_ids=(quantity.quantity_id,),
            )
        )
    if formula is not None:
        if formula.output_unit != quantity.unit:
            findings.append(
                ValidationFinding(
                    code=FindingCode.QUANTITY_UNIT_MISMATCH,
                    severity=Severity.BLOCKER,
                    message="Quantity formula output unit differs from the BoQ quantity unit",
                    entity_ids=(quantity.quantity_id, formula.formula_id),
                )
            )
        recalculated = (
            recalculate_formula(formula) * (Decimal("1") + quantity.waste_factor)
        ).quantize(
            Decimal(1).scaleb(-quantity.rounding_scale),
            rounding=ROUND_HALF_UP,
        )
        in_tolerance = _within_tolerance(quantity.value, recalculated, policy)
        if in_tolerance is False:
            findings.append(
                ValidationFinding(
                    code=FindingCode.QUANTITY_FORMULA_MISMATCH,
                    severity=Severity.BLOCKER,
                    message="Stored quantity differs from independent formula calculation",
                    entity_ids=(quantity.quantity_id, formula.formula_id),
                    details={
                        "stored": quantity.value,
                        "recalculated": recalculated,
                    },
                )
            )
    if policy.historical_min is not None and quantity.value < policy.historical_min:
        findings.append(
            ValidationFinding(
                code=FindingCode.QUANTITY_HISTORICAL_OUTLIER,
                severity=Severity.WARNING,
                message="Quantity is below the approved historical range",
                entity_ids=(quantity.quantity_id,),
                details={"minimum": policy.historical_min},
            )
        )
    if policy.historical_max is not None and quantity.value > policy.historical_max:
        findings.append(
            ValidationFinding(
                code=FindingCode.QUANTITY_HISTORICAL_OUTLIER,
                severity=Severity.WARNING,
                message="Quantity is above the approved historical range",
                entity_ids=(quantity.quantity_id,),
                details={"maximum": policy.historical_max},
            )
        )
    return QuantityValidationResult(
        quantity_id=quantity.quantity_id,
        recalculated_value=recalculated,
        passed=not any(item.severity is Severity.BLOCKER for item in findings),
        findings=tuple(findings),
    )


def validate_balance(
    balance: QuantityBalance,
    *,
    policy: QuantityValidationPolicy,
) -> tuple[ValidationFinding, ...]:
    child_total = sum(balance.child_values.values(), start=Decimal("0"))
    in_tolerance = _within_tolerance(balance.parent_value, child_total, policy)
    if in_tolerance is None:
        return (
            ValidationFinding(
                code=FindingCode.QUANTITY_THRESHOLD_UNCONFIGURED,
                severity=Severity.BLOCKER,
                message="Balance tolerances are not configured by methodology",
                entity_ids=(balance.balance_id,),
            ),
        )
    if not in_tolerance:
        return (
            ValidationFinding(
                code=FindingCode.QUANTITY_BALANCE_MISMATCH,
                severity=Severity.BLOCKER,
                message="Parent quantity does not equal the sum of child quantities",
                entity_ids=(balance.parent_quantity_id, *balance.child_values.keys()),
                details={
                    "parent": balance.parent_value,
                    "children_total": child_total,
                    "unit": balance.unit,
                },
            ),
        )
    return ()
