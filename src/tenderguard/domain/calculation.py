from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import (
    ROUND_DOWN,
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    ROUND_UP,
    Decimal,
    InvalidOperation,
    localcontext,
)
from functools import reduce
from operator import mul
from typing import Literal

from pydantic import Field, model_validator

from tenderguard.domain.common import content_hash
from tenderguard.domain.enums import CostCategory, FindingCode, Severity
from tenderguard.domain.models import (
    CalculationLineResult,
    CalculationResult,
    CalculationSnapshot,
    ControlledVersion,
    DomainModel,
    IndependentValidationResult,
    ValidationFinding,
)

ROUNDING_MODES = {
    "ROUND_HALF_UP": ROUND_HALF_UP,
    "ROUND_HALF_EVEN": ROUND_HALF_EVEN,
    "ROUND_UP": ROUND_UP,
    "ROUND_DOWN": ROUND_DOWN,
}


class AppliedFactor(DomainModel):
    factor_id: str
    version_id: str
    value: Decimal = Field(gt=0, max_digits=38, decimal_places=12)
    evidence_or_rule_id: str


class AtomicCostInput(DomainModel):
    cost_input_id: str
    line_id: str
    wbs_node_id: str
    semantic_key: str
    category: CostCategory
    quantity: Decimal = Field(ge=0, max_digits=38, decimal_places=12)
    unit: str
    unit_rate: Decimal = Field(ge=0, max_digits=38, decimal_places=12)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    factors: tuple[AppliedFactor, ...] = ()
    sign: Literal[-1, 1] = 1
    source_observation_id: str | None = None
    approved_assumption_id: str | None = None
    normative_rate_id: str | None = None
    risk_reserve_id: str | None = None
    derived_cost_model_id: str | None = None

    @model_validator(mode="after")
    def basis_reference_is_unambiguous(self) -> AtomicCostInput:
        references = [
            self.source_observation_id,
            self.approved_assumption_id,
            self.normative_rate_id,
            self.risk_reserve_id,
            self.derived_cost_model_id,
        ]
        if sum(value is not None for value in references) > 1:
            raise ValueError("A cost input may have only one direct basis reference")
        return self

    @property
    def has_basis(self) -> bool:
        return any(
            value is not None
            for value in (
                self.source_observation_id,
                self.approved_assumption_id,
                self.normative_rate_id,
                self.risk_reserve_id,
                self.derived_cost_model_id,
            )
        )


class CalculationPolicy(DomainModel):
    policy_version: str
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    line_rounding_scale: int = Field(ge=0, le=8)
    total_rounding_scale: int = Field(ge=0, le=8)
    rounding_mode: str
    independent_tolerance: Decimal = Field(ge=0, max_digits=38, decimal_places=12)
    expected_semantic_keys: frozenset[str] = frozenset()

    @model_validator(mode="after")
    def rounding_mode_is_supported(self) -> CalculationPolicy:
        if self.rounding_mode not in ROUNDING_MODES:
            raise ValueError(f"Unsupported rounding mode: {self.rounding_mode}")
        return self


def _quantizer(scale: int) -> Decimal:
    return Decimal(1).scaleb(-scale)


_MAX_STORED_DECIMAL = Decimal("99999999999999999999999999.999999999999")


def _require_storable_decimal(value: Decimal, label: str) -> Decimal:
    if not value.is_finite() or abs(value) > _MAX_STORED_DECIMAL:
        raise ValueError(f"{label} exceeds the supported financial decimal range")
    return value


