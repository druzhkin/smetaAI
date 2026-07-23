from __future__ import annotations

from decimal import Decimal

from tenderguard.domain.enums import FindingCode, Severity
from tenderguard.domain.models import ValidationFinding
from tenderguard.integrations.contracts import (
    NormativeCalculationRequest,
    NormativeCalculationResult,
)


def validate_normative_result(
    request: NormativeCalculationRequest,
    result: NormativeCalculationResult,
    *,
    rounding_tolerance: Decimal,
) -> tuple[ValidationFinding, ...]:
    findings: list[ValidationFinding] = []
    mismatches = {
        "normative_basis_version": (
            request.normative_basis_version,
            result.normative_basis_version,
        ),
        "calculation_method": (
            request.calculation_method,
            result.calculation_method,
        ),
        "region": (request.region, result.region),
        "price_period": (request.price_period, result.price_period),
        "work_or_resource_code": (
            request.work_or_resource_code,
            result.work_or_resource_code,
        ),
        "unit": (request.unit, result.unit),
    }
    for field_name, (expected, actual) in mismatches.items():
        if expected != actual:
            findings.append(
                ValidationFinding(
                    code=FindingCode.NORMATIVE_ENGINE_UNAVAILABLE,
                    severity=Severity.BLOCKER,
                    message=f"Normative result mismatch: {field_name}",
                    entity_ids=(request.request_id,),
                    details={"expected": expected, "actual": actual},
                )
            )
    if request.requested_coefficients != result.applied_coefficients:
        findings.append(
            ValidationFinding(
                code=FindingCode.NORMATIVE_ENGINE_UNAVAILABLE,
                severity=Severity.BLOCKER,
                message="Normative engine applied a different coefficient set",
                entity_ids=(request.request_id,),
            )
        )
    component_total = sum(
        (component.quantity * component.rate for component in result.resource_components),
        start=Decimal("0"),
    )
    if abs(component_total - result.total) > rounding_tolerance:
        findings.append(
            ValidationFinding(
                code=FindingCode.INDEPENDENT_RECALCULATION_MISMATCH,
                severity=Severity.BLOCKER,
                message="Normative resource components do not reproduce the engine total",
                entity_ids=(request.request_id,),
                details={
                    "component_total": component_total,
                    "engine_total": result.total,
                },
            )
        )
    resource_keys = [
        (component.resource_code, component.category) for component in result.resource_components
    ]
    duplicates = sorted({key for key in resource_keys if resource_keys.count(key) > 1})
    if duplicates:
        findings.append(
            ValidationFinding(
                code=FindingCode.INDEPENDENT_RECALCULATION_MISMATCH,
                severity=Severity.BLOCKER,
                message="Normative result contains duplicate resource components",
                entity_ids=tuple(f"{code}:{category}" for code, category in duplicates),
            )
        )
    return tuple(findings)
