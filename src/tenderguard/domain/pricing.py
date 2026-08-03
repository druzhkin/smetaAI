from __future__ import annotations

from datetime import date, datetime
from decimal import (
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    Decimal,
    InvalidOperation,
    localcontext,
)

from pydantic import Field, field_validator

from tenderguard.domain.common import content_hash
from tenderguard.domain.enums import (
    PriceEvidenceClass,
    PriceSourceType,
    PriceStatus,
    VatBasis,
)
from tenderguard.domain.models import (
    CommercialBasis,
    DomainModel,
    NormalizedPrice,
    PriceQuote,
)


class PriceAdjustment(DomainModel):
    adjustment_id: str
    kind: str
    amount_per_target_unit: Decimal = Field(max_digits=38, decimal_places=12)
    evidence_id: str
    reason: str


class NormalizationRequest(DomainModel):
    policy_version_id: str = Field(min_length=1)
    target_basis: CommercialBasis
    source_units_per_target_unit: Decimal = Field(gt=0)
    unit_conversion_id: str | None = None
    target_currency_per_source_currency: Decimal = Field(gt=0)
    fx_rate_id: str | None = None
    adjustments: tuple[PriceAdjustment, ...] = ()
    region_adjustment_id: str | None = None
    party_adjustment_id: str | None = None
    payment_adjustment_id: str | None = None
    rounding_scale: int = Field(ge=0, le=12)
    rounding_mode: str

    @field_validator("rounding_mode")
    @classmethod
    def rounding_mode_is_supported(cls, value: str) -> str:
        if value not in {"ROUND_HALF_UP", "ROUND_HALF_EVEN"}:
            raise ValueError(f"Unsupported price normalization rounding mode: {value}")
        return value


class TriangulationResult(DomainModel):
    item_id: str
    quote_ids: tuple[str, ...]
    passed: bool
    resulting_status: PriceStatus
    missing_evidence_classes: tuple[PriceEvidenceClass, ...]
    missing_source_groups: tuple[str, ...]
    reason: str


def _vat_exclusive(amount: Decimal, basis: CommercialBasis) -> Decimal:
    if basis.vat_basis is VatBasis.INCLUSIVE:
        if basis.vat_rate is None:
            raise ValueError("Inclusive source VAT basis has no VAT rate")
        return amount / (Decimal("1") + basis.vat_rate)
    return amount


def _apply_target_vat(amount: Decimal, basis: CommercialBasis) -> Decimal:
    if basis.vat_basis is VatBasis.INCLUSIVE:
        if basis.vat_rate is None:
            raise ValueError("Inclusive target VAT basis has no VAT rate")
        return amount * (Decimal("1") + basis.vat_rate)
    return amount


