from __future__ import annotations

import hashlib
import http.client
import json
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode

from pydantic import Field, StrictInt, StrictStr, field_validator, model_validator

from tenderguard.domain.common import ensure_utc, utc_now
from tenderguard.domain.enums import VerificationStatus
from tenderguard.domain.models import DomainModel

FGIS_CS_HOST = "fgiscs.minstroyrf.ru"
FGIS_CS_ORIGIN = f"https://{FGIS_CS_HOST}"
FGIS_CS_PUBLIC_SCHEMA_VERSION = "fgiscs-public-api/v1"

_RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
_SUBJECTS_PATH = "/api/EstimatedPrice/CountrySubjects"
_PRICE_ZONES_PATH = "/api/EstimatedPrice/PriceZones"
_PERIODS_PATH = "/api/EstimatedPrice/Periods"
_MATERIAL_SEARCH_PATH = "/api/EstimatedPrice/BuildingResources/Search/Materials"
_KSR_TIP_SEARCH_PATH = "/api/Ksr/TipSearch"

FgisCsFetch = Callable[
    [str, dict[str, str | int]],
    tuple[Any, bytes, str],
]


@dataclass(frozen=True)
class FgisCsRawHttpExchange:
    request_uri: str
    response_body: bytes

    def __post_init__(self) -> None:
        if not self.request_uri.startswith(f"{FGIS_CS_ORIGIN}/api/"):
            raise ValueError("FGIS CS captured request URI is outside the approved origin")
        if not self.response_body:
            raise ValueError("FGIS CS captured response body is empty")

    @property
    def response_sha256(self) -> str:
        return hashlib.sha256(self.response_body).hexdigest()


@dataclass(frozen=True)
class FgisCsMaterialAcquisition:
    request: FgisCsMaterialLookupRequest
    result: FgisCsMaterialLookupResult
    exchanges: tuple[FgisCsRawHttpExchange, ...]

    def __post_init__(self) -> None:
        if len(self.exchanges) != 4:
            raise ValueError("FGIS CS material acquisition must retain exactly four responses")


class FgisCsPublicApiError(RuntimeError):
    def __init__(self, *, code: str, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(code)


class FgisCsReference(DomainModel):
    id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=500)

    @field_validator("name")
    @classmethod
    def name_is_literal_and_bounded(cls, value: str) -> str:
        if value != value.strip() or any(character in value for character in "\r\n\x00"):
            raise ValueError("FGIS CS reference name is not a literal single-line value")
        return value


