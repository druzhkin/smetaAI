from __future__ import annotations

import hashlib
import json
from collections import deque
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from tenderguard.application.fgiscs_diagnostic import (
    PreparedFgisCsDiagnosticMaterialPackage,
    prepare_fgiscs_diagnostic_material_package,
    verify_fgiscs_diagnostic_material_package,
)
from tenderguard.integrations.fgiscs_public import (
    FGIS_CS_PUBLIC_SCHEMA_VERSION,
    FgisCsKsrSearchAcquisition,
    FgisCsMaterialHistoryAcquisition,
    FgisCsMaterialHistoryRequest,
    FgisCsMaterialLookupRequest,
    FgisCsPublicApi,
    FgisCsPublicApiError,
    FgisCsRawHttpExchange,
    replay_fgiscs_ksr_search_acquisition,
    replay_fgiscs_material_acquisition,
    replay_fgiscs_material_history_acquisition,
)

_PERIOD_NAME = "2 квартал 2026 \u0433."


class _Headers:
    def __init__(self, media_type: str = "application/json") -> None:
        self.media_type = media_type

    def get_content_type(self) -> str:
        return self.media_type


class _Response:
    def __init__(
        self,
        payload: bytes,
        *,
        status: int = 200,
        media_type: str = "application/json",
    ) -> None:
        self.payload = payload
        self.status = status
        self.headers = _Headers(media_type)

    def read(self, _: int) -> bytes:
        return self.payload


def _json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def _install_responses(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[_Response],
) -> list[tuple[str, str, dict[str, str]]]:
    pending = deque(responses)
    requests: list[tuple[str, str, dict[str, str]]] = []

    class _Connection:
        def request(
            self,
            method: str,
            path: str,
            *,
            headers: dict[str, str],
        ) -> None:
            requests.append((method, path, headers))

        def getresponse(self) -> _Response:
            return pending.popleft()

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        "tenderguard.integrations.fgiscs_public.http.client.HTTPSConnection",
        lambda *args, **kwargs: _Connection(),
    )
    return requests


def _metadata_responses() -> list[_Response]:
    return [
        _Response(_json([{"id": 331, "name": "Московская область"}])),
        _Response(_json([{"id": 127, "name": "Московская область"}])),
        _Response(_json([{"id": 426, "name": _PERIOD_NAME}])),
    ]


def _price_payload(*, code: str = "02.3.01.02-1102") -> dict[str, Any]:
    return {
        "items": [
            {
                "id": 1,
                "ksrType": 1,
                "items": [
                    {
                        "code": code,
                        "name": ("Песок природный для строительных работ I класс, мелкий"),
                        "unitName": "м3",
                        "aggregatedPrice": "409.89",
                        "estimatedPrice": "1054.48",
                        "distancePrice": "623.91",
                        "procureStorageCostPercent": "2.00",
                        "ksrType": 1,
                        "id": 3655581,
                    }
                ],
            }
        ],
        "total": 1,
    }


def _request() -> FgisCsMaterialLookupRequest:
    return FgisCsMaterialLookupRequest(
        subject_name="Московская область",
        price_zone_name=None,
        period_name=_PERIOD_NAME,
        resource_code="02.3.01.02-1102",
    )


def test_lookup_material_preserves_exact_official_record_and_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    price_payload = _json(_price_payload())
    requests = _install_responses(
        monkeypatch,
        [*_metadata_responses(), _Response(price_payload)],
    )
    retrieved_at = datetime(2026, 7, 31, 9, 30, tzinfo=UTC)

    result = FgisCsPublicApi().lookup_material(
        _request(),
        retrieved_at=retrieved_at,
    )

    assert result.schema_version == FGIS_CS_PUBLIC_SCHEMA_VERSION
    assert result.subject.id == 331
    assert result.price_zone.id == 127
    assert result.period.id == 426
    assert result.price is not None
    assert result.price.source_record_id == "3655581"
    assert result.price.resource_code == "02.3.01.02-1102"
    assert result.price.unit == "м3"
    assert result.price.aggregated_price == Decimal("409.89")
    assert result.price.estimated_price == Decimal("1054.48")
    assert result.price.distance_price == Decimal("623.91")
    assert result.price.procure_storage_cost_percent == Decimal("2.00")
    assert result.price.source_amount_literals == {
        "aggregatedPrice": "409.89",
        "estimatedPrice": "1054.48",
        "distancePrice": "623.91",
        "procureStorageCostPercent": "2.00",
    }
    assert result.response_sha256 == hashlib.sha256(price_payload).hexdigest()
    assert result.retrieved_at == retrieved_at
    assert result.public_page_uri == ("https://fgiscs.minstroyrf.ru/prices?subjectId=331")
    assert not result.ready_for_pricing
    assert "APPROVED_FGIS_MAPPING_REQUIRED" in result.pricing_blockers

    assert [method for method, _, _ in requests] == ["GET"] * 4
    search_path = requests[-1][1]
    parsed = urlsplit(search_path)
    assert parsed.path.endswith("/BuildingResources/Search/Materials")
    assert parse_qs(parsed.query) == {
        "countrySubjectId": ["331"],
        "priceZoneId": ["127"],
        "periodId": ["426"],
        "search": ["02.3.01.02-1102"],
        "refresh": ["{}"],
        "materials": ["true"],
        "value": ["02.3.01.02-1102"],
        "page": ["1"],
        "take": ["25"],
        "sort": ["{}"],
    }
    assert requests[-1][2] == {
        "Accept": "application/json",
        "User-Agent": "TenderGuard-FGIS-CS/1.0",
    }