def calculate_primary(
    inputs: tuple[AtomicCostInput, ...],
    policy: CalculationPolicy,
    *,
    engine_version: str,
    calculated_at: datetime,
) -> CalculationResult:
    """Primary calculation path: calculate and round each atomic line."""

    line_quantum = _quantizer(policy.line_rounding_scale)
    total_quantum = _quantizer(policy.total_rounding_scale)
    rounding = ROUNDING_MODES[policy.rounding_mode]
    lines: list[CalculationLineResult] = []
    category_totals: dict[CostCategory, Decimal] = defaultdict(Decimal)

    try:
        with localcontext() as context:
            context.prec = 160
            for item in sorted(inputs, key=lambda value: value.cost_input_id):
                if item.currency != policy.currency:
                    raise ValueError(
                        f"Input {item.cost_input_id} is {item.currency}; expected {policy.currency}"
                    )
                amount = item.quantity * item.unit_rate
                for factor in item.factors:
                    amount *= factor.value
                amount = _require_storable_decimal(
                    (amount * item.sign).quantize(line_quantum, rounding=rounding),
                    f"Calculated line {item.cost_input_id}",
                )
                lines.append(
                    CalculationLineResult(
                        line_id=item.line_id,
                        category=item.category,
                        amount=amount,
                        currency=policy.currency,
                    )
                )
                category_totals[item.category] += amount

            rounded_categories = {
                category: _require_storable_decimal(
                    amount.quantize(total_quantum, rounding=rounding),
                    f"Calculated category {category.value}",
                )
                for category, amount in sorted(
                    category_totals.items(),
                    key=lambda item: item[0].value,
                )
            }
            grand_total = _require_storable_decimal(
                sum(rounded_categories.values(), start=Decimal("0")).quantize(
                    total_quantum,
                    rounding=rounding,
                ),
                "Calculated project total",
            )
    except InvalidOperation as error:
        raise ValueError("Calculation cannot be represented with the approved precision") from error
    return CalculationResult(
        engine_version=engine_version,
        currency=policy.currency,
        lines=tuple(lines),
        category_totals=rounded_categories,
        grand_total=grand_total,
        calculated_at=calculated_at,
    )


