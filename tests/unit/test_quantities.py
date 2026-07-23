from decimal import Decimal

from tenderguard.domain.enums import (
    QuantityOperation,
    VerificationStatus,
)
from tenderguard.domain.models import QuantityRecord
from tenderguard.domain.quantities import (
    QuantityBalance,
    QuantityFormulaDefinition,
    QuantityValidationPolicy,
    validate_balance,
    validate_quantity,
)


def quantity(value: str) -> QuantityRecord:
    return QuantityRecord(
        quantity_id="quantity-1",
        boq_line_id="line-1",
        value=Decimal(value),
        unit="m3",
        source_observation_ids=("obs-1",),
        source_priority=1,
        formula="length * width * height",
        formula_inputs={
            "length": Decimal("10"),
            "width": Decimal("2"),
            "height": Decimal("1.5"),
        },
        rounding_mode="ROUND_HALF_UP",
        rounding_scale=2,
        waste_factor=Decimal("0"),
        status=VerificationStatus.IN_REVIEW,
    )


def policy() -> QuantityValidationPolicy:
    return QuantityValidationPolicy(
        policy_version="quantity-policy-v1",
        absolute_tolerance=Decimal("0.01"),
        relative_tolerance=Decimal("0.001"),
        allow_zero=False,
        allow_negative=False,
    )


def test_quantity_formula_mismatch_is_blocking() -> None:
    formula = QuantityFormulaDefinition(
        formula_id="formula-1",
        formula_version="1",
        operation=QuantityOperation.RECTANGULAR_VOLUME,
        inputs={
            "length": Decimal("10"),
            "width": Decimal("2"),
            "height": Decimal("1.5"),
        },
        output_unit="m3",
        display_formula="10 * 2 * 1.5",
    )
    result = validate_quantity(quantity("29"), formula=formula, policy=policy())
    assert not result.passed
    assert result.recalculated_value == Decimal("30.00")
    assert any(item.code.value == "QUANTITY_FORMULA_MISMATCH" for item in result.findings)


def test_quantity_balance_uses_methodology_tolerance() -> None:
    findings = validate_balance(
        QuantityBalance(
            balance_id="balance-1",
            parent_quantity_id="parent",
            parent_value=Decimal("100"),
            child_values={"child-1": Decimal("40"), "child-2": Decimal("58")},
            unit="m",
        ),
        policy=policy(),
    )
    assert findings
    assert findings[0].code.value == "QUANTITY_BALANCE_MISMATCH"


def test_missing_quantity_thresholds_fail_closed() -> None:
    incomplete_policy = QuantityValidationPolicy(
        policy_version="quantity-policy-draft",
        allow_zero=False,
        allow_negative=False,
    )
    result = validate_quantity(quantity("30"), formula=None, policy=incomplete_policy)
    assert not result.passed
    assert result.findings[0].code.value == "QUANTITY_THRESHOLD_UNCONFIGURED"


def test_quantity_recalculation_includes_explicit_waste_factor() -> None:
    formula = QuantityFormulaDefinition(
        formula_id="formula-waste",
        formula_version="quantity-formulas-v1",
        operation=QuantityOperation.PRODUCT,
        inputs={"length": Decimal("10")},
        output_unit="m3",
        display_formula="length",
    )
    with_waste = quantity("10.50").model_copy(update={"waste_factor": Decimal("0.05")})
    result = validate_quantity(with_waste, formula=formula, policy=policy())
    assert result.passed
    assert result.recalculated_value == Decimal("10.50")
