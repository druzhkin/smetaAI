from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from tenderguard.application.pricing import NormalizePriceCommand, PriceQuoteDraft
from tenderguard.domain.enums import (
    PriceEvidenceClass,
    PriceSourceType,
    PriceStatus,
    VatBasis,
)
from tenderguard.domain.models import CommercialBasis, PriceQuote, PriceSourceReference
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
    source_type = {
        PriceEvidenceClass.OFFICIAL_OR_PRIMARY: PriceSourceType.FGIS_CS,
        PriceEvidenceClass.INDEPENDENT_MARKET: PriceSourceType.MARKETPLACE,
        PriceEvidenceClass.INTERNAL_HISTORY: PriceSourceType.WON_TENDER,
        PriceEvidenceClass.COMMERCIAL_QUOTE: PriceSourceType.SUPPLIER_QUOTE,
    }[evidence]
    return PriceQuote(
        quote_id=identifier,
        item_id="pipe-1",
        supplier_id=f"supplier-{identifier}",
        evidence_class=evidence,
        source_reference=PriceSourceReference(
            source_type=source_type,
            display_name=f"Source {identifier}",
            source_item_name="Steel pipe DN100 PN16",
            source_record_id=f"record-{identifier}",
            source_uri=(
                None
                if source_type is PriceSourceType.SUPPLIER_QUOTE
                else f"https://prices.example.test/{identifier}"
            ),
        ),
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
        policy_version_id="price-policy-v1",
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
        rounding_scale=2,
        rounding_mode="ROUND_HALF_UP",
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
        policy_version_id="price-policy-v1",
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
        rounding_scale=2,
        rounding_mode="ROUND_HALF_UP",
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

    changed_policy = normalize_quote(
        quote("q1", PriceEvidenceClass.OFFICIAL_OR_PRIMARY),
        request.model_copy(
            update={
                "policy_version_id": "price-policy-v2",
                "region_adjustment_id": "logistics-rule-v2",
            }
        ),
        normalized_at=datetime(2026, 7, 23, tzinfo=UTC),
    )
    assert changed_policy.amount_per_unit == normalized.amount_per_unit
    assert changed_policy.normalized_price_id != normalized.normalized_price_id
    assert changed_policy.normalization_formula != normalized.normalization_formula


def test_price_normalization_applies_explicit_policy_rounding() -> None:
    source = quote("q-rounding", PriceEvidenceClass.OFFICIAL_OR_PRIMARY)
    source = source.model_copy(
        update={
            "amount": Decimal("1"),
            "basis": source.basis.model_copy(update={"package_quantity": Decimal("3")}),
        }
    )
    request = NormalizationRequest(
        policy_version_id="price-policy-v1",
        target_basis=source.basis,
        source_units_per_target_unit=Decimal("1"),
        target_currency_per_source_currency=Decimal("1"),
        rounding_scale=2,
        rounding_mode="ROUND_HALF_UP",
    )

    normalized = normalize_quote(
        source,
        request,
        normalized_at=datetime(2026, 7, 23, tzinfo=UTC),
    )

    assert normalized.amount_per_unit == Decimal("0.33")


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
        quotes=(*two_sources, quote("q3", PriceEvidenceClass.INTERNAL_HISTORY)),
        as_of=date(2026, 7, 23),
        critical=True,
    )
    assert result.passed
    assert result.resulting_status is PriceStatus.VERIFIED


def test_triangulation_excludes_commercially_incomplete_or_future_quotes() -> None:
    complete = quote("q1", PriceEvidenceClass.OFFICIAL_OR_PRIMARY)
    incomplete = quote("q2", PriceEvidenceClass.INDEPENDENT_MARKET).model_copy(
        update={"available": None, "valid_until": None, "lead_time_days": None}
    )
    future = quote("q3", PriceEvidenceClass.COMMERCIAL_QUOTE).model_copy(
        update={"quote_date": date(2026, 7, 24)}
    )
    result = evaluate_triangulation(
        item_id="pipe-1",
        quotes=(complete, incomplete, future),
        as_of=date(2026, 7, 23),
        critical=True,
    )
    assert result.quote_ids == ("q1",)
    assert not result.passed
    assert set(result.missing_evidence_classes) == {
        PriceEvidenceClass.INDEPENDENT_MARKET,
        PriceEvidenceClass.INTERNAL_HISTORY,
    }