def test_material_acquisition_retains_and_replays_every_raw_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_responses(
        monkeypatch,
        [*_metadata_responses(), _Response(_json(_price_payload()))],
    )
    acquired = FgisCsPublicApi().acquire_material(
        _request(),
        retrieved_at=datetime(2026, 7, 31, 9, 30, tzinfo=UTC),
    )

    assert len(acquired.exchanges) == 4
    assert all(exchange.response_sha256 for exchange in acquired.exchanges)
    assert replay_fgiscs_material_acquisition(acquired) == acquired.result

    prepared = prepare_fgiscs_diagnostic_material_package(acquired)
    assert prepared.manifest.status == "UNVERIFIED"
    assert not prepared.manifest.ready_for_pricing
    assert [item.sequence for item in prepared.manifest.raw_responses] == [1, 2, 3, 4]
    assert prepared.manifest.raw_responses[-1].sha256 == acquired.result.response_sha256
    assert tuple(item[0] for item in prepared.raw_files) == tuple(
        reference.file_name for reference in prepared.manifest.raw_responses
    )
    assert (
        verify_fgiscs_diagnostic_material_package(
            prepared.manifest,
            prepared.raw_files,
        )
        == prepared.manifest
    )
    corrupted = (
        *prepared.raw_files[:-1],
        (prepared.raw_files[-1][0], prepared.raw_files[-1][1] + b" "),
    )
    with pytest.raises(ValueError, match="raw response differs"):
        PreparedFgisCsDiagnosticMaterialPackage(
            manifest=prepared.manifest,
            raw_files=corrupted,
        )

    tampered = FgisCsRawHttpExchange(
        request_uri=acquired.exchanges[-1].request_uri,
        response_body=_json(_price_payload(code="02.3.01.02-9999")),
    )
    altered = acquired.__class__(
        request=acquired.request,
        result=acquired.result,
        exchanges=(*acquired.exchanges[:-1], tampered),
    )
    with pytest.raises(FgisCsPublicApiError) as mismatch:
        replay_fgiscs_material_acquisition(altered)
    assert mismatch.value.code == "FGIS_NON_EXACT_SEARCH_RESULT"

    failed_exchange = FgisCsRawHttpExchange(
        request_uri=acquired.exchanges[-1].request_uri,
        response_body=acquired.exchanges[-1].response_body,
        status_code=422,
    )
    with pytest.raises(ValueError, match="contains a failed HTTP response"):
        acquired.__class__(
            request=acquired.request,
            result=acquired.result,
            exchanges=(*acquired.exchanges[:-1], failed_exchange),
        )