class FgisCsMaterialLookupRequest(DomainModel):
    subject_name: str = Field(min_length=1, max_length=500)
    price_zone_name: str | None = Field(default=None, min_length=1, max_length=500)
    period_name: str = Field(min_length=1, max_length=200)
    resource_code: str = Field(min_length=1, max_length=200)

    @field_validator("subject_name", "price_zone_name", "period_name", "resource_code")
    @classmethod
    def lookup_values_are_exact_literals(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value != value.strip() or any(character in value for character in "\r\n\x00"):
            raise ValueError("FGIS CS lookup values must be exact single-line literals")
        return value


class FgisCsMaterialPrice(DomainModel):
    source_record_id: str = Field(min_length=1, max_length=100)
    resource_code: str = Field(min_length=1, max_length=200)
    source_item_name: str = Field(min_length=1, max_length=4000)
    unit: str = Field(min_length=1, max_length=100)
    aggregated_price: Decimal = Field(gt=0, max_digits=38, decimal_places=12)
    estimated_price: Decimal = Field(gt=0, max_digits=38, decimal_places=12)
    distance_price: Decimal = Field(ge=0, max_digits=38, decimal_places=12)
    procure_storage_cost_percent: Decimal = Field(
        ge=0,
        max_digits=38,
        decimal_places=12,
    )
    source_amount_literals: dict[str, str]
    ksr_type: int = Field(gt=0)

    @model_validator(mode="after")
    def amount_literals_reproduce_values(self) -> FgisCsMaterialPrice:
        expected = {
            "aggregatedPrice": self.aggregated_price,
            "estimatedPrice": self.estimated_price,
            "distancePrice": self.distance_price,
            "procureStorageCostPercent": self.procure_storage_cost_percent,
        }
        if set(self.source_amount_literals) != set(expected):
            raise ValueError("FGIS CS amount literals are incomplete")
        for key, amount in expected.items():
            try:
                reproduced = Decimal(self.source_amount_literals[key])
            except InvalidOperation as error:
                raise ValueError("FGIS CS amount literal is not decimal") from error
            if reproduced != amount:
                raise ValueError("FGIS CS amount literal does not reproduce its value")
        return self


class FgisCsMaterialLookupResult(DomainModel):
    schema_version: str
    subject: FgisCsReference
    price_zone: FgisCsReference
    period: FgisCsReference
    requested_resource_code: str
    price: FgisCsMaterialPrice | None
    public_page_uri: str = Field(pattern=r"^https://")
    api_request_uri: str = Field(pattern=r"^https://")
    response_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    retrieved_at: datetime
    ready_for_pricing: bool = False
    pricing_blockers: tuple[str, ...] = (
        "APPROVED_FGIS_MAPPING_REQUIRED",
        "COMMERCIAL_BASIS_NOT_ESTABLISHED",
    )

    @field_validator("retrieved_at")
    @classmethod
    def retrieved_at_is_aware(cls, value: datetime) -> datetime:
        normalized = ensure_utc(value)
        if normalized is None or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("FGIS CS retrieval timestamp must include a timezone")
        return normalized

    @model_validator(mode="after")
    def result_is_fail_closed(self) -> FgisCsMaterialLookupResult:
        if self.schema_version != FGIS_CS_PUBLIC_SCHEMA_VERSION:
            raise ValueError("Unsupported FGIS CS public response schema")
        if self.ready_for_pricing or not self.pricing_blockers:
            raise ValueError("Raw FGIS CS public data cannot be released as a normalized price")
        if self.price is not None and self.price.resource_code != self.requested_resource_code:
            raise ValueError("FGIS CS result code does not match the requested code")
        return self


class FgisCsKsrCandidate(DomainModel):
    source_record_id: str = Field(min_length=1, max_length=100)
    resource_code: str = Field(min_length=1, max_length=200)
    source_item_name: str = Field(min_length=1, max_length=4000)
    unit: str = Field(min_length=1, max_length=100)
    status: VerificationStatus = VerificationStatus.UNVERIFIED

    @model_validator(mode="after")
    def candidate_is_never_verified_by_retrieval(self) -> FgisCsKsrCandidate:
        if self.status is not VerificationStatus.UNVERIFIED:
            raise ValueError("FGIS CS KSR retrieval cannot verify a nomenclature match")
        return self


class FgisCsKsrSearchResult(DomainModel):
    schema_version: str
    query: str = Field(min_length=1, max_length=1000)
    candidates: tuple[FgisCsKsrCandidate, ...]
    api_request_uri: str = Field(pattern=r"^https://")
    response_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    retrieved_at: datetime
    status: VerificationStatus = VerificationStatus.UNVERIFIED
    retrieval_notice: str = (
        "FGIS CS ordering is retrieval only; critical attributes and units "
        "must be compared under an approved nomenclature policy."
    )

    @field_validator("retrieved_at")
    @classmethod
    def retrieved_at_is_aware(cls, value: datetime) -> datetime:
        normalized = ensure_utc(value)
        if normalized is None or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("FGIS CS retrieval timestamp must include a timezone")
        return normalized

    @model_validator(mode="after")
    def result_is_unverified(self) -> FgisCsKsrSearchResult:
        if self.schema_version != FGIS_CS_PUBLIC_SCHEMA_VERSION:
            raise ValueError("Unsupported FGIS CS public response schema")
        if self.status is not VerificationStatus.UNVERIFIED:
            raise ValueError("FGIS CS KSR search result must remain unverified")
        return self


class _FgisCsKsrTip(DomainModel):
    id: StrictInt
    title: StrictStr
    description: StrictStr
    name: StrictStr
    unitName: StrictStr


class _FgisCsMaterialItem(DomainModel):
    code: str
    name: str
    unitName: str
    aggregatedPrice: StrictStr
    estimatedPrice: StrictStr
    distancePrice: StrictStr
    procureStorageCostPercent: StrictStr
    ksrType: int
    id: int

    @field_validator(
        "aggregatedPrice",
        "estimatedPrice",
        "distancePrice",
        "procureStorageCostPercent",
    )
    @classmethod
    def amounts_are_decimal_strings(cls, value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("FGIS CS financial values must remain source strings")
        try:
            amount = Decimal(value)
        except InvalidOperation as error:
            raise ValueError("FGIS CS financial value is not decimal") from error
        if not amount.is_finite():
            raise ValueError("FGIS CS financial value must be finite")
        return value


class _FgisCsMaterialGroup(DomainModel):
    id: int
    ksrType: int
    items: tuple[_FgisCsMaterialItem, ...]


class _FgisCsMaterialSearchEnvelope(DomainModel):
    items: tuple[_FgisCsMaterialGroup, ...]
    total: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def total_matches_flattened_items(self) -> _FgisCsMaterialSearchEnvelope:
        flattened_count = sum(len(group.items) for group in self.items)
        if self.total is not None and self.total != flattened_count:
            raise ValueError("FGIS CS response total does not match returned items")
        return self


class FgisCsPublicApi:
    """Bounded read-only client for the public FGIS CS portal endpoints.

    This client retrieves source records only. It deliberately does not map a
    row to a BoQ item, infer VAT, normalize the commercial basis, or approve a
    bid price.
    """

    def __init__(
        self,
        *,
        timeout_seconds: int = 30,
        max_response_bytes: int = 2_000_000,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("FGIS CS timeout must be positive")
        if max_response_bytes <= 0:
            raise ValueError("FGIS CS response limit must be positive")
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._ssl_context = ssl_context or ssl.create_default_context()

    def lookup_material(
        self,
        request: FgisCsMaterialLookupRequest,
        *,
        retrieved_at: datetime | None = None,
    ) -> FgisCsMaterialLookupResult:
        return self.acquire_material(request, retrieved_at=retrieved_at).result

    def acquire_material(
        self,
        request: FgisCsMaterialLookupRequest,
        *,
        retrieved_at: datetime | None = None,
    ) -> FgisCsMaterialAcquisition:
        exchanges: list[FgisCsRawHttpExchange] = []

        def captured_fetch(
            path: str,
            parameters: dict[str, str | int],
        ) -> tuple[Any, bytes, str]:
            raw, payload, request_uri = self._get_json(path, parameters)
            exchanges.append(
                FgisCsRawHttpExchange(
                    request_uri=request_uri,
                    response_body=payload,
                )
            )
            return raw, payload, request_uri

        result = self._lookup_material_with_fetch(
            request,
            fetch=captured_fetch,
            retrieved_at=retrieved_at or utc_now(),
        )
        return FgisCsMaterialAcquisition(
            request=request,
            result=result,
            exchanges=tuple(exchanges),
        )

    def _lookup_material_with_fetch(
        self,
        request: FgisCsMaterialLookupRequest,
        *,
        fetch: FgisCsFetch,
        retrieved_at: datetime,
    ) -> FgisCsMaterialLookupResult:
        subject = self._resolve_exact(
            self._get_references_with_fetch(_SUBJECTS_PATH, {}, fetch=fetch),
            request.subject_name,
            label="SUBJECT",
        )
        zones = self._get_references_with_fetch(
            _PRICE_ZONES_PATH,
            {"subjectId": subject.id},
            fetch=fetch,
        )
        if request.price_zone_name is None:
            if len(zones) != 1:
                raise FgisCsPublicApiError(code="FGIS_PRICE_ZONE_REQUIRED")
            price_zone = zones[0]
        else:
            price_zone = self._resolve_exact(
                zones,
                request.price_zone_name,
                label="PRICE_ZONE",
            )
        period = self._resolve_exact(
            self._get_references_with_fetch(
                _PERIODS_PATH,
                {"priceZoneId": price_zone.id},
                fetch=fetch,
            ),
            request.period_name,
            label="PERIOD",
        )
        search_parameters: dict[str, str | int] = {
            "countrySubjectId": subject.id,
            "priceZoneId": price_zone.id,
            "periodId": period.id,
            "search": request.resource_code,
            "refresh": "{}",
            "materials": "true",
            "value": request.resource_code,
            "page": 1,
            "take": 25,
            "sort": "{}",
        }
        raw, payload, request_uri = fetch(
            _MATERIAL_SEARCH_PATH,
            search_parameters,
        )
        try:
            envelope = _FgisCsMaterialSearchEnvelope.model_validate(raw)
        except ValueError as error:
            raise FgisCsPublicApiError(code="FGIS_PRICE_SCHEMA_INVALID") from error
        items = tuple(item for group in envelope.items for item in group.items)
        non_exact_codes = sorted(
            {item.code for item in items if item.code != request.resource_code}
        )
        if non_exact_codes:
            raise FgisCsPublicApiError(code="FGIS_NON_EXACT_SEARCH_RESULT")
        if len(items) > 1:
            raise FgisCsPublicApiError(code="FGIS_AMBIGUOUS_EXACT_PRICE")
        price = self._material_price(items[0]) if items else None
        return FgisCsMaterialLookupResult(
            schema_version=FGIS_CS_PUBLIC_SCHEMA_VERSION,
            subject=subject,
            price_zone=price_zone,
            period=period,
            requested_resource_code=request.resource_code,
            price=price,
            public_page_uri=f"{FGIS_CS_ORIGIN}/prices?subjectId={subject.id}",
            api_request_uri=request_uri,
            response_sha256=hashlib.sha256(payload).hexdigest(),
            retrieved_at=retrieved_at,
        )

    def list_subjects(self) -> tuple[FgisCsReference, ...]:
        return self._get_references(_SUBJECTS_PATH, {})

    def search_ksr(
        self,
        query: str,
        *,
        retrieved_at: datetime | None = None,
    ) -> FgisCsKsrSearchResult:
        if (
            not query
            or query != query.strip()
            or len(query) > 1000
            or any(character in query for character in "\r\n\x00")
        ):
            raise ValueError("FGIS CS KSR query must be an exact single-line literal")
        raw, payload, request_uri = self._get_json(
            _KSR_TIP_SEARCH_PATH,
            {"value": query},
        )
        if not isinstance(raw, list):
            raise FgisCsPublicApiError(code="FGIS_KSR_SCHEMA_INVALID")
        try:
            tips = tuple(_FgisCsKsrTip.model_validate(item) for item in raw)
            candidates = tuple(
                FgisCsKsrCandidate(
                    source_record_id=str(item.id),
                    resource_code=item.title,
                    source_item_name=item.name,
                    unit=item.unitName,
                )
                for item in tips
            )
        except ValueError as error:
            raise FgisCsPublicApiError(code="FGIS_KSR_SCHEMA_INVALID") from error
        identities = tuple((item.source_record_id, item.resource_code) for item in candidates)
        if len(identities) != len(set(identities)):
            raise FgisCsPublicApiError(code="FGIS_KSR_DUPLICATE_CANDIDATE")
        return FgisCsKsrSearchResult(
            schema_version=FGIS_CS_PUBLIC_SCHEMA_VERSION,
            query=query,
            candidates=candidates,
            api_request_uri=request_uri,
            response_sha256=hashlib.sha256(payload).hexdigest(),
            retrieved_at=retrieved_at or utc_now(),
        )

    def list_price_zones(self, subject_id: int) -> tuple[FgisCsReference, ...]:
        if subject_id <= 0:
            raise ValueError("FGIS CS subject ID must be positive")
        return self._get_references(_PRICE_ZONES_PATH, {"subjectId": subject_id})

    def list_periods(self, price_zone_id: int) -> tuple[FgisCsReference, ...]:
        if price_zone_id <= 0:
            raise ValueError("FGIS CS price-zone ID must be positive")
        return self._get_references(_PERIODS_PATH, {"priceZoneId": price_zone_id})

    def _get_references(
        self,
        path: str,
        parameters: dict[str, str | int],
    ) -> tuple[FgisCsReference, ...]:
        return self._get_references_with_fetch(path, parameters, fetch=self._get_json)

    @staticmethod
    def _get_references_with_fetch(
        path: str,
        parameters: dict[str, str | int],
        *,
        fetch: FgisCsFetch,
    ) -> tuple[FgisCsReference, ...]:
        raw, _, _ = fetch(path, parameters)
        if not isinstance(raw, list):
            raise FgisCsPublicApiError(code="FGIS_REFERENCE_SCHEMA_INVALID")
        try:
            return tuple(FgisCsReference.model_validate(item) for item in raw)
        except ValueError as error:
            raise FgisCsPublicApiError(code="FGIS_REFERENCE_SCHEMA_INVALID") from error

    def _get_json(
        self,
        path: str,
        parameters: dict[str, str | int],
    ) -> tuple[Any, bytes, str]:
        if not path.startswith("/api/") or "?" in path or "#" in path:
            raise ValueError("FGIS CS API path is invalid")
        query = urlencode(parameters)
        request_path = f"{path}?{query}" if query else path
        connection = http.client.HTTPSConnection(
            FGIS_CS_HOST,
            port=443,
            timeout=self._timeout_seconds,
            context=self._ssl_context,
        )
        try:
            connection.request(
                "GET",
                request_path,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "TenderGuard-FGIS-CS/1.0",
                },
            )
            response = connection.getresponse()
            payload = response.read(self._max_response_bytes + 1)
            if len(payload) > self._max_response_bytes:
                raise FgisCsPublicApiError(code="FGIS_RESPONSE_TOO_LARGE")
            if response.status in _RETRYABLE_STATUS_CODES:
                raise FgisCsPublicApiError(
                    code=f"FGIS_HTTP_{response.status}",
                    retryable=True,
                )
            if response.status != 200:
                raise FgisCsPublicApiError(code=f"FGIS_HTTP_{response.status}")
            media_type = response.headers.get_content_type().lower()
            if media_type != "application/json":
                raise FgisCsPublicApiError(code="FGIS_MEDIA_TYPE_INVALID")
            try:
                raw = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise FgisCsPublicApiError(code="FGIS_JSON_INVALID") from error
            return raw, payload, f"{FGIS_CS_ORIGIN}{request_path}"
        except FgisCsPublicApiError:
            raise
        except (OSError, TimeoutError, http.client.HTTPException) as error:
            raise FgisCsPublicApiError(
                code="FGIS_TRANSPORT_FAILED",
                retryable=True,
            ) from error
        finally:
            connection.close()

    @staticmethod
    def _resolve_exact(
        references: tuple[FgisCsReference, ...],
        expected_name: str,
        *,
        label: str,
    ) -> FgisCsReference:
        matches = tuple(item for item in references if item.name == expected_name)
        if not matches:
            raise FgisCsPublicApiError(code=f"FGIS_{label}_NOT_FOUND")
        if len(matches) != 1:
            raise FgisCsPublicApiError(code=f"FGIS_{label}_AMBIGUOUS")
        return matches[0]

    @staticmethod
    def _material_price(item: _FgisCsMaterialItem) -> FgisCsMaterialPrice:
        literals = {
            "aggregatedPrice": item.aggregatedPrice,
            "estimatedPrice": item.estimatedPrice,
            "distancePrice": item.distancePrice,
            "procureStorageCostPercent": item.procureStorageCostPercent,
        }
        try:
            return FgisCsMaterialPrice(
                source_record_id=str(item.id),
                resource_code=item.code,
                source_item_name=item.name,
                unit=item.unitName,
                aggregated_price=Decimal(item.aggregatedPrice),
                estimated_price=Decimal(item.estimatedPrice),
                distance_price=Decimal(item.distancePrice),
                procure_storage_cost_percent=Decimal(item.procureStorageCostPercent),
                source_amount_literals=literals,
                ksr_type=item.ksrType,
            )
        except (InvalidOperation, ValueError) as error:
            raise FgisCsPublicApiError(code="FGIS_PRICE_SCHEMA_INVALID") from error


def replay_fgiscs_material_acquisition(
    acquisition: FgisCsMaterialAcquisition,
) -> FgisCsMaterialLookupResult:
    pending = iter(acquisition.exchanges)

    def replay_fetch(
        path: str,
        parameters: dict[str, str | int],
    ) -> tuple[Any, bytes, str]:
        try:
            exchange = next(pending)
        except StopIteration as error:
            raise ValueError("FGIS CS acquisition response journal is incomplete") from error
        query = urlencode(parameters)
        request_path = f"{path}?{query}" if query else path
        expected_uri = f"{FGIS_CS_ORIGIN}{request_path}"
        if exchange.request_uri != expected_uri:
            raise ValueError("FGIS CS acquisition request journal does not reproduce")
        try:
            raw = json.loads(exchange.response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("FGIS CS acquisition contains invalid retained JSON") from error
        return raw, exchange.response_body, exchange.request_uri

    replayed = FgisCsPublicApi()._lookup_material_with_fetch(
        acquisition.request,
        fetch=replay_fetch,
        retrieved_at=acquisition.result.retrieved_at,
    )
    try:
        next(pending)
    except StopIteration:
        pass
    else:
        raise ValueError("FGIS CS acquisition response journal has unexpected entries")
    if replayed != acquisition.result:
        raise ValueError("FGIS CS retained result does not reproduce from raw responses")
    return replayed
