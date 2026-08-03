from __future__ import annotations

import socket
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tenderguard.domain.enums import PriceSourceType
from tenderguard.integrations.public_market import (
    PublicMarketPageClient,
    PublicMarketPageError,
    PublicMarketPageRequest,
    PublicMarketRawHttpExchange,
    replay_public_market_page_acquisition,
)

_SOURCE_URI = "https://supplier.example/products/cable-1"


def _request() -> PublicMarketPageRequest:
    return PublicMarketPageRequest(
        source_uri=_SOURCE_URI,
        source_type=PriceSourceType.SUPPLIER_WEBSITE,
        display_name="Example supplier",
    )


def _exchange(html: str) -> PublicMarketRawHttpExchange:
    return PublicMarketRawHttpExchange(
        request_uri=_SOURCE_URI,
        response_body=html.encode(),
        status_code=200,
        media_type="text/html",
        charset="utf-8",
    )


def test_json_ld_offer_is_decimal_replayable_and_never_ready_for_pricing() -> None:
    html = """
    <html><head><script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@type": "Product",
      "name": "Cable APvPg 1x240/70",
      "sku": "CABLE-240-70",
      "brand": {"@type": "Brand", "name": "Cable Plant"},
      "offers": {
        "@type": "Offer",
        "price": "533.28",
        "priceCurrency": "RUB",
        "availability": "https://schema.org/InStock",
        "url": "/products/cable-1",
        "priceValidUntil": "2026-08-31",
        "unitCode": "MTR"
      }
    }
    </script></head><body><p>Visible text is not evidence.</p></body></html>
    """
    exchange = _exchange(html)
    acquired = PublicMarketPageClient(fetch=lambda _request: exchange).acquire_page(
        _request(),
        retrieved_at=datetime(2026, 8, 3, 12, 0, tzinfo=UTC),
    )

    assert acquired.result.status == "UNVERIFIED"
    assert not acquired.result.ready_for_pricing
    assert len(acquired.result.candidates) == 1
    candidate = acquired.result.candidates[0]
    assert candidate.source_item_name == "Cable APvPg 1x240/70"
    assert candidate.source_record_locator == "sku:CABLE-240-70"
    assert candidate.brand_name == "Cable Plant"
    assert candidate.amount == Decimal("533.28")
    assert candidate.amount_literal == "533.28"
    assert candidate.currency == "RUB"
    assert candidate.unit_code == "MTR"
    assert candidate.offer_uri == _SOURCE_URI
    assert "VAT_BASIS_UNKNOWN" in acquired.result.pricing_blockers
    assert replay_public_market_page_acquisition(acquired) == acquired.result

    corrupted = PublicMarketRawHttpExchange(
        request_uri=exchange.request_uri,
        response_body=exchange.response_body.replace(b"533.28", b"633.28"),
        status_code=exchange.status_code,
        media_type=exchange.media_type,
        charset=exchange.charset,
    )
    with pytest.raises(ValueError, match="retained evidence"):
        acquired.__class__(
            request=acquired.request,
            result=acquired.result,
            exchange=corrupted,
        )


def test_microdata_extracts_only_scoped_product_offer() -> None:
    html = """
    <html><body>
      <div>Different product <span>99999.00 RUB</span></div>
      <article itemscope itemtype="https://schema.org/Product">
        <h1 itemprop="name">Coupling 1PST-10-150/240-B</h1>
        <meta itemprop="sku" content="57817">
        <span itemprop="brand">KVT</span>
        <section itemprop="offers" itemscope itemtype="https://schema.org/Offer">
          <meta itemprop="price" content="5044.00">
          <meta itemprop="priceCurrency" content="RUB">
          <link itemprop="availability" href="https://schema.org/InStock">
          <link itemprop="url" href="/products/mufta-57817">
          <meta itemprop="unitCode" content="H87">
        </section>
      </article>
    </body></html>
    """

    result = PublicMarketPageClient(fetch=lambda _request: _exchange(html)).acquire_page(
        _request()
    ).result

    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.extraction_method == "MICRODATA"
    assert candidate.source_item_name == "Coupling 1PST-10-150/240-B"
    assert candidate.source_record_locator == "sku:57817"
    assert candidate.amount == Decimal("5044.00")
    assert candidate.currency == "RUB"
    assert candidate.unit_code == "H87"
    assert candidate.offer_uri == "https://supplier.example/products/mufta-57817"


