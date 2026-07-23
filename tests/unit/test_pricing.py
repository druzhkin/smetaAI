from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from tenderguard.domain.enums import (
    PriceEvidenceClass,
    PriceStatus,
    VatBasis,
)
from tenderguard.domain.models import CommercialBasis, PriceQuote
from tenderguard.domain.pricing import (
    NormalizationRequest,
    PriceAdjustment,
    evaluate_triangulation,
    normalize_quote,
)


def basis(
    *,
    region: str,
    currency: str = "RUB",
    vat_basis: VatBasis = VatBasis.INCLUSIVE,
    delivery: bool = False,
) -> CommercialBasis:
    return CommercialBasis(
        currency=currency,
        vat_basis=vat_basis,
        vat_rate=Decimal("0.20"),
        unit="m",
        package_quantity=Decimal("10"),
        party_quantity=Decimal("1000"),
        region=region,
        delivery_included=delivery,
        unloading_included=False,
        payment_terms="30 days",
    )


def quote(identifier: str, evidence: PriceEvidenceClass) -> PriceQuote:
    return PriceQuote(
        quote_id=identifier,
        item_id="pipe-1",
        supplier_id=f"supplier-{identifier}",
        evidence_class=evidence,
        source_observation_id=f"obs-{identifier}",
        technical_attributes={"diameter": "DN100", "pressure": "PN16"},
        amount=Decimal("1200"),
        basis=basis(region="Moscow"),
        quote_date=date(2026, 7, 20),
        valid_until=date(2026, 8, 20),
        lead_time_days=10,
        available=True,
        source_reliability=Decimal("0.9"),
        status=PriceStatus.NORMALIZED,
    )


def test_price_normalization_requires_explicit_region_adjustment() -> None:
    target = basis(region="Kazan", delivery=True)
    request = NormalizationRequest(
        target_basis=target,
        source_units_per_target_unit=Decimal("1"),
        target_currency_per_source_currency=Decimal("1"),
        adjustments=(
            PriceAdjustment(
                adjustment_id="delivery-1",
                kind="delivery",
                amount_per_target_unit=Decimal("12"),
                evidence_id="logistics-quote-1",
                reason="Delivery to site",
            ),
        ),
    )
    with pytest.raises(ValueError, match="Region mismatch"):
        normalize_quote(
            quote("q1", PriceEvidenceClass.OFFICIAL_OR_PRIMARY),
            request,
            normalized_at=datetime(2026, 7, 23, tzinfo=UTC),
        )


def test_price_normalization_reproduces_commercial_basis_formula() -> None:
    target = basis(region="Kazan", delivery=True)
    request = NormalizationRequest(
        target_basis=target,
        source_units_per_target_unit=Decimal("1"),
        target_currency_per_source_currency=Decimal("1"),
        region_adjustment_id="logistics-rule-v1",
        adjustments=(
            PriceAdjustment(
                adjustment_id="delivery-1",
                kind="delivery",
                amount_per_target_unit=Decimal("12"),
                evidence_id="logistics-quote-1",
                reason="Delivery to site",
            ),
        ),
    )
    normalized = normalize_quote(
        quote("q1", PriceEvidenceClass.OFFICIAL_OR_PRIMARY),
        request,
        normalized_at=datetime(2026, 7, 23, tzinfo=UTC),
    )
    assert normalized.amount_per_unit == Decimal("134.4")
    assert normalized.delivery_component == Decimal("12")
    assert normalized.normalization_formula.startswith("sha256:")
    assert normalized.status is PriceStatus.NORMALIZED


def test_critical_price_requires_three_way_triangulation() -> None:
    two_sources = (
        quote("q1", PriceEvidenceClass.OFFICIAL_OR_PRIMARY),
        quote("q2", PriceEvidenceClass.INDEPENDENT_MARKET),
    )
    result = evaluate_triangulation(
        item_id="pipe-1",
        quotes=two_sources,
        as_of=date(2026, 7, 23),
        critical=True,
    )
    assert not result.passed
    assert result.resulting_status is PriceStatus.RFQ_REQUIRED

    result = evaluate_triangulation(
        item_id="pipe-1",
        quotes=(*two_sources, quote("q3", PriceEvidenceClass.COMMERCIAL_QUOTE)),
        as_of=date(2026, 7, 23),
        critical=True,
    )
    assert result.passed
    assert result.resulting_status is PriceStatus.VERIFIED