def test_material_history_reuses_metadata_and_replays_complete_grid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    older_period = "1 квартал 2026 г."  # noqa: RUF001
    code_one = "02.3.01.02-1102"
    code_two = "01.7.06.08-0008"
    requests = _install_responses(
        monkeypatch,
        [
            _Response(_json([{"id": 331, "name": "Московская область"}])),
            _Response(_json([{"id": 127, "name": "Московская область"}])),
            _Response(
                _json(
                    [
                        {"id": 426, "name": _PERIOD_NAME},
                        {"id": 425, "name": older_period},
                    ]
                )
            ),
            _Response(_json(_price_payload(code=code_one))),
            _Response(_json({"items": []})),
            _Response(_json({"items": []})),
            _Response(_json(_price_payload(code=code_two))),
        ],
    )
    request = FgisCsMaterialHistoryRequest(
        subject_name="Московская область",
        price_zone_name=None,
        resource_codes=(code_one, code_two),
    )

    acquired = FgisCsPublicApi().acquire_material_history(
        request,
        retrieved_at=datetime(2026, 8, 3, 15, 0, tzinfo=UTC),
    )

    assert len(requests) == 7
    assert len(acquired.exchanges) == 7
    assert [item.requested_resource_code for item in acquired.result.observations] == [
        code_one,
        code_one,
        code_two,
        code_two,
    ]
    assert [item.period.id for item in acquired.result.observations] == [426, 425, 426, 425]
    assert [item.price is not None for item in acquired.result.observations] == [
        True,
        False,
        False,
        True,
    ]
    assert not acquired.result.ready_for_pricing
    assert replay_fgiscs_material_history_acquisition(acquired) == acquired.result

    with pytest.raises(ValueError, match="journal is incomplete"):
        FgisCsMaterialHistoryAcquisition(
            request=acquired.request,
            result=acquired.result,
            exchanges=acquired.exchanges[:-1],
        )
    altered_exchange = FgisCsRawHttpExchange(
        request_uri=acquired.exchanges[-1].request_uri + "&unexpected=1",
        response_body=acquired.exchanges[-1].response_body,
    )
    altered = FgisCsMaterialHistoryAcquisition(
        request=acquired.request,
        result=acquired.result,
        exchanges=(*acquired.exchanges[:-1], altered_exchange),
    )
    with pytest.raises(ValueError, match="journal does not reproduce"):
        replay_fgiscs_material_history_acquisition(altered)


def test_material_history_rejects_duplicate_codes_before_network() -> None:
    with pytest.raises(ValueError, match="duplicate resource codes"):
        FgisCsMaterialHistoryRequest(
            subject_name="Московская область",
            resource_codes=("02.3.01.02-1102", "02.3.01.02-1102"),
        )


