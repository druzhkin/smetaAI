from __future__ import annotations

import codecs
import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Any, Literal
from urllib.parse import SplitResult, urljoin, urlsplit

from pydantic import Field, field_validator, model_validator

from tenderguard.domain.common import ensure_utc, utc_now
from tenderguard.domain.enums import PriceSourceType
from tenderguard.domain.models import DomainModel

PUBLIC_MARKET_SCHEMA_VERSION = "public-market-page/v1"

_ALLOWED_SOURCE_TYPES = frozenset(
    {PriceSourceType.MARKETPLACE, PriceSourceType.SUPPLIER_WEBSITE}
)
_RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
_ALLOWED_MEDIA_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_ALLOWED_CHARSETS = frozenset({"utf-8", "cp1251"})
_VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source"}
)
_PRICE_LITERAL_PATTERN = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
_META_CHARSET_PATTERN = re.compile(
    rb"<meta\s+[^>]*(?:charset\s*=\s*[\"']?\s*|content\s*=\s*[\"'][^\"']*charset\s*=\s*)([A-Za-z0-9._-]+)",
    re.IGNORECASE,
)


class PublicMarketPageRequest(DomainModel):
    source_uri: str = Field(min_length=1, max_length=2000)
    source_type: PriceSourceType
    display_name: str = Field(min_length=1, max_length=500)

    @field_validator("display_name")
    @classmethod
    def display_name_is_exact(cls, value: str) -> str:
        if value != value.strip() or any(character in value for character in "\r\n\x00"):
            raise ValueError("Market source display name must be an exact single-line literal")
        return value

    @model_validator(mode="after")
    def request_is_public_https(self) -> PublicMarketPageRequest:
        if self.source_type not in _ALLOWED_SOURCE_TYPES:
            raise ValueError("Diagnostic market request requires a website or marketplace type")
        _validate_https_uri(self.source_uri)
        return self


