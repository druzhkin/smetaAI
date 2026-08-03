from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tenderguard.domain.enums import (
    ApprovalDecision,
    ApprovalState,
    CostCategory,
    EvidenceMethod,
    FindingCode,
    MatchClass,
    PriceEvidenceClass,
    PriceSourceType,
    PriceStatus,
    Severity,
    VatBasis,
    VerificationStatus,
    VersionStatus,
)


class DomainModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ControlledVersion(DomainModel):
    kind: str = Field(min_length=1)
    version_id: str = Field(min_length=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: VersionStatus
    approved_by: str | None = None
    approved_at: datetime | None = None

    @model_validator(mode="after")
    def approval_is_complete(self) -> ControlledVersion:
        if self.status is VersionStatus.APPROVED and not (self.approved_by and self.approved_at):
            raise ValueError("Approved controlled versions require approver and timestamp")
        return self


class EvidenceLocation(DomainModel):
    document_id: str = Field(min_length=1, max_length=64)
    document_revision_id: str = Field(min_length=1, max_length=64)
    original_object_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    locator_kind: str = Field(min_length=1, max_length=100)
    locator: str = Field(min_length=1, max_length=4000)
    page: int | None = Field(default=None, ge=1)
    table: str | None = Field(default=None, max_length=500)
    sheet: str | None = Field(default=None, max_length=500)
    cell_or_range: str | None = Field(default=None, max_length=500)


class Observation(DomainModel):
    observation_id: str = Field(min_length=1, max_length=64)
    field_name: str = Field(min_length=1, max_length=300)
    value: Any
    unit: str | None = Field(default=None, max_length=100)
    method: EvidenceMethod
    method_version: str = Field(min_length=1, max_length=200)
    source_priority: int = Field(ge=0)
    location: EvidenceLocation
    observed_at: datetime
    actor_id: str = Field(min_length=1, max_length=128)
    confidence: Decimal | None = Field(default=None, ge=0, le=1)
    status: VerificationStatus = VerificationStatus.UNVERIFIED

    @field_validator("value")
    @classmethod
    def reject_floats(cls, value: Any) -> Any:
        if _contains_float(value):
            raise ValueError("Evidence values may not contain floating point at any nesting level")
        return value

    @field_validator("observed_at")
    @classmethod
    def observed_timestamp_is_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Evidence observation timestamp must include a timezone")
        return value


def _contains_float(value: Any) -> bool:
    if isinstance(value, float):
        return True
    if isinstance(value, dict):
        return any(_contains_float(item) for item in value.values())
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_contains_float(item) for item in value)
    return False


class Conflict(DomainModel):
    conflict_id: str
    field_name: str
    observation_ids: tuple[str, ...] = Field(min_length=2)
    reason: str
    status: VerificationStatus = VerificationStatus.CONFLICT
    resolved_value: Any | None = None
    resolved_by: str | None = None
    resolved_at: datetime | None = None
    resolution_reason: str | None = None

    @model_validator(mode="after")
    def resolution_is_atomic(self) -> Conflict:
        complete = all(
            item is not None
            for item in (
                self.resolved_value,
                self.resolved_by,
                self.resolved_at,
                self.resolution_reason,
            )
        )
        if self.status is VerificationStatus.VERIFIED and not complete:
            raise ValueError("Resolved conflicts require value, actor, timestamp, and reason")
        if self.status is VerificationStatus.CONFLICT and any(
            item is not None
            for item in (
                self.resolved_value,
                self.resolved_by,
                self.resolved_at,
                self.resolution_reason,
            )
        ):
            raise ValueError("Unresolved conflicts cannot contain partial resolution")
        return self


class QuantityRecord(DomainModel):
    quantity_id: str
    boq_line_id: str
    value: Decimal
    unit: str
    source_observation_ids: tuple[str, ...] = Field(min_length=1)
    source_priority: int = Field(ge=0)
    formula: str | None = None
    formula_inputs: dict[str, Decimal] = Field(default_factory=dict)
    rounding_mode: str
    rounding_scale: int = Field(ge=0, le=12)
    waste_factor: Decimal = Field(ge=0)
    alternative_quantity_ids: tuple[str, ...] = ()
    manual_change_id: str | None = None
    status: VerificationStatus


class ManualChange(DomainModel):
    change_id: str
    entity_type: str
    entity_id: str
    field_name: str
    before: Any
    after: Any
    reason: str = Field(min_length=1)
    changed_by: str
    changed_at: datetime
    critical: bool


class ApprovalRecord(DomainModel):
    approval_id: str
    task_type: str
    entity_type: str
    entity_id: str
    decision: ApprovalDecision
    decided_by: str
    decided_at: datetime
    reason: str = Field(min_length=1)
    related_change_ids: tuple[str, ...] = ()


class NomenclatureMatch(DomainModel):
    match_id: str
    source_item_id: str
    canonical_item_id: str | None = None
    match_class: MatchClass
    required_critical_attributes: frozenset[str]
    source_attributes: dict[str, str]
    canonical_attributes: dict[str, str]
    mismatched_attributes: frozenset[str] = frozenset()
    missing_attributes: frozenset[str] = frozenset()
    verified_by: str | None = None
    verified_at: datetime | None = None