def test_material_history_retains_422_cell_and_continues_replayably(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code_one = "02.3.01.02-1102"
    code_two = "01.7.06.08-0008"
    requests = _install_responses(
        monkeypatch,
        [
            *_metadata_responses(),
            _Response(_json({"detail": "unprocessable period"}), status=422),
            _Response(_json(_price_payload(code=code_two))),
        ],
    )

    acquired = FgisCsPublicApi().acquire_material_history(
        FgisCsMaterialHistoryRequest(
            subject_name="Московская область",
            resource_codes=(code_one, code_two),
        ),
        retrieved_at=datetime(2026, 8, 3, 15, 0, tzinfo=UTC),
    )

    assert len(requests) == 5
    assert len(acquired.exchanges) == 5
    failed, successful = acquired.result.observations
    assert failed.price is None
    assert failed.response_status_code == 422
    assert failed.acquisition_error_code == "FGIS_HTTP_422"
    assert "FGIS_HTTP_422" in failed.pricing_blockers
    assert acquired.exchanges[3].status_code == 422
    assert successful.price is not None
    assert replay_fgiscs_material_history_acquisition(acquired) == acquired.result

    altered_error = FgisCsRawHttpExchange(
        request_uri=acquired.exchanges[3].request_uri,
        response_body=acquired.exchanges[3].response_body,
        status_code=404,
        media_type=acquired.exchanges[3].media_type,
    )
    altered = FgisCsMaterialHistoryAcquisition(
        request=acquired.request,
        result=acquired.result,
        exchanges=(*acquired.exchanges[:3], altered_error, acquired.exchanges[4]),
    )
    with pytest.raises(ValueError, match="does not reproduce"):
        replay_fgiscs_material_history_acquisition(altered)


def test_material_history_stops_on_retryable_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_responses(
        monkeypatch,
        [
            *_metadata_responses(),
            _Response(_json({"detail": "rate limited"}), status=429),
        ],
    )

    with pytest.raises(FgisCsPublicApiError) as captured:
        FgisCsPublicApi().acquire_material_history(
            FgisCsMaterialHistoryRequest(
                subject_name="Московская область",
                resource_codes=("02.3.01.02-1102",),
            )
        )

    assert captured.value.code == "FGIS_HTTP_429"
    assert captured.value.retryable
    assert captured.value.exchange is not None
    assert captured.value.exchange.status_code == 429


def test_lookup_material_returns_not_found_without_inventing_a_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_responses(
        monkeypatch,
        [*_metadata_responses(), _Response(_json({"items": []}))],
    )

    acquired = FgisCsPublicApi().acquire_material(_request())
    result = acquired.result

    assert result.price is None
    assert not result.ready_for_pricing
    assert result.requested_resource_code == "02.3.01.02-1102"
    prepared = prepare_fgiscs_diagnostic_material_package(acquired)
    assert "FGIS_PRICE_NOT_PUBLISHED" in prepared.manifest.blockers


def test_ksr_search_returns_unverified_source_names_and_units(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _json(
        [
            {
                "id": 9225217,
                "title": "02.3.01.02-1102",
                "description": "<em>Песок</em> природный I класс, мелкий",
                "name": "Песок природный для строительных работ I класс, мелкий",
                "unitName": "м3",
            }
        ]
    )
    requests = _install_responses(monkeypatch, [_Response(payload)])
    retrieved_at = datetime(2026, 7, 31, 9, 30, tzinfo=UTC)

    result = FgisCsPublicApi().search_ksr(
        "песок природный",
        retrieved_at=retrieved_at,
    )

    assert result.status.value == "UNVERIFIED"
    assert result.response_sha256 == hashlib.sha256(payload).hexdigest()
    assert result.retrieved_at == retrieved_at
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.status.value == "UNVERIFIED"
    assert candidate.source_record_id == "9225217"
    assert candidate.resource_code == "02.3.01.02-1102"
    assert candidate.source_item_name == ("Песок природный для строительных работ I класс, мелкий")
    assert candidate.unit == "м3"
    parsed = urlsplit(requests[0][1])
    assert parsed.path == "/api/Ksr/TipSearch"
    assert parse_qs(parsed.query) == {"value": ["песок природный"]}


def test_ksr_acquisition_retains_and_replays_raw_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _json(
        [
            {
                "id": 9225217,
                "title": "02.3.01.02-1102",
                "description": "Песок",
                "name": "Песок природный",
                "unitName": "м3",
            }
        ]
    )
    _install_responses(monkeypatch, [_Response(payload)])
    acquired = FgisCsPublicApi().acquire_ksr_search(
        "песок природный",
        retrieved_at=datetime(2026, 7, 31, 9, 30, tzinfo=UTC),
    )

    assert acquired.exchange.response_body == payload
    assert acquired.exchange.response_sha256 == acquired.result.response_sha256
    assert replay_fgiscs_ksr_search_acquisition(acquired) == acquired.result

    altered_payload = _json(
        [
            {
                "id": 9225217,
                "title": "02.3.01.02-1102",
                "description": "Песок",
                "name": "Другое наименование",
                "unitName": "м3",
            }
        ]
    )
    altered_exchange = FgisCsRawHttpExchange(
        request_uri=acquired.exchange.request_uri,
        response_body=altered_payload,
    )
    altered = FgisCsKsrSearchAcquisition(
        query=acquired.query,
        result=acquired.result.model_copy(
            update={"response_sha256": altered_exchange.response_sha256}
        ),
        exchange=altered_exchange,
    )
    with pytest.raises(ValueError, match="does not reproduce"):
        replay_fgiscs_ksr_search_acquisition(altered)

    with pytest.raises(ValueError, match="contains a failed HTTP response"):
        FgisCsKsrSearchAcquisition(
            query=acquired.query,
            result=acquired.result,
            exchange=FgisCsRawHttpExchange(
                request_uri=acquired.exchange.request_uri,
                response_body=acquired.exchange.response_body,
                status_code=422,
            ),
        )


def test_ksr_search_rejects_schema_drift_and_duplicate_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    numeric_name = [
        {
            "id": 9225217,
            "title": "02.3.01.02-1102",
            "description": "Песок",
            "name": 123,
            "unitName": "м3",
        }
    ]
    _install_responses(monkeypatch, [_Response(_json(numeric_name))])
    with pytest.raises(FgisCsPublicApiError) as invalid:
        FgisCsPublicApi().search_ksr("песок")
    assert invalid.value.code == "FGIS_KSR_SCHEMA_INVALID"

    duplicate = {
        "id": 9225217,
        "title": "02.3.01.02-1102",
        "description": "Песок",
        "name": "Песок природный",
        "unitName": "м3",
    }
    _install_responses(
        monkeypatch,
        [_Response(_json([duplicate, duplicate]))],
    )
    with pytest.raises(FgisCsPublicApiError) as duplicated:
        FgisCsPublicApi().search_ksr("песок")
    assert duplicated.value.code == "FGIS_KSR_DUPLICATE_CANDIDATE"


def test_lookup_material_rejects_non_exact_and_ambiguous_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_responses(
        monkeypatch,
        [
            *_metadata_responses(),
            _Response(_json(_price_payload(code="02.3.01.02-9999"))),
        ],
    )
    with pytest.raises(FgisCsPublicApiError) as non_exact:
        FgisCsPublicApi().lookup_material(_request())
    assert non_exact.value.code == "FGIS_NON_EXACT_SEARCH_RESULT"

    ambiguous_payload = _price_payload()
    duplicate = dict(ambiguous_payload["items"][0]["items"][0])
    duplicate["id"] = 3655582
    ambiguous_payload["items"][0]["items"].append(duplicate)
    ambiguous_payload["total"] = 2
    _install_responses(
        monkeypatch,
        [*_metadata_responses(), _Response(_json(ambiguous_payload))],
    )
    with pytest.raises(FgisCsPublicApiError) as ambiguous:
        FgisCsPublicApi().lookup_material(_request())
    assert ambiguous.value.code == "FGIS_AMBIGUOUS_EXACT_PRICE"


def test_lookup_material_rejects_numeric_financial_json_and_total_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    numeric_payload = _price_payload()
    numeric_payload["items"][0]["items"][0]["estimatedPrice"] = 1054.48
    _install_responses(
        monkeypatch,
        [*_metadata_responses(), _Response(_json(numeric_payload))],
    )
    with pytest.raises(FgisCsPublicApiError) as numeric:
        FgisCsPublicApi().lookup_material(_request())
    assert numeric.value.code == "FGIS_PRICE_SCHEMA_INVALID"

    drifted_payload = _price_payload()
    drifted_payload["total"] = 2
    _install_responses(
        monkeypatch,
        [*_metadata_responses(), _Response(_json(drifted_payload))],
    )
    with pytest.raises(FgisCsPublicApiError) as drifted:
        FgisCsPublicApi().lookup_material(_request())
    assert drifted.value.code == "FGIS_PRICE_SCHEMA_INVALID"


def test_lookup_material_requires_unambiguous_exact_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_responses(
        monkeypatch,
        [
            _Response(
                _json(
                    [
                        {"id": 331, "name": "Московская область"},
                        {"id": 999, "name": "Московская область"},
                    ]
                )
            )
        ],
    )
    with pytest.raises(FgisCsPublicApiError) as duplicate_subject:
        FgisCsPublicApi().lookup_material(_request())
    assert duplicate_subject.value.code == "FGIS_SUBJECT_AMBIGUOUS"

    _install_responses(
        monkeypatch,
        [
            _Response(_json([{"id": 331, "name": "Московская область"}])),
            _Response(
                _json(
                    [
                        {"id": 127, "name": "Московская область"},
                        {"id": 128, "name": "Москва, зона 2"},
                    ]
                )
            ),
        ],
    )
    with pytest.raises(FgisCsPublicApiError) as zone_required:
        FgisCsPublicApi().lookup_material(_request())
    assert zone_required.value.code == "FGIS_PRICE_ZONE_REQUIRED"


def test_transport_is_bounded_and_classifies_retryable_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_responses(
        monkeypatch,
        [_Response(b"{}" * 20)],
    )
    with pytest.raises(FgisCsPublicApiError) as oversized:
        FgisCsPublicApi(max_response_bytes=10).list_subjects()
    assert oversized.value.code == "FGIS_RESPONSE_TOO_LARGE"
    assert not oversized.value.retryable

    _install_responses(
        monkeypatch,
        [_Response(b"{}", status=429)],
    )
    with pytest.raises(FgisCsPublicApiError) as rate_limited:
        FgisCsPublicApi().list_subjects()
    assert rate_limited.value.code == "FGIS_HTTP_429"
    assert rate_limited.value.retryable

    _install_responses(
        monkeypatch,
        [_Response(b"[]", media_type="text/html")],
    )
    with pytest.raises(FgisCsPublicApiError) as media_type:
        FgisCsPublicApi().list_subjects()
    assert media_type.value.code == "FGIS_MEDIA_TYPE_INVALID"