class PublicMarketOfferCandidate(DomainModel):
    extraction_method: Literal["JSON_LD", "MICRODATA"]
    source_path: str = Field(min_length=1, max_length=2000)
    source_item_name: str = Field(min_length=1, max_length=2000)
    source_record_locator: str = Field(min_length=1, max_length=1000)
    brand_name: str | None = Field(default=None, min_length=1, max_length=500)
    amount: Decimal = Field(gt=0, max_digits=38, decimal_places=12)
    amount_literal: str = Field(min_length=1, max_length=100)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    availability_literal: str | None = Field(default=None, min_length=1, max_length=1000)
    offer_uri: str | None = Field(default=None, min_length=1, max_length=2000)
    price_valid_until_literal: str | None = Field(default=None, min_length=1, max_length=100)
    unit_code: str | None = Field(default=None, min_length=1, max_length=100)
    unit_text: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator(
        "source_path",
        "source_item_name",
        "source_record_locator",
        "brand_name",
        "availability_literal",
        "price_valid_until_literal",
        "unit_code",
        "unit_text",
    )
    @classmethod
    def candidate_literals_are_exact(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value != value.strip() or any(character in value for character in "\r\n\x00"):
            raise ValueError("Market candidate text must be an exact single-line literal")
        return value

    @model_validator(mode="after")
    def literal_reproduces_amount(self) -> PublicMarketOfferCandidate:
        if not _PRICE_LITERAL_PATTERN.fullmatch(self.amount_literal):
            raise ValueError("Market price literal is not an unambiguous decimal")
        try:
            parsed = Decimal(self.amount_literal)
        except InvalidOperation as error:
            raise ValueError("Market price literal is invalid") from error
        if not parsed.is_finite() or parsed != self.amount:
            raise ValueError("Market price literal differs from the decimal amount")
        if not _CURRENCY_PATTERN.fullmatch(self.currency):
            raise ValueError("Market price currency must be an explicit ISO-style code")
        if self.offer_uri is not None:
            _validate_https_uri(self.offer_uri)
        return self


class PublicMarketPageResult(DomainModel):
    schema_version: str = PUBLIC_MARKET_SCHEMA_VERSION
    source_uri: str = Field(min_length=1, max_length=2000)
    source_type: PriceSourceType
    display_name: str = Field(min_length=1, max_length=500)
    candidates: tuple[PublicMarketOfferCandidate, ...] = Field(max_length=200)
    extraction_findings: tuple[str, ...] = Field(max_length=200)
    response_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_size_bytes: int = Field(gt=0)
    response_media_type: str = Field(min_length=1, max_length=200)
    response_charset: str = Field(min_length=1, max_length=100)
    retrieved_at: datetime
    status: Literal["UNVERIFIED", "BLOCKED"]
    ready_for_pricing: bool = False
    pricing_blockers: tuple[str, ...] = Field(min_length=1)

    @field_validator("retrieved_at")
    @classmethod
    def retrieved_at_is_aware(cls, value: datetime) -> datetime:
        normalized = ensure_utc(value)
        if normalized is None or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Market retrieval timestamp must include a timezone")
        return normalized

    @model_validator(mode="after")
    def result_is_complete_and_fail_closed(self) -> PublicMarketPageResult:
        if self.schema_version != PUBLIC_MARKET_SCHEMA_VERSION:
            raise ValueError("Unsupported public market page schema")
        if self.ready_for_pricing:
            raise ValueError("Diagnostic public market data cannot release a price")
        _validate_https_uri(self.source_uri)
        if self.source_type not in _ALLOWED_SOURCE_TYPES:
            raise ValueError("Public market result has an unsupported source type")
        identities = tuple(
            (
                item.extraction_method,
                item.source_path,
                item.source_item_name,
                item.amount,
                item.currency,
            )
            for item in self.candidates
        )
        if len(identities) != len(set(identities)):
            raise ValueError("Public market result contains duplicate offer candidates")
        if len(self.extraction_findings) != len(set(self.extraction_findings)):
            raise ValueError("Public market extraction findings must be unique")
        expected_status = "UNVERIFIED" if self.candidates else "BLOCKED"
        if self.status != expected_status:
            raise ValueError("Public market result status contradicts its candidates")
        if self.pricing_blockers != _page_blockers(self.candidates, self.extraction_findings):
            raise ValueError("Public market blockers contradict the extracted evidence")
        return self


@dataclass(frozen=True)
class PublicMarketRawHttpExchange:
    request_uri: str
    response_body: bytes
    status_code: int
    media_type: str
    charset: str | None

    def __post_init__(self) -> None:
        _validate_https_uri(self.request_uri)
        if not self.response_body:
            raise ValueError("Public market captured response body is empty")
        if not 100 <= self.status_code <= 599:
            raise ValueError("Public market captured response status is invalid")
        if (
            not self.media_type
            or self.media_type != self.media_type.strip()
            or any(character in self.media_type for character in "\r\n\x00")
        ):
            raise ValueError("Public market captured media type is invalid")
        if self.charset is not None and (
            self.charset != self.charset.strip()
            or any(character in self.charset for character in "\r\n\x00")
        ):
            raise ValueError("Public market captured charset is invalid")

    @property
    def response_sha256(self) -> str:
        return hashlib.sha256(self.response_body).hexdigest()


@dataclass(frozen=True)
class PublicMarketPageAcquisition:
    request: PublicMarketPageRequest
    result: PublicMarketPageResult
    exchange: PublicMarketRawHttpExchange

    def __post_init__(self) -> None:
        if self.exchange.status_code != 200:
            raise ValueError("Successful public market acquisition has a failed response")
        if self.exchange.request_uri != self.request.source_uri:
            raise ValueError("Public market acquisition URI differs from its request")
        if (
            self.result.source_uri != self.request.source_uri
            or self.result.source_type is not self.request.source_type
            or self.result.display_name != self.request.display_name
        ):
            raise ValueError("Public market acquisition result differs from its request")
        if (
            self.result.response_sha256 != self.exchange.response_sha256
            or self.result.response_size_bytes != len(self.exchange.response_body)
            or self.result.response_media_type != self.exchange.media_type
        ):
            raise ValueError("Public market acquisition result differs from retained evidence")


class PublicMarketPageError(RuntimeError):
    def __init__(
        self,
        *,
        code: str,
        retryable: bool = False,
        exchange: PublicMarketRawHttpExchange | None = None,
    ) -> None:
        self.code = code
        self.retryable = retryable
        self.exchange = exchange
        super().__init__(code)


MarketFetch = Callable[[PublicMarketPageRequest], PublicMarketRawHttpExchange]


class PublicMarketPageClient:
    """Read-only collector for structured product offers on exact public pages.

    Search results, visible prose and snippets are deliberately not parsed as
    prices. Only Schema.org JSON-LD and microdata Product/Offer properties are
    retained as diagnostic candidates.
    """

    def __init__(
        self,
        *,
        timeout_seconds: int = 30,
        max_response_bytes: int = 2_000_000,
        ssl_context: ssl.SSLContext | None = None,
        fetch: MarketFetch | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Public market timeout must be positive")
        if max_response_bytes <= 0:
            raise ValueError("Public market response limit must be positive")
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._ssl_context = ssl_context or ssl.create_default_context()
        self._fetch = fetch or self._fetch_https

    def acquire_page(
        self,
        request: PublicMarketPageRequest,
        *,
        retrieved_at: datetime | None = None,
    ) -> PublicMarketPageAcquisition:
        exchange = self._fetch(request)
        try:
            result = _parse_market_exchange(
                request=request,
                exchange=exchange,
                retrieved_at=retrieved_at or utc_now(),
            )
        except PublicMarketPageError as error:
            if error.exchange is not None:
                raise
            raise PublicMarketPageError(
                code=error.code,
                retryable=error.retryable,
                exchange=exchange,
            ) from error
        return PublicMarketPageAcquisition(
            request=request,
            result=result,
            exchange=exchange,
        )

    def _fetch_https(self, request: PublicMarketPageRequest) -> PublicMarketRawHttpExchange:
        parsed = _validate_https_uri(request.source_uri)
        assert parsed.hostname is not None
        port = parsed.port or 443
        addresses = _resolve_public_addresses(parsed.hostname, port)
        request_path = parsed.path or "/"
        if parsed.query:
            request_path = f"{request_path}?{parsed.query}"
        last_connection_error: OSError | ssl.SSLError | None = None
        for family, socket_address in addresses:
            raw_socket: socket.socket | None = None
            connection: http.client.HTTPSConnection | None = None
            try:
                raw_socket = socket.socket(family, socket.SOCK_STREAM)
                raw_socket.settimeout(self._timeout_seconds)
                raw_socket.connect(socket_address)
                tls_socket = self._ssl_context.wrap_socket(
                    raw_socket,
                    server_hostname=parsed.hostname,
                )
                raw_socket = None
                connection = http.client.HTTPSConnection(
                    parsed.hostname,
                    port=port,
                    timeout=self._timeout_seconds,
                    context=self._ssl_context,
                )
                connection.sock = tls_socket
            except (OSError, ssl.SSLError) as error:
                last_connection_error = error
                if raw_socket is not None:
                    raw_socket.close()
                if connection is not None:
                    connection.close()
                continue
            assert connection is not None
            try:
                connection.request(
                    "GET",
                    request_path,
                    headers={
                        "Accept": "text/html,application/xhtml+xml",
                        "User-Agent": "TenderGuard-Public-Market/1.0",
                    },
                )
                response = connection.getresponse()
                payload = response.read(self._max_response_bytes + 1)
                if len(payload) > self._max_response_bytes:
                    raise PublicMarketPageError(code="MARKET_RESPONSE_TOO_LARGE")
                exchange = (
                    PublicMarketRawHttpExchange(
                        request_uri=request.source_uri,
                        response_body=payload,
                        status_code=response.status,
                        media_type=response.headers.get_content_type().lower(),
                        charset=response.headers.get_content_charset(),
                    )
                    if payload
                    else None
                )
                if response.status in _RETRYABLE_STATUS_CODES:
                    raise PublicMarketPageError(
                        code=f"MARKET_HTTP_{response.status}",
                        retryable=True,
                        exchange=exchange,
                    )
                if response.status != 200:
                    raise PublicMarketPageError(
                        code=(
                            "MARKET_REDIRECT_NOT_ALLOWED"
                            if 300 <= response.status < 400
                            else f"MARKET_HTTP_{response.status}"
                        ),
                        exchange=exchange,
                    )
                if exchange is None:
                    raise PublicMarketPageError(code="MARKET_RESPONSE_EMPTY")
                return exchange
            except PublicMarketPageError:
                raise
            except (OSError, TimeoutError, http.client.HTTPException) as error:
                raise PublicMarketPageError(
                    code="MARKET_TRANSPORT_FAILED",
                    retryable=True,
                ) from error
            finally:
                connection.close()
        raise PublicMarketPageError(
            code="MARKET_TRANSPORT_FAILED",
            retryable=True,
        ) from last_connection_error


def replay_public_market_page_acquisition(
    acquisition: PublicMarketPageAcquisition,
) -> PublicMarketPageResult:
    replayed = _parse_market_exchange(
        request=acquisition.request,
        exchange=acquisition.exchange,
        retrieved_at=acquisition.result.retrieved_at,
    )
    if replayed != acquisition.result:
        raise ValueError("Public market result does not reproduce from retained response")
    return replayed


def replay_public_market_page_failure(
    *,
    request: PublicMarketPageRequest,
    exchange: PublicMarketRawHttpExchange,
    expected_error_code: str,
) -> str:
    if exchange.request_uri != request.source_uri:
        raise ValueError("Public market failed response URI differs from its request")
    if exchange.status_code in _RETRYABLE_STATUS_CODES:
        reproduced_code = f"MARKET_HTTP_{exchange.status_code}"
    elif exchange.status_code != 200:
        reproduced_code = (
            "MARKET_REDIRECT_NOT_ALLOWED"
            if 300 <= exchange.status_code < 400
            else f"MARKET_HTTP_{exchange.status_code}"
        )
    else:
        try:
            _parse_market_exchange(
                request=request,
                exchange=exchange,
                retrieved_at=datetime.fromisoformat("2000-01-01T00:00:00+00:00"),
            )
        except PublicMarketPageError as error:
            reproduced_code = error.code
        else:
            raise ValueError("Retained public market response no longer reproduces a failure")
    if reproduced_code != expected_error_code:
        raise ValueError("Public market failure code does not reproduce from retained response")
    return reproduced_code


def _parse_market_exchange(
    *,
    request: PublicMarketPageRequest,
    exchange: PublicMarketRawHttpExchange,
    retrieved_at: datetime,
) -> PublicMarketPageResult:
    if exchange.request_uri != request.source_uri:
        raise ValueError("Public market retained response URI differs from its request")
    if exchange.status_code != 200:
        raise ValueError("Public market retained response is not successful")
    if exchange.media_type not in _ALLOWED_MEDIA_TYPES:
        raise PublicMarketPageError(code="MARKET_MEDIA_TYPE_INVALID")
    text, charset = _decode_html(exchange.response_body, exchange.charset)
    candidates, findings = _extract_structured_offers(text, request.source_uri)
    return PublicMarketPageResult(
        source_uri=request.source_uri,
        source_type=request.source_type,
        display_name=request.display_name,
        candidates=candidates,
        extraction_findings=findings,
        response_sha256=exchange.response_sha256,
        response_size_bytes=len(exchange.response_body),
        response_media_type=exchange.media_type,
        response_charset=charset,
        retrieved_at=retrieved_at,
        status="UNVERIFIED" if candidates else "BLOCKED",
        pricing_blockers=_page_blockers(candidates, findings),
    )


def _validate_https_uri(uri: str) -> SplitResult:
    if (
        not uri
        or uri != uri.strip()
        or "\\" in uri
        or any(character.isspace() for character in uri)
        or any(character in uri for character in "\r\n\x00")
    ):
        raise ValueError("Public market URI must be an exact single-line HTTPS address")
    parsed = urlsplit(uri)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.port not in (None, 443)
    ):
        raise ValueError("Public market URI must use HTTPS port 443 without credentials")
    host = parsed.hostname.lower().rstrip(".")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise ValueError("Public market URI cannot target a local hostname")
    return parsed


def _resolve_public_addresses(host: str, port: int) -> tuple[tuple[int, tuple[Any, ...]], ...]:
    try:
        resolved = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as error:
        raise PublicMarketPageError(
            code="MARKET_DNS_FAILED",
            retryable=True,
        ) from error
    addresses: list[tuple[int, tuple[Any, ...]]] = []
    seen: set[tuple[int, str]] = set()
    for family, socket_type, _protocol, _canonical_name, socket_address in resolved:
        if socket_type != socket.SOCK_STREAM or family not in (socket.AF_INET, socket.AF_INET6):
            continue
        address_literal = str(socket_address[0])
        try:
            address = ipaddress.ip_address(address_literal)
        except ValueError as error:
            raise PublicMarketPageError(code="MARKET_DNS_ADDRESS_INVALID") from error
        if not address.is_global:
            raise PublicMarketPageError(code="MARKET_PRIVATE_DESTINATION_BLOCKED")
        identity = (family, address_literal)
        if identity not in seen:
            seen.add(identity)
            addresses.append((family, socket_address))
    if not addresses:
        raise PublicMarketPageError(code="MARKET_DNS_NO_PUBLIC_ADDRESS")
    return tuple(addresses)


def _decode_html(payload: bytes, declared_charset: str | None) -> tuple[str, str]:
    charset = declared_charset
    if charset is None:
        match = _META_CHARSET_PATTERN.search(payload[:8192])
        charset = match.group(1).decode("ascii") if match is not None else "utf-8"
    try:
        normalized = codecs.lookup(charset).name
    except LookupError as error:
        raise PublicMarketPageError(code="MARKET_CHARSET_UNSUPPORTED") from error
    if normalized not in _ALLOWED_CHARSETS:
        raise PublicMarketPageError(code="MARKET_CHARSET_UNSUPPORTED")
    try:
        return payload.decode(normalized), normalized
    except UnicodeDecodeError as error:
        raise PublicMarketPageError(code="MARKET_HTML_DECODE_FAILED") from error


@dataclass
class _ProductDraft:
    path: str
    properties: dict[str, list[str]] = field(default_factory=dict)
    offers: list[_OfferDraft] = field(default_factory=list)


@dataclass
class _OfferDraft:
    path: str
    properties: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class _MicroScope:
    kind: Literal["PRODUCT", "OFFER", "OTHER"]
    target: _ProductDraft | _OfferDraft | None


@dataclass
class _TextCapture:
    target: _ProductDraft | _OfferDraft
    properties: tuple[str, ...]
    chunks: list[str] = field(default_factory=list)


@dataclass
class _ElementFrame:
    tag: str
    pushed_scope: bool
    captures: list[_TextCapture]


class _StructuredHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.json_ld_scripts: list[str] = []
        self.products: list[_ProductDraft] = []
        self.findings: list[str] = []
        self._json_ld_chunks: list[str] | None = None
        self._scopes: list[_MicroScope] = []
        self._frames: list[_ElementFrame] = []
        self._active_captures: list[_TextCapture] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.lower()
        attributes = {key.lower(): value for key, value in attrs}
        if normalized_tag == "script" and _is_json_ld_type(attributes.get("type")):
            if self._json_ld_chunks is not None:
                self.findings.append("MARKET_JSON_LD_NESTED_SCRIPT")
            self._json_ld_chunks = []

        has_scope = "itemscope" in attributes
        pushed_scope = False
        if has_scope:
            item_types = _item_tokens(attributes.get("itemtype"))
            if _schema_type_present(item_types, "Product"):
                product = _ProductDraft(path=f"microdata:product[{len(self.products)}]")
                self.products.append(product)
                self._scopes.append(_MicroScope(kind="PRODUCT", target=product))
            elif _schema_type_present(item_types, "Offer"):
                parent_product = self._nearest_product()
                offer = (
                    _OfferDraft(path=f"{parent_product.path}.offer[{len(parent_product.offers)}]")
                    if parent_product is not None
                    else None
                )
                if parent_product is not None and offer is not None:
                    parent_product.offers.append(offer)
                self._scopes.append(_MicroScope(kind="OFFER", target=offer))
            else:
                self._scopes.append(_MicroScope(kind="OTHER", target=None))
            pushed_scope = True

        captures: list[_TextCapture] = []
        if not has_scope:
            target = self._scopes[-1].target if self._scopes else None
            properties = _item_tokens(attributes.get("itemprop"))
            if target is not None and properties:
                immediate = _attribute_value(attributes)
                if immediate is not None:
                    _assign_properties(target, properties, immediate)
                elif normalized_tag not in _VOID_TAGS:
                    capture = _TextCapture(target=target, properties=properties)
                    captures.append(capture)
                    self._active_captures.append(capture)
        if normalized_tag not in _VOID_TAGS:
            self._frames.append(
                _ElementFrame(
                    tag=normalized_tag,
                    pushed_scope=pushed_scope,
                    captures=captures,
                )
            )
        elif pushed_scope:
            self._scopes.pop()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._json_ld_chunks is not None:
            self._json_ld_chunks.append(data)
        for capture in self._active_captures:
            capture.chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.lower()
        if normalized_tag == "script" and self._json_ld_chunks is not None:
            payload = "".join(self._json_ld_chunks).strip()
            if payload:
                if len(self.json_ld_scripts) >= 100:
                    self.findings.append("MARKET_JSON_LD_SCRIPT_LIMIT_REACHED")
                else:
                    self.json_ld_scripts.append(payload)
            self._json_ld_chunks = None
        frame_index = next(
            (
                index
                for index in range(len(self._frames) - 1, -1, -1)
                if self._frames[index].tag == normalized_tag
            ),
            None,
        )
        if frame_index is None:
            return
        while len(self._frames) > frame_index:
            self._close_frame(self._frames.pop())

    def close(self) -> None:
        super().close()
        while self._frames:
            self._close_frame(self._frames.pop())

    def _close_frame(self, frame: _ElementFrame) -> None:
        for capture in frame.captures:
            if capture in self._active_captures:
                self._active_captures.remove(capture)
            value = _normalize_text("".join(capture.chunks))
            if value is not None:
                _assign_properties(capture.target, capture.properties, value)
        if frame.pushed_scope and self._scopes:
            self._scopes.pop()

    def _nearest_product(self) -> _ProductDraft | None:
        for scope in reversed(self._scopes):
            if scope.kind == "PRODUCT" and isinstance(scope.target, _ProductDraft):
                return scope.target
        return None


def _extract_structured_offers(
    html: str,
    page_uri: str,
) -> tuple[tuple[PublicMarketOfferCandidate, ...], tuple[str, ...]]:
    parser = _StructuredHtmlParser()
    try:
        parser.feed(html)
        parser.close()
    except (RecursionError, ValueError) as error:
        raise PublicMarketPageError(code="MARKET_HTML_PARSE_FAILED") from error
    candidates: list[PublicMarketOfferCandidate] = []
    findings = list(parser.findings)
    for script_index, payload in enumerate(parser.json_ld_scripts):
        try:
            raw = json.loads(payload, parse_float=Decimal)
        except (json.JSONDecodeError, InvalidOperation, RecursionError):
            findings.append("MARKET_JSON_LD_INVALID")
            continue
        candidates.extend(
            _json_ld_candidates(
                raw,
                page_uri=page_uri,
                script_index=script_index,
                findings=findings,
            )
        )
    candidates.extend(
        _microdata_candidates(parser.products, page_uri=page_uri, findings=findings)
    )
    unique: list[PublicMarketOfferCandidate] = []
    identities: set[tuple[str, str, Decimal, str, str | None]] = set()
    for candidate in candidates:
        identity = (
            candidate.source_item_name,
            candidate.source_record_locator,
            candidate.amount,
            candidate.currency,
            candidate.availability_literal,
        )
        if identity in identities:
            continue
        identities.add(identity)
        unique.append(candidate)
    if len(unique) > 200:
        raise PublicMarketPageError(code="MARKET_OFFER_LIMIT_EXCEEDED")
    return tuple(unique), tuple(dict.fromkeys(findings))


def _json_ld_candidates(
    raw: Any,
    *,
    page_uri: str,
    script_index: int,
    findings: list[str],
) -> list[PublicMarketOfferCandidate]:
    products: list[tuple[str, dict[str, Any]]] = []
    stack: list[tuple[str, Any, int]] = [(f"jsonld:script[{script_index}]", raw, 0)]
    visited = 0
    while stack:
        path, node, depth = stack.pop()
        visited += 1
        if visited > 20_000 or depth > 50:
            raise PublicMarketPageError(code="MARKET_JSON_LD_COMPLEXITY_LIMIT")
        if isinstance(node, dict):
            if _schema_type_present(_json_type_tokens(node.get("@type")), "Product"):
                products.append((path, node))
            for key, value in reversed(tuple(node.items())):
                if isinstance(value, dict | list):
                    stack.append((f"{path}.{key}", value, depth + 1))
        elif isinstance(node, list):
            for index in range(len(node) - 1, -1, -1):
                value = node[index]
                if isinstance(value, dict | list):
                    stack.append((f"{path}[{index}]", value, depth + 1))
    candidates: list[PublicMarketOfferCandidate] = []
    for product_path, product in products:
        name = _single_json_string(product.get("name"))
        if name is None:
            findings.append("MARKET_JSON_LD_PRODUCT_NAME_MISSING")
            continue
        locator = _json_product_locator(product) or product_path
        brand = _json_brand(product.get("brand"))
        offers = product.get("offers")
        offer_items = offers if isinstance(offers, list) else [offers]
        structured_offers = [item for item in offer_items if isinstance(item, dict)]
        if len(structured_offers) > 1:
            discriminators = tuple(_json_offer_discriminator(item) for item in structured_offers)
            if any(item is None for item in discriminators) or len(discriminators) != len(
                set(discriminators)
            ):
                findings.append("MARKET_JSON_LD_MULTIPLE_OFFERS_AMBIGUOUS")
                continue
        for offer_index, offer in enumerate(offer_items):
            if not isinstance(offer, dict):
                continue
            offer_name = _single_json_string(offer.get("name"))
            offer_locator = _json_offer_locator(offer)
            candidate = _candidate_from_properties(
                extraction_method="JSON_LD",
                source_path=f"{product_path}.offers[{offer_index}]",
                source_item_name=offer_name or name,
                source_record_locator=(
                    f"{locator}|{offer_locator}"
                    if len(structured_offers) > 1 and offer_locator is not None
                    else locator
                ),
                brand_name=brand,
                properties=offer,
                page_uri=page_uri,
                findings=findings,
                json_values=True,
            )
            if candidate is not None:
                candidates.append(candidate)
    return candidates


def _microdata_candidates(
    products: Iterable[_ProductDraft],
    *,
    page_uri: str,
    findings: list[str],
) -> list[PublicMarketOfferCandidate]:
    candidates: list[PublicMarketOfferCandidate] = []
    for product in products:
        names = product.properties.get("name", [])
        if len(names) != 1:
            findings.append(
                "MARKET_MICRODATA_PRODUCT_NAME_MISSING"
                if not names
                else "MARKET_MICRODATA_PRODUCT_NAME_AMBIGUOUS"
            )
            continue
        locator = _microdata_product_locator(product) or product.path
        brands = product.properties.get("brand", [])
        brand = brands[0] if len(brands) == 1 else None
        if len(brands) > 1:
            findings.append("MARKET_MICRODATA_BRAND_AMBIGUOUS")
        if len(product.offers) > 1:
            discriminators = tuple(_microdata_offer_discriminator(item) for item in product.offers)
            if any(item is None for item in discriminators) or len(discriminators) != len(
                set(discriminators)
            ):
                findings.append("MARKET_MICRODATA_MULTIPLE_OFFERS_AMBIGUOUS")
                continue
        for offer in product.offers:
            offer_names = offer.properties.get("name", [])
            if len(offer_names) > 1:
                findings.append("MARKET_MICRODATA_OFFER_NAME_AMBIGUOUS")
                continue
            offer_locator = _microdata_offer_locator(offer)
            candidate = _candidate_from_properties(
                extraction_method="MICRODATA",
                source_path=offer.path,
                source_item_name=offer_names[0] if offer_names else names[0],
                source_record_locator=(
                    f"{locator}|{offer_locator}"
                    if len(product.offers) > 1 and offer_locator is not None
                    else locator
                ),
                brand_name=brand,
                properties=offer.properties,
                page_uri=page_uri,
                findings=findings,
                json_values=False,
            )
            if candidate is not None:
                candidates.append(candidate)
    return candidates


def _candidate_from_properties(
    *,
    extraction_method: Literal["JSON_LD", "MICRODATA"],
    source_path: str,
    source_item_name: str,
    source_record_locator: str,
    brand_name: str | None,
    properties: dict[str, Any],
    page_uri: str,
    findings: list[str],
    json_values: bool,
) -> PublicMarketOfferCandidate | None:
    prices = _property_strings(properties, "price", json_values=json_values)
    currencies = _property_strings(properties, "priceCurrency", json_values=json_values)
    if len(prices) != 1 or len(currencies) != 1:
        findings.append(
            "MARKET_STRUCTURED_PRICE_MISSING"
            if not prices or not currencies
            else "MARKET_STRUCTURED_PRICE_AMBIGUOUS"
        )
        return None
    price_literal = prices[0]
    currency = currencies[0]
    if not _PRICE_LITERAL_PATTERN.fullmatch(price_literal) or not _CURRENCY_PATTERN.fullmatch(
        currency
    ):
        findings.append("MARKET_STRUCTURED_PRICE_LITERAL_INVALID")
        return None
    amount = Decimal(price_literal)
    if not amount.is_finite() or amount <= 0:
        findings.append("MARKET_STRUCTURED_PRICE_NON_POSITIVE")
        return None
    availability = _single_optional_property(
        properties,
        "availability",
        json_values=json_values,
        findings=findings,
        ambiguous_code="MARKET_STRUCTURED_AVAILABILITY_AMBIGUOUS",
    )
    offer_uri_literal = _single_optional_property(
        properties,
        "url",
        json_values=json_values,
        findings=findings,
        ambiguous_code="MARKET_STRUCTURED_OFFER_URI_AMBIGUOUS",
    )
    offer_uri = urljoin(page_uri, offer_uri_literal) if offer_uri_literal else None
    if offer_uri is not None:
        try:
            _validate_https_uri(offer_uri)
        except ValueError:
            findings.append("MARKET_STRUCTURED_OFFER_URI_INVALID")
            offer_uri = None
    return PublicMarketOfferCandidate(
        extraction_method=extraction_method,
        source_path=source_path,
        source_item_name=source_item_name,
        source_record_locator=source_record_locator,
        brand_name=brand_name,
        amount=amount,
        amount_literal=price_literal,
        currency=currency,
        availability_literal=availability,
        offer_uri=offer_uri,
        price_valid_until_literal=_single_optional_property(
            properties,
            "priceValidUntil",
            json_values=json_values,
            findings=findings,
            ambiguous_code="MARKET_STRUCTURED_PRICE_VALIDITY_AMBIGUOUS",
        ),
        unit_code=_single_optional_property(
            properties,
            "unitCode",
            json_values=json_values,
            findings=findings,
            ambiguous_code="MARKET_STRUCTURED_UNIT_CODE_AMBIGUOUS",
        ),
        unit_text=_single_optional_property(
            properties,
            "unitText",
            json_values=json_values,
            findings=findings,
            ambiguous_code="MARKET_STRUCTURED_UNIT_TEXT_AMBIGUOUS",
        ),
    )


def _property_strings(
    properties: dict[str, Any],
    key: str,
    *,
    json_values: bool,
) -> list[str]:
    raw = properties.get(key)
    values = raw if isinstance(raw, list) else [raw]
    result: list[str] = []
    for value in values:
        normalized = _json_scalar_literal(value) if json_values else _normalize_text(value)
        if normalized is not None and normalized not in result:
            result.append(normalized)
    return result


def _single_optional_property(
    properties: dict[str, Any],
    key: str,
    *,
    json_values: bool,
    findings: list[str],
    ambiguous_code: str,
) -> str | None:
    values = _property_strings(properties, key, json_values=json_values)
    if len(values) > 1:
        findings.append(ambiguous_code)
        return None
    return values[0] if values else None


def _json_scalar_literal(value: Any) -> str | None:
    if isinstance(value, str):
        return _normalize_text(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def _single_json_string(value: Any) -> str | None:
    return _normalize_text(value) if isinstance(value, str) else None


def _json_brand(value: Any) -> str | None:
    if isinstance(value, str):
        return _normalize_text(value)
    if isinstance(value, dict):
        return _single_json_string(value.get("name"))
    return None


def _json_product_locator(product: dict[str, Any]) -> str | None:
    for key in ("sku", "productID", "mpn", "gtin14", "gtin13", "gtin12", "gtin8"):
        value = _json_scalar_literal(product.get(key))
        if value is not None:
            return f"{key}:{value}"
    return None


def _json_offer_locator(offer: dict[str, Any]) -> str | None:
    for key in ("sku", "productID", "mpn", "gtin14", "gtin13", "gtin12", "gtin8", "url"):
        value = _json_scalar_literal(offer.get(key))
        if value is not None:
            return f"offer-{key}:{value}"
    return None


def _json_offer_discriminator(offer: dict[str, Any]) -> str | None:
    return _json_offer_locator(offer) or (
        f"offer-name:{name}" if (name := _single_json_string(offer.get("name"))) else None
    )


def _microdata_product_locator(product: _ProductDraft) -> str | None:
    for key in ("sku", "productID", "mpn", "gtin14", "gtin13", "gtin12", "gtin8"):
        values = product.properties.get(key, [])
        if len(values) == 1:
            return f"{key}:{values[0]}"
    return None


def _microdata_offer_locator(offer: _OfferDraft) -> str | None:
    for key in ("sku", "productID", "mpn", "gtin14", "gtin13", "gtin12", "gtin8", "url"):
        values = offer.properties.get(key, [])
        if len(values) == 1:
            return f"offer-{key}:{values[0]}"
    return None


def _microdata_offer_discriminator(offer: _OfferDraft) -> str | None:
    locator = _microdata_offer_locator(offer)
    if locator is not None:
        return locator
    names = offer.properties.get("name", [])
    return f"offer-name:{names[0]}" if len(names) == 1 else None


def _assign_properties(
    target: _ProductDraft | _OfferDraft,
    properties: tuple[str, ...],
    value: str,
) -> None:
    normalized = _normalize_text(value)
    if normalized is None:
        return
    for property_name in properties:
        values = target.properties.setdefault(property_name, [])
        if normalized not in values:
            values.append(normalized)


def _attribute_value(attributes: dict[str, str | None]) -> str | None:
    for key in ("content", "href", "src", "data", "value", "datetime"):
        value = attributes.get(key)
        if value is not None:
            return _normalize_text(value)
    return None


def _normalize_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > 5000 or any(
        character in normalized for character in "\r\n\x00"
    ):
        return None
    return normalized


def _item_tokens(value: str | None) -> tuple[str, ...]:
    return tuple(value.split()) if value else ()


def _json_type_tokens(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
    return ()


def _schema_type_present(values: Iterable[str], expected: str) -> bool:
    return any(value == expected or value.rstrip("/").endswith(f"/{expected}") for value in values)


def _is_json_ld_type(value: str | None) -> bool:
    return bool(value and value.split(";", 1)[0].strip().lower() == "application/ld+json")


def _page_blockers(
    candidates: tuple[PublicMarketOfferCandidate, ...],
    findings: tuple[str, ...],
) -> tuple[str, ...]:
    blockers = [
        "DIAGNOSTIC_MARKET_SOURCE_NOT_GOVERNED",
        "TECHNICAL_EQUIVALENCE_NOT_ESTABLISHED",
        "MARKET_UNIT_MAPPING_REQUIRED",
        "VAT_BASIS_UNKNOWN",
        "DELIVERY_BASIS_UNKNOWN",
        "UNLOADING_BASIS_UNKNOWN",
        "PAYMENT_TERMS_UNKNOWN",
        "PRICE_VALIDITY_NOT_ESTABLISHED",
        "PRICE_NORMALIZATION_REQUIRED",
        "INDEPENDENT_VALIDATION_REQUIRED",
        "BID_RELEASE_NOT_APPROVED",
    ]
    if not candidates:
        blockers.insert(0, "STRUCTURED_MARKET_OFFER_NOT_FOUND")
    if findings:
        blockers.insert(0, "MARKET_STRUCTURED_DATA_FINDINGS_PRESENT")
    return tuple(blockers)