def validate_independently(
    inputs: tuple[AtomicCostInput, ...],
    primary: CalculationResult,
    policy: CalculationPolicy,
    *,
    validator_version: str,
    validated_at: datetime,
) -> IndependentValidationResult:
    """Independent path based only on atomic inputs, never primary line totals.

    This implementation intentionally uses a separate aggregation shape from
    the primary engine and additionally checks completeness and duplication.
    """

    findings: list[ValidationFinding] = []
    identifiers = [item.cost_input_id for item in inputs]
    duplicated_ids = sorted(
        {identifier for identifier in identifiers if identifiers.count(identifier) > 1}
    )
    if duplicated_ids:
        findings.append(
            ValidationFinding(
                code=FindingCode.INDEPENDENT_RECALCULATION_MISMATCH,
                severity=Severity.BLOCKER,
                message="Duplicate atomic cost input identifiers detected",
                entity_ids=tuple(duplicated_ids),
            )
        )

    semantic_keys: dict[tuple[str, str], list[str]] = defaultdict(list)
    for item in inputs:
        semantic_keys[(item.wbs_node_id, item.semantic_key)].append(item.cost_input_id)
    duplicate_semantics = tuple(
        input_id
        for _, input_ids in sorted(semantic_keys.items())
        if len(input_ids) > 1
        for input_id in input_ids
    )
    if duplicate_semantics:
        findings.append(
            ValidationFinding(
                code=FindingCode.INDEPENDENT_RECALCULATION_MISMATCH,
                severity=Severity.BLOCKER,
                message="Potential double counting: repeated WBS/semantic cost component",
                entity_ids=duplicate_semantics,
            )
        )

    present_keys = {item.semantic_key for item in inputs}
    missing_keys = sorted(policy.expected_semantic_keys - present_keys)
    if missing_keys:
        findings.append(
            ValidationFinding(
                code=FindingCode.INDEPENDENT_RECALCULATION_MISMATCH,
                severity=Severity.BLOCKER,
                message="Expected price components are missing",
                entity_ids=tuple(missing_keys),
            )
        )

    no_basis = tuple(item.cost_input_id for item in inputs if not item.has_basis)
    if no_basis:
        findings.append(
            ValidationFinding(
                code=FindingCode.COST_WITHOUT_BASIS,
                severity=Severity.BLOCKER,
                message="Atomic cost inputs without source, normative rate, or approved assumption",
                entity_ids=no_basis,
            )
        )

    wrong_currency = tuple(
        item.cost_input_id for item in inputs if item.currency != policy.currency
    )
    if wrong_currency:
        findings.append(
            ValidationFinding(
                code=FindingCode.INDEPENDENT_RECALCULATION_MISMATCH,
                severity=Severity.BLOCKER,
                message="Atomic inputs are not normalized to the calculation currency",
                entity_ids=wrong_currency,
            )
        )

    line_quantum = _quantizer(policy.line_rounding_scale)
    total_quantum = _quantizer(policy.total_rounding_scale)
    rounding = ROUNDING_MODES[policy.rounding_mode]

    independent_category_totals: dict[CostCategory, Decimal] = defaultdict(Decimal)
    try:
        with localcontext() as context:
            context.prec = 160
            for item in inputs:
                factors = [factor.value for factor in item.factors]
                multiplier = reduce(mul, factors, Decimal("1"))
                independently_rounded_line = _require_storable_decimal(
                    (item.unit_rate * item.quantity * multiplier * Decimal(item.sign)).quantize(
                        line_quantum, rounding=rounding
                    ),
                    f"Independently calculated line {item.cost_input_id}",
                )
                independent_category_totals[item.category] += independently_rounded_line

            independent_total = _require_storable_decimal(
                sum(
                    (
                        subtotal.quantize(total_quantum, rounding=rounding)
                        for subtotal in independent_category_totals.values()
                    ),
                    start=Decimal("0"),
                ).quantize(total_quantum, rounding=rounding),
                "Independently calculated project total",
            )
    except InvalidOperation as error:
        raise ValueError(
            "Independent calculation cannot be represented with the approved precision"
        ) from error

    difference = abs(independent_total - primary.grand_total)
    if difference > policy.independent_tolerance:
        findings.append(
            ValidationFinding(
                code=FindingCode.INDEPENDENT_RECALCULATION_MISMATCH,
                severity=Severity.BLOCKER,
                message="Independent recalculation differs from primary total",
                details={
                    "primary_total": primary.grand_total,
                    "independent_total": independent_total,
                    "difference": difference,
                    "tolerance": policy.independent_tolerance,
                },
            )
        )

    passed = not any(finding.severity is Severity.BLOCKER for finding in findings)
    return IndependentValidationResult(
        validator_version=validator_version,
        passed=passed,
        independently_calculated_total=independent_total,
        primary_total=primary.grand_total,
        difference=difference,
        tolerance=policy.independent_tolerance,
        findings=tuple(findings),
        validated_at=validated_at,
    )


def create_snapshot(
    *,
    project_id: str,
    document_set_revision_id: str,
    inputs: tuple[AtomicCostInput, ...],
    policy: CalculationPolicy,
    controlled_versions: tuple[ControlledVersion, ...],
    primary: CalculationResult,
    independent: IndependentValidationResult,
    created_by: str,
    created_at: datetime,
) -> CalculationSnapshot:
    canonical_inputs = tuple(sorted(inputs, key=lambda item: item.cost_input_id))
    canonical_versions = tuple(
        sorted(controlled_versions, key=lambda item: (item.kind, item.version_id))
    )
    input_record = {
        "atomic_inputs": canonical_inputs,
        "calculation_policy": policy,
        "controlled_versions": canonical_versions,
    }
    output_record = {
        "primary": primary,
        "independent": independent,
    }
    input_hash = content_hash(input_record)
    output_hash = content_hash(output_record)
    snapshot_record = {
        "project_id": project_id,
        "document_set_revision_id": document_set_revision_id,
        "input_hash": input_hash,
        "output_hash": output_hash,
        "created_by": created_by,
        "created_at": created_at,
    }
    snapshot_hash = content_hash(snapshot_record)
    return CalculationSnapshot(
        snapshot_id=f"snapshot-{snapshot_hash[:24]}",
        project_id=project_id,
        document_set_revision_id=document_set_revision_id,
        input_hash=input_hash,
        output_hash=output_hash,
        snapshot_hash=snapshot_hash,
        created_by=created_by,
        created_at=created_at,
        fixed=True,
    )