def normalize_quote(
    quote: PriceQuote,
    request: NormalizationRequest,
    *,
    normalized_at: datetime,
) -> NormalizedPrice:
    source = quote.basis
    target = request.target_basis
    if source.unit != target.unit and request.unit_conversion_id is None:
        raise ValueError("Unit conversion requires a versioned conversion reference")
    if source.currency != target.currency and request.fx_rate_id is None:
        raise ValueError("Currency conversion requires a versioned FX rate")
    if source.region != target.region and request.region_adjustment_id is None:
        raise ValueError("Region mismatch requires an evidenced logistics/region adjustment")
    if source.party_quantity != target.party_quantity and request.party_adjustment_id is None:
        raise ValueError("Party-size mismatch requires an evidenced adjustment")
    if source.payment_terms != target.payment_terms and request.payment_adjustment_id is None:
        raise ValueError("Payment-term mismatch requires an evidenced financing adjustment")

    adjustment_kinds = {adjustment.kind for adjustment in request.adjustments}
    if (
        target.delivery_included
        and not source.delivery_included
        and "delivery" not in adjustment_kinds
    ):
        raise ValueError("Delivered target basis requires a delivery adjustment")
    if (
        target.unloading_included
        and not source.unloading_included
        and "unloading" not in adjustment_kinds
    ):
        raise ValueError("Target basis with unloading requires an unloading adjustment")

    try:
        with localcontext() as decimal_context:
            # Operands are bounded to 38 significant digits. This precision
            # prevents the process-global Decimal context from choosing a
            # financial result before the explicit policy rounding below.
            decimal_context.prec = 160
            per_source_unit = quote.amount / source.package_quantity
            exclusive_source = _vat_exclusive(per_source_unit, source)
            converted = (
                exclusive_source
                * request.source_units_per_target_unit
                * request.target_currency_per_source_currency
            )
            delivery = sum(
                (
                    adjustment.amount_per_target_unit
                    for adjustment in request.adjustments
                    if adjustment.kind == "delivery"
                ),
                start=Decimal("0"),
            )
            unloading = sum(
                (
                    adjustment.amount_per_target_unit
                    for adjustment in request.adjustments
                    if adjustment.kind == "unloading"
                ),
                start=Decimal("0"),
            )
            other = sum(
                (
                    adjustment.amount_per_target_unit
                    for adjustment in request.adjustments
                    if adjustment.kind not in {"delivery", "unloading"}
                ),
                start=Decimal("0"),
            )
            normalized_exclusive = converted + delivery + unloading + other
            unrounded_amount = _apply_target_vat(normalized_exclusive, target)
            rounding = {
                "ROUND_HALF_UP": ROUND_HALF_UP,
                "ROUND_HALF_EVEN": ROUND_HALF_EVEN,
            }[request.rounding_mode]
            normalized_amount = unrounded_amount.quantize(
                Decimal(1).scaleb(-request.rounding_scale),
                rounding=rounding,
            )
    except InvalidOperation as error:
        raise ValueError("Price normalization exceeded the controlled decimal precision") from error
    formula_record = {
        "policy_version_id": request.policy_version_id,
        "source_quote_id": quote.quote_id,
        "source_amount": quote.amount,
        "source_package_quantity": source.package_quantity,
        "source_vat_basis": source.vat_basis,
        "source_vat_rate": source.vat_rate,
        "source_units_per_target_unit": request.source_units_per_target_unit,
        "unit_conversion_id": request.unit_conversion_id,
        "fx_rate": request.target_currency_per_source_currency,
        "fx_rate_id": request.fx_rate_id,
        "adjustments": request.adjustments,
        "region_adjustment_id": request.region_adjustment_id,
        "party_adjustment_id": request.party_adjustment_id,
        "payment_adjustment_id": request.payment_adjustment_id,
        "target_vat_basis": target.vat_basis,
        "target_vat_rate": target.vat_rate,
        "rounding_scale": request.rounding_scale,
        "rounding_mode": request.rounding_mode,
    }
    formula_hash = content_hash(formula_record)
    return NormalizedPrice(
        normalized_price_id=f"normalized-{formula_hash[:24]}",
        quote_id=quote.quote_id,
        amount_per_unit=normalized_amount,
        target_basis=target,
        fx_rate_id=request.fx_rate_id,
        unit_conversion_id=request.unit_conversion_id,
        delivery_component=delivery,
        unloading_component=unloading,
        normalization_formula=f"sha256:{formula_hash}",
        normalized_at=normalized_at,
        status=PriceStatus.NORMALIZED,
    )


def evaluate_triangulation(
    *,
    item_id: str,
    quotes: tuple[PriceQuote, ...],
    as_of: date,
    critical: bool,
) -> TriangulationResult:
    eligible = tuple(
        quote
        for quote in quotes
        if quote.item_id == item_id
        and quote.status in {PriceStatus.NORMALIZED, PriceStatus.VERIFIED}
        and quote.available is True
        and quote.lead_time_days is not None
        and quote.quote_date <= as_of
        and quote.valid_until is not None
        and quote.valid_until >= as_of
    )
    present = {quote.evidence_class for quote in eligible}
    required = {
        PriceEvidenceClass.OFFICIAL_OR_PRIMARY,
        PriceEvidenceClass.INDEPENDENT_MARKET,
        PriceEvidenceClass.INTERNAL_HISTORY,
    }
    missing = set(required - present)
    source_types = {quote.source_reference.source_type for quote in eligible}
    missing_source_groups: list[str] = []
    if PriceSourceType.FGIS_CS not in source_types:
        missing_source_groups.append("FGIS_CS")
    if PriceSourceType.WON_TENDER not in source_types:
        missing_source_groups.append("WON_TENDER")
    if not source_types.intersection(
        {
            PriceSourceType.MARKETPLACE,
            PriceSourceType.SUPPLIER_WEBSITE,
        }
    ):
        missing_source_groups.append("MARKET")
    passed = not missing and not missing_source_groups
    return TriangulationResult(
        item_id=item_id,
        quote_ids=tuple(quote.quote_id for quote in eligible),
        passed=passed,
        resulting_status=PriceStatus.VERIFIED if passed else PriceStatus.RFQ_REQUIRED,
        missing_evidence_classes=tuple(sorted(missing, key=lambda item: item.value)),
        missing_source_groups=tuple(missing_source_groups),
        reason=(
            "FGIS CS, won-tender and independent-market evidence are present"
            if passed
            else "Price requires the missing source groups, independent evidence, or an RFQ"
        ),
    )