def test_quote_and_normalization_commands_reject_ambiguous_commercial_inputs() -> None:
    with pytest.raises(ValueError, match="supplier identity"):
        PriceQuoteDraft(
            item_id="pipe-1",
            evidence_class=PriceEvidenceClass.COMMERCIAL_QUOTE,
            source_reference=PriceSourceReference(
                source_type=PriceSourceType.SUPPLIER_QUOTE,
                display_name="Supplier",
                source_item_name="Steel pipe DN100",
                source_record_id="quote-1",
            ),
            source_observation_id="observation-quote",
            technical_attributes={"diameter": "DN100"},
            amount=Decimal("100"),
            basis=basis(region="Moscow"),
            quote_date=date(2026, 7, 20),
            valid_until=date(2026, 8, 20),
            lead_time_days=10,
            available=True,
            source_reliability=Decimal("0.9"),
        )
    with pytest.raises(ValueError, match="cannot end before"):
        PriceQuoteDraft(
            item_id="pipe-1",
            supplier_id="supplier-1",
            evidence_class=PriceEvidenceClass.COMMERCIAL_QUOTE,
            source_reference=PriceSourceReference(
                source_type=PriceSourceType.SUPPLIER_QUOTE,
                display_name="Supplier",
                source_item_name="Steel pipe DN100",
                source_record_id="quote-1",
            ),
            source_observation_id="observation-quote",
            technical_attributes={"diameter": "DN100"},
            amount=Decimal("100"),
            basis=basis(region="Moscow"),
            quote_date=date(2026, 7, 20),
            valid_until=date(2026, 7, 19),
            lead_time_days=10,
            available=True,
            source_reliability=Decimal("0.9"),
        )
    with pytest.raises(ValueError, match="must be unique"):
        NormalizePriceCommand(
            quote_id="quote-1",
            adjustment_ids=("delivery-1", "delivery-1"),
        )


def test_price_source_reference_rejects_missing_or_unsafe_external_uri() -> None:
    with pytest.raises(ValueError, match="exact HTTPS URI"):
        PriceSourceReference(
            source_type=PriceSourceType.FGIS_CS,
            display_name="ФГИС ЦС",
            source_item_name="Pipe DN100",
            source_record_id="fgis-1",
        )
    with pytest.raises(ValueError, match="without credentials"):
        PriceSourceReference(
            source_type=PriceSourceType.MARKETPLACE,
            display_name="Market",
            source_item_name="Pipe DN100",
            source_record_id="market-1",
            source_uri="https://user:password@example.test/price",
        )


def test_price_quote_rejects_source_type_evidence_class_conflict() -> None:
    with pytest.raises(ValueError, match="conflicts with the declared source type"):
        PriceQuoteDraft(
            item_id="pipe-1",
            supplier_id="supplier-1",
            evidence_class=PriceEvidenceClass.INDEPENDENT_MARKET,
            source_reference=PriceSourceReference(
                source_type=PriceSourceType.FGIS_CS,
                display_name="ФГИС ЦС",
                source_item_name="Pipe DN100",
                source_record_id="fgis-1",
                source_uri="https://fgis.example.test/price/1",
            ),
            source_observation_id="observation-quote",
            technical_attributes={"diameter": "DN100"},
            amount=Decimal("100"),
            basis=basis(region="Moscow"),
            quote_date=date(2026, 7, 20),
            valid_until=date(2026, 8, 20),
            lead_time_days=10,
            available=True,
            source_reliability=Decimal("0.9"),
        )
