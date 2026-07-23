from __future__ import annotations

from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal

from pydantic import Field, model_validator

from tenderguard.domain.enums import FindingCode, Severity, VerificationStatus
from tenderguard.domain.models import DomainModel, ValidationFinding


class RiskItem(DomainModel):
    risk_id: str
    description: str
    probability: Decimal = Field(ge=0, le=1)
    impact_min: Decimal = Field(ge=0)
    impact_most_likely: Decimal = Field(ge=0)
    impact_max: Decimal = Field(ge=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    observation_ids: tuple[str, ...] = Field(min_length=1)
    status: VerificationStatus
    correlated: bool = False
    correlation_group: str | None = None
    mitigation_cost_input_id: str | None = None

    @model_validator(mode="after")
    def impacts_are_ordered(self) -> RiskItem:
        if not self.impact_min <= self.impact_most_likely <= self.impact_max:
            raise ValueError("Risk impacts must be ordered min <= likely <= max")
        if self.correlated and not self.correlation_group:
            raise ValueError("Correlated risk requires a correlation group")
        return self


class RiskPolicy(DomainModel):
    policy_version: str
    method: str
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    correlation_model_version_id: str | None = None
    rounding_scale: int = Field(ge=0, le=12)
    rounding_mode: str

    @model_validator(mode="after")
    def rounding_is_supported(self) -> RiskPolicy:
        if self.rounding_mode not in {"ROUND_HALF_UP", "ROUND_HALF_EVEN"}:
            raise ValueError("Risk policy rounding mode is unsupported")
        return self


class RiskCalculation(DomainModel):
    policy_version: str
    expected_reserve: Decimal
    currency: str
    per_risk_expected_impact: dict[str, Decimal]
    findings: tuple[ValidationFinding, ...]
    passed: bool


def calculate_risk_reserve(
    items: tuple[RiskItem, ...],
    policy: RiskPolicy,
) -> RiskCalculation:
    findings: list[ValidationFinding] = []
    if policy.method != "THREE_POINT_EXPECTED_VALUE":
        findings.append(
            ValidationFinding(
                code=FindingCode.RISK_MODEL_INCOMPLETE,
                severity=Severity.BLOCKER,
                message="Risk method is not implemented by the qualified deterministic engine",
            )
        )
    if any(item.correlated for item in items) and not policy.correlation_model_version_id:
        findings.append(
            ValidationFinding(
                code=FindingCode.RISK_MODEL_INCOMPLETE,
                severity=Severity.BLOCKER,
                message="Correlated risks require a versioned correlation model",
                entity_ids=tuple(item.risk_id for item in items if item.correlated),
            )
        )
    expected: dict[str, Decimal] = {}
    rounding = {
        "ROUND_HALF_UP": ROUND_HALF_UP,
        "ROUND_HALF_EVEN": ROUND_HALF_EVEN,
    }[policy.rounding_mode]
    quantum = Decimal(1).scaleb(-policy.rounding_scale)
    for item in items:
        if item.currency != policy.currency or item.status is not VerificationStatus.VERIFIED:
            findings.append(
                ValidationFinding(
                    code=FindingCode.RISK_MODEL_INCOMPLETE,
                    severity=Severity.BLOCKER,
                    message="Risk item is unverified or uses a different currency",
                    entity_ids=(item.risk_id,),
                )
            )
            continue
        triangular_mean = (item.impact_min + item.impact_most_likely + item.impact_max) / Decimal(
            "3"
        )
        expected[item.risk_id] = (item.probability * triangular_mean).quantize(
            quantum,
            rounding=rounding,
        )
    reserve = sum(expected.values(), start=Decimal("0")).quantize(
        quantum,
        rounding=rounding,
    )
    passed = not any(item.severity is Severity.BLOCKER for item in findings)
    return RiskCalculation(
        policy_version=policy.policy_version,
        expected_reserve=reserve,
        currency=policy.currency,
        per_risk_expected_impact=expected,
        findings=tuple(findings),
        passed=passed,
    )