def test_multiple_unidentified_offer_variants_are_blocked_as_ambiguous() -> None:
    html = """
    <div itemscope itemtype="https://schema.org/Product">
      <span itemprop="name">Coupling 1PST</span>
      <div itemscope itemtype="https://schema.org/Offer">
        <meta itemprop="price" content="3135.00">
        <meta itemprop="priceCurrency" content="RUB">
      </div>
      <div itemscope itemtype="https://schema.org/Offer">
        <meta itemprop="price" content="4290.00">
        <meta itemprop="priceCurrency" content="RUB">
      </div>
    </div>
    """

    result = PublicMarketPageClient(fetch=lambda _request: _exchange(html)).acquire_page(
        _request()
    ).result

    assert result.status == "BLOCKED"
    assert not result.candidates
    assert "MARKET_MICRODATA_MULTIPLE_OFFERS_AMBIGUOUS" in result.extraction_findings


@pytest.mark.parametrize(
    "html,expected_finding",
    [
        ("<html><body>Cable price 533.28 RUB</body></html>", None),
        (
            """
            <div itemscope itemtype="https://schema.org/Product">
              <span itemprop="name">Cable</span>
              <div itemscope itemtype="https://schema.org/Offer">
                <meta itemprop="price" content="1,234.50">
                <meta itemprop="priceCurrency" content="RUB">
              </div>
            </div>
            """,
            "MARKET_STRUCTURED_PRICE_LITERAL_INVALID",
        ),
        (
            '<script type="application/ld+json">{"@type":</script>',
            "MARKET_JSON_LD_INVALID",
        ),
    ],
)
def test_visible_ambiguous_or_invalid_prices_are_blocked(
    html: str,
    expected_finding: str | None,
) -> None:
    result = PublicMarketPageClient(fetch=lambda _request: _exchange(html)).acquire_page(
        _request()
    ).result

    assert result.status == "BLOCKED"
    assert not result.candidates
    assert "STRUCTURED_MARKET_OFFER_NOT_FOUND" in result.pricing_blockers
    if expected_finding is not None:
        assert expected_finding in result.extraction_findings


def test_market_request_rejects_unsafe_or_non_market_sources() -> None:
    for uri in (
        "http://supplier.example/product",
        "https://user:password@supplier.example/product",
        "https://localhost/product",
        "https://supplier.example:8443/product",
        "https://supplier.example/product#price",
    ):
        with pytest.raises(ValueError):
            PublicMarketPageRequest(
                source_uri=uri,
                source_type=PriceSourceType.SUPPLIER_WEBSITE,
                display_name="Supplier",
            )

    with pytest.raises(ValueError, match="website or marketplace"):
        PublicMarketPageRequest(
            source_uri=_SOURCE_URI,
            source_type=PriceSourceType.FGIS_CS,
            display_name="Wrong source class",
        )


def test_market_client_blocks_private_dns_destination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "tenderguard.integrations.public_market.socket.getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        ],
    )

    with pytest.raises(PublicMarketPageError) as captured:
        PublicMarketPageClient().acquire_page(_request())

    assert captured.value.code == "MARKET_PRIVATE_DESTINATION_BLOCKED"
    assert not captured.value.retryable


def test_market_page_rejects_media_type_charset_and_failed_retained_status() -> None:
    request = _request()
    for exchange, expected in (
        (
            PublicMarketRawHttpExchange(
                request_uri=_SOURCE_URI,
                response_body=b"{}",
                status_code=200,
                media_type="application/json",
                charset="utf-8",
            ),
            "MARKET_MEDIA_TYPE_INVALID",
        ),
        (
            PublicMarketRawHttpExchange(
                request_uri=_SOURCE_URI,
                response_body=b"<html></html>",
                status_code=200,
                media_type="text/html",
                charset="utf-16",
            ),
            "MARKET_CHARSET_UNSUPPORTED",
        ),
    ):
        with pytest.raises(PublicMarketPageError) as captured:
            PublicMarketPageClient(fetch=lambda _request, item=exchange: item).acquire_page(
                request
            )
        assert captured.value.code == expected

    failed = PublicMarketRawHttpExchange(
        request_uri=_SOURCE_URI,
        response_body=b"not found",
        status_code=404,
        media_type="text/html",
        charset="utf-8",
    )
    with pytest.raises(ValueError, match="not successful"):
        PublicMarketPageClient(fetch=lambda _request: failed).acquire_page(request)
