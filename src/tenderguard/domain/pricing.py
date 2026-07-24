from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import Field

from tenderguard.domain.common import content_hash
from tenderguard.domain.enums import PriceEvidenceClass, PriceStatus, VatBasis
from tenderguard.domain.models import (
    CommercialBasis,
    DomainModel,
    NormalizedPrice,
    PriceQuote,
)


class PriceAdjustment(DomainModel):
    adjustment_id: str
    kind: str
    amount_per_target_unit: Decimal
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


class TriangulationResult(DomainModel):
    item_id: str
    quote_ids: tuple[str, ...]
    passed: bool
    resulting_status: PriceStatus
    missing_evidence_classes: tuple[PriceEvidenceClass, ...]
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
    normalized_amount = _apply_target_vat(normalized_exclusive, target)
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
    }
    missing = set(required - present)
    has_internal_or_quote = bool(
        present
        & {
            PriceEvidenceClass.INTERNAL_HISTORY,
            PriceEvidenceClass.COMMERCIAL_QUOTE,
        }
    )
    if critical and not has_internal_or_quote:
        missing.add(PriceEvidenceClass.COMMERCIAL_QUOTE)
    passed = not missing
    return TriangulationResult(
        item_id=item_id,
        quote_ids=tuple(quote.quote_id for quote in eligible),
        passed=passed,
        resulting_status=PriceStatus.VERIFIED if passed else PriceStatus.RFQ_REQUIRED,
        missing_evidence_classes=tuple(sorted(missing, key=lambda item: item.value)),
        reason=(
            "Required independent evidence classes are present"
            if passed
            else "Price requires additional independent evidence or an RFQ"
        ),
    )