class CommercialBasis(DomainModel):
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    vat_basis: VatBasis
    vat_rate: Decimal | None = Field(
        default=None,
        ge=0,
        le=1,
        max_digits=13,
        decimal_places=12,
    )
    unit: str = Field(min_length=1)
    package_quantity: Decimal = Field(
        gt=0,
        max_digits=38,
        decimal_places=12,
    )
    party_quantity: Decimal = Field(
        gt=0,
        max_digits=38,
        decimal_places=12,
    )
    region: str = Field(min_length=1)
    delivery_included: bool
    unloading_included: bool
    payment_terms: str = Field(min_length=1)

    @model_validator(mode="after")
    def vat_consistency(self) -> CommercialBasis:
        if self.vat_basis is VatBasis.NOT_APPLICABLE and self.vat_rate not in (None, Decimal("0")):
            raise ValueError("VAT rate conflicts with NOT_APPLICABLE basis")
        if self.vat_basis is not VatBasis.NOT_APPLICABLE and self.vat_rate is None:
            raise ValueError("VAT rate is required for inclusive/exclusive price")
        return self


class PriceSourceReference(DomainModel):
    source_type: PriceSourceType
    display_name: str = Field(min_length=1, max_length=500)
    source_item_name: str = Field(min_length=1, max_length=1000)
    source_record_id: str = Field(min_length=1, max_length=500)
    source_uri: str | None = Field(default=None, min_length=1, max_length=2000)

    @model_validator(mode="after")
    def external_sources_have_safe_uri(self) -> PriceSourceReference:
        if self.source_type is not PriceSourceType.SUPPLIER_QUOTE and self.source_uri is None:
            raise ValueError("An external price source requires its exact HTTPS URI")
        if self.source_uri is not None:
            parsed = urlsplit(self.source_uri)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or "\\" in self.source_uri
                or any(character.isspace() for character in self.source_uri)
            ):
                raise ValueError("Price source URI must be an HTTPS address without credentials")
        return self


class PriceQuote(DomainModel):
    quote_id: str
    item_id: str
    supplier_id: str | None = None
    evidence_class: PriceEvidenceClass
    source_reference: PriceSourceReference
    source_observation_id: str
    technical_attributes: dict[str, str]
    amount: Decimal = Field(gt=0, max_digits=38, decimal_places=12)
    basis: CommercialBasis
    quote_date: date
    valid_until: date | None
    lead_time_days: int | None = Field(default=None, ge=0)
    available: bool | None
    source_reliability: Decimal = Field(ge=0, le=1)
    status: PriceStatus = PriceStatus.UNNORMALIZED


class NormalizedPrice(DomainModel):
    normalized_price_id: str
    quote_id: str
    amount_per_unit: Decimal = Field(
        gt=0,
        max_digits=38,
        decimal_places=12,
    )
    target_basis: CommercialBasis
    fx_rate_id: str | None = None
    unit_conversion_id: str | None = None
    delivery_component: Decimal = Field(ge=0)
    unloading_component: Decimal = Field(ge=0)
    normalization_formula: str
    normalized_at: datetime
    status: PriceStatus = PriceStatus.NORMALIZED


class ScopeFinding(DomainModel):
    finding_id: str
    rule_id: str
    wbs_node_id: str
    required_work_code: str
    severity: Severity
    reason: str
    supporting_entity_ids: tuple[str, ...] = ()
    resolved: bool = False
    resolved_by: str | None = None
    resolution_reason: str | None = None


class ValidationFinding(DomainModel):
    code: FindingCode
    severity: Severity
    message: str
    entity_ids: tuple[str, ...] = ()
    details: dict[str, Any] = Field(default_factory=dict)


class CalculationLineResult(DomainModel):
    line_id: str
    category: CostCategory
    amount: Decimal
    currency: str = Field(pattern=r"^[A-Z]{3}$")


class CalculationResult(DomainModel):
    engine_version: str
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    lines: tuple[CalculationLineResult, ...]
    category_totals: dict[CostCategory, Decimal]
    grand_total: Decimal
    calculated_at: datetime


class IndependentValidationResult(DomainModel):
    validator_version: str
    passed: bool
    independently_calculated_total: Decimal
    primary_total: Decimal
    difference: Decimal
    tolerance: Decimal = Field(ge=0)
    findings: tuple[ValidationFinding, ...]
    validated_at: datetime


class CalculationSnapshot(DomainModel):
    snapshot_id: str
    project_id: str
    document_set_revision_id: str
    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_by: str
    created_at: datetime
    fixed: bool


class WorkflowTransition(DomainModel):
    project_id: str
    from_state: ApprovalState
    to_state: ApprovalState
    actor_id: str
    reason: str = Field(min_length=1)
    occurred_at: datetime


class GateDecision(DomainModel):
    requested_state: ApprovalState
    allowed: bool
    resulting_state: ApprovalState
    findings: tuple[ValidationFinding, ...]
