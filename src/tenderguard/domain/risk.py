from __future__ import annotations

from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal

from pydantic import Field, field_validator, model_validator

from tenderguard.domain.enums import ActorRole, FindingCode, Severity, VerificationStatus
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


class RiskPolicyBasis(DomainModel):
    method: str
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    correlation_model_version_id: str | None = None
    rounding_scale: int = Field(ge=0, le=12)
    rounding_mode: str

    @model_validator(mode="after")
    def method_and_rounding_are_supported(self) -> RiskPolicyBasis:
        if self.method != "THREE_POINT_EXPECTED_VALUE":
            raise ValueError("Risk policy method is not implemented")
        if self.rounding_mode not in {"ROUND_HALF_UP", "ROUND_HALF_EVEN"}:
            raise ValueError("Risk policy rounding mode is unsupported")
        return self


class RiskPolicy(RiskPolicyBasis):
    policy_version: str


class RiskReserveComponentReference(DomainModel):
    line_id: str = Field(min_length=1, max_length=64)
    semantic_key: str = Field(min_length=1, max_length=200)


class RiskModelDefinition(DomainModel):
    policy: RiskPolicyBasis
    risk_keys: tuple[str, ...] = Field(min_length=1, max_length=200)
    required_risk_keys: tuple[str, ...] = Field(min_length=1, max_length=200)
    independently_verified_risk_keys: tuple[str, ...] = Field(default=(), max_length=200)
    evidence_field_names: dict[str, str] = Field(max_length=200)
    review_role: ActorRole
    minimum_risk_items: int = Field(ge=1, le=200)
    reserve_unit: str = Field(min_length=1, max_length=64)
    reserve_cost_component: RiskReserveComponentReference

    @field_validator(
        "risk_keys",
        "required_risk_keys",
        "independently_verified_risk_keys",
    )
    @classmethod
    def risk_keys_are_normalized_and_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("Risk keys must be unique")
        if any(
            not value
            or value != value.strip()
            or len(value) > 128
            or any(character.isspace() for character in value)
            for value in values
        ):
            raise ValueError("Risk key is invalid")
        return values

    @model_validator(mode="after")
    def requirements_are_closed_and_reviewed(self) -> RiskModelDefinition:
        declared = set(self.risk_keys)
        required = set(self.required_risk_keys)
        independent = set(self.independently_verified_risk_keys)
        if not required.issubset(declared):
            raise ValueError("Required risk keys must be declared")
        if not independent.issubset(required):
            raise ValueError("Independent risk keys must be required")
        if self.minimum_risk_items < len(required):
            raise ValueError("Risk minimum cannot be below the required risk-key count")
        if self.minimum_risk_items > len(declared):
            raise ValueError("Risk minimum cannot exceed the declared risk-key count")
        if set(self.evidence_field_names) != declared:
            raise ValueError("Every declared risk key needs exactly one evidence field")
        fields = tuple(self.evidence_field_names.values())
        if len(fields) != len(set(fields)) or any(
            not value or value != value.strip() or len(value) > 200 for value in fields
        ):
            raise ValueError("Risk evidence field names must be normalized and unique")
        if self.review_role not in {
            ActorRole.REVIEWER,
            ActorRole.TECHNICAL_EXPERT,
        }:
            raise ValueError("Risk review role must be REVIEWER or TECHNICAL_EXPERT")
        return self

    def calculation_policy(self, version_id: str) -> RiskPolicy:
        return RiskPolicy(
            **self.policy.model_dump(),
            policy_version=version_id,
        )


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
    if any(item.correlated for item in items):
        findings.append(
            ValidationFinding(
                code=FindingCode.RISK_MODEL_INCOMPLETE,
                severity=Severity.BLOCKER,
                message=(
                    "Correlated risks require a separately qualified correlation engine; "
                    "a version identifier alone is not executable evidence"
                ),
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
        if (
            item.currency != policy.currency
            or item.status is not VerificationStatus.VERIFIED
            or item.correlated
        ):
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
