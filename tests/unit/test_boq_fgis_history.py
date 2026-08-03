from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode

import pytest

from tenderguard import cli
from tenderguard.application.boq_fgis_history import (
    BoqFgisHistoryLineSelection,
    BoqFgisHistoryPackage,
    BoqFgisHistoryProfile,
    PreparedBoqFgisHistoryPackage,
    run_boq_fgis_history_research,
    verify_boq_fgis_history_package,
)
from tenderguard.application.free_source_research import (
    BoqFreeSourceLineRule,
    BoqFreeSourceResearchProfile,
    BoqResearchEvidenceReference,
    run_boq_free_source_research,
)
from tenderguard.domain.boq_spreadsheet import (
    BoqCellEvidence,
    BoqRowCandidate,
    BoqXlsxExtractionResult,
)
from tenderguard.domain.common import canonical_json, content_hash
from tenderguard.integrations.fgiscs_public import (
    FGIS_CS_ORIGIN,
    FGIS_CS_PUBLIC_SCHEMA_VERSION,
    FgisCsKsrCandidate,
    FgisCsKsrSearchAcquisition,
    FgisCsKsrSearchResult,
    FgisCsMaterialHistoryAcquisition,
    FgisCsMaterialHistoryRequest,
    FgisCsPublicApi,
    FgisCsPublicApiError,
    FgisCsRawHttpExchange,
)

_SAND_CODE = "02.3.01.02-1104"
_CABLE_CODE = "21.1.07.02-1144"
_RESEARCH_MANIFEST_SHA256 = "e" * 64


def _candidate(identity: str, row: int, description: str, unit: str) -> BoqRowCandidate:
    return BoqRowCandidate(
        provisional_candidate_id=f"boq-candidate-{identity * 24}",
        worksheet_name="BoQ",
        row_number=row,
        source_position_id=str(row),
        description=description,
        unit=unit,
        quantity=Decimal("2"),
        cells={
            "description": BoqCellEvidence(
                coordinate=f"B{row}",
                value_kind="TEXT",
                source_literal=description,
            )
        },
    )


def _research():
    candidates = (
        _candidate("a", 2, "Песок природный", "м3"),
        _candidate("b", 3, "Кабель АПвПг", "м"),
    )
    extraction = BoqXlsxExtractionResult(
        status="UNVERIFIED",
        profile_version_id="xlsx-profile-v1",
        profile_content_hash="1" * 64,
        root_object_sha256="2" * 64,
        workbook_object_sha256="3" * 64,
        archive_path="source.xlsx",
        worksheet_name="BoQ",
        candidates=candidates,
        extracted_at=datetime(2026, 8, 3, 9, 0, tzinfo=UTC),
    )
    profile = BoqFreeSourceResearchProfile(
        profile_version_id="research-v1",
        project_code="PROJECT-1",
        subject_name="Московская область",
        expected_extraction_content_hash=content_hash(extraction),
        expected_workbook_sha256=extraction.workbook_object_sha256,
        context_evidence=(
            BoqResearchEvidenceReference(
                label="Project title",
                object_sha256="4" * 64,
                source_locator="project.pdf#page=1",
            ),
        ),
        line_rules=tuple(
            BoqFreeSourceLineRule(
                candidate_id=item.provisional_candidate_id,
                cost_nature="MATERIAL",
                fgis_ksr_query=item.description,
            )
            for item in candidates
        ),
    )

    def acquire_ksr(query: str) -> FgisCsKsrSearchAcquisition:
        code = _SAND_CODE if query == "Песок природный" else _CABLE_CODE
        unit = "м3" if query == "Песок природный" else "м"
        payload = json.dumps(
            [
                {
                    "id": 101 if code == _SAND_CODE else 102,
                    "title": code,
                    "description": query,
                    "name": query,
                    "unitName": unit,
                }
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        request_uri = f"{FGIS_CS_ORIGIN}/api/Ksr/TipSearch?" + urlencode({"value": query})
        exchange = FgisCsRawHttpExchange(request_uri=request_uri, response_body=payload)
        result = FgisCsKsrSearchResult(
            schema_version=FGIS_CS_PUBLIC_SCHEMA_VERSION,
            query=query,
            candidates=(
                FgisCsKsrCandidate(
                    source_record_id="101" if code == _SAND_CODE else "102",
                    resource_code=code,
                    source_item_name=query,
                    unit=unit,
                ),
            ),
            api_request_uri=request_uri,
            response_sha256=exchange.response_sha256,
            retrieved_at=datetime(2026, 8, 3, 9, 30, tzinfo=UTC),
        )
        return FgisCsKsrSearchAcquisition(query=query, result=result, exchange=exchange)

    return run_boq_free_source_research(
        extraction=extraction,
        profile=profile,
        acquire_ksr_search=acquire_ksr,
        completed_at=datetime(2026, 8, 3, 10, 0, tzinfo=UTC),
    )


def _history_acquisition(
    request: FgisCsMaterialHistoryRequest,
) -> FgisCsMaterialHistoryAcquisition:
    exchanges: list[FgisCsRawHttpExchange] = []

    def fetch(path: str, parameters: dict[str, str | int]):
        if path.endswith("CountrySubjects"):
            raw = [{"id": 331, "name": "Московская область"}]
        elif path.endswith("PriceZones"):
            raw = [{"id": 127, "name": "Московская область"}]
        elif path.endswith("Periods"):
            raw = [
                {"id": 426, "name": "2 квартал 2026 г."},  # noqa: RUF001
                {"id": 425, "name": "1 квартал 2026 г."},  # noqa: RUF001
            ]
        else:
            code = str(parameters["value"])
            period_id = int(parameters["periodId"])
            if code == _SAND_CODE and period_id == 425:
                raw = {"detail": "unprocessable period"}
                payload = json.dumps(raw, separators=(",", ":")).encode()
                query = urlencode(parameters)
                request_uri = f"{FGIS_CS_ORIGIN}{path}?{query}"
                exchange = FgisCsRawHttpExchange(
                    request_uri=request_uri,
                    response_body=payload,
                    status_code=422,
                )
                exchanges.append(exchange)
                raise FgisCsPublicApiError(
                    code="FGIS_HTTP_422",
                    exchange=exchange,
                )
            if code == _SAND_CODE and period_id == 426:
                raw = {
                    "items": [
                        {
                            "id": 1,
                            "ksrType": 1,
                            "items": [
                                {
                                    "code": code,
                                    "name": "Песок природный",
                                    "unitName": "м3",
                                    "aggregatedPrice": "890.24",
                                    "estimatedPrice": "1452.64",
                                    "distancePrice": "533.92",
                                    "procureStorageCostPercent": "2.00",
                                    "ksrType": 1,
                                    "id": 3655581,
                                }
                            ],
                        }
                    ],
                    "total": 1,
                }
            else:
                raw = {"items": [], "total": 0}
        payload = json.dumps(raw, ensure_ascii=False, separators=(",", ":")).encode()
        query = urlencode(parameters)
        request_uri = f"{FGIS_CS_ORIGIN}{path}" + (f"?{query}" if query else "")
        exchanges.append(FgisCsRawHttpExchange(request_uri=request_uri, response_body=payload))
        return raw, payload, request_uri

    result = FgisCsPublicApi()._lookup_material_history_with_fetch(
        request,
        fetch=fetch,
        retrieved_at=datetime(2026, 8, 3, 11, 0, tzinfo=UTC),
    )
    return FgisCsMaterialHistoryAcquisition(
        request=request,
        result=result,
        exchanges=tuple(exchanges),
    )


def _profile(
    research,
    manifest_sha256: str = _RESEARCH_MANIFEST_SHA256,
) -> BoqFgisHistoryProfile:
    return BoqFgisHistoryProfile(
        profile_version_id="history-v1",
        expected_research_manifest_sha256=manifest_sha256,
        subject_name="Московская область",
        price_zone_name="Московская область",
        line_selections=(
            BoqFgisHistoryLineSelection(
                candidate_id=research.result.lines[0].candidate_id,
                decision="DIAGNOSTIC_CANDIDATES_SELECTED",
                resource_codes=(_SAND_CODE,),
            ),
            BoqFgisHistoryLineSelection(
                candidate_id=research.result.lines[1].candidate_id,
                decision="NO_SUITABLE_CANDIDATE_RETRIEVED",
            ),
        ),
    )


def test_boq_fgis_history_is_bound_complete_replayable_and_blocked() -> None:
    research = _research()
    prepared = run_boq_fgis_history_research(
        research=research,
        research_manifest_sha256=_RESEARCH_MANIFEST_SHA256,
        profile=_profile(research),
        acquire_material_history=_history_acquisition,
    )

    assert prepared.manifest.status == "BLOCKED"
    assert not prepared.manifest.ready_for_pricing
    assert len(prepared.manifest.history.periods) == 2
    assert len(prepared.manifest.history.observations) == 2
    assert len(prepared.manifest.raw_responses) == 5
    assert prepared.manifest.line_results[0].published_observation_count == 1
    assert prepared.manifest.line_results[0].source_error_observation_count == 1
    assert "FGIS_HISTORY_SOURCE_ERRORS_PRESENT" in (
        prepared.manifest.line_results[0].pricing_blockers
    )
    assert prepared.manifest.line_results[1].published_observation_count == 0
    assert "FGIS_KSR_SUITABLE_CANDIDATE_NOT_SELECTED" in (
        prepared.manifest.line_results[1].pricing_blockers
    )
    assert verify_boq_fgis_history_package(prepared) == prepared.manifest

    corrupted = (
        *prepared.raw_files[:-1],
        (prepared.raw_files[-1][0], prepared.raw_files[-1][1] + b" "),
    )
    with pytest.raises(ValueError, match="raw response differs"):
        PreparedBoqFgisHistoryPackage(
            manifest=prepared.manifest,
            raw_files=corrupted,
        )
    incomplete = prepared.manifest.model_dump(mode="python")
    incomplete["raw_responses"] = incomplete["raw_responses"][:-1]
    with pytest.raises(ValueError, match="count differs"):
        BoqFgisHistoryPackage.model_validate(incomplete)
    altered_status = prepared.manifest.model_dump(mode="python")
    altered_status["raw_responses"][3]["status_code"] = 422
    with pytest.raises(ValueError, match="differs from its observation"):
        BoqFgisHistoryPackage.model_validate(altered_status)
    altered_count = prepared.manifest.model_dump(mode="python")
    altered_count["line_results"][0]["published_observation_count"] = 2
    with pytest.raises(ValueError, match="counters differ"):
        BoqFgisHistoryPackage.model_validate(altered_count)
    altered_profile = prepared.manifest.model_dump(mode="python")
    altered_profile["profile"]["subject_name"] = "Другой субъект"
    with pytest.raises(ValueError, match="profile binding does not reproduce"):
        BoqFgisHistoryPackage.model_validate(altered_profile)


def test_boq_fgis_history_rejects_unbound_incomplete_or_invented_selections() -> None:
    research = _research()
    profile = _profile(research)

    with pytest.raises(ValueError, match="not bound"):
        run_boq_fgis_history_research(
            research=research,
            research_manifest_sha256="f" * 64,
            profile=profile,
            acquire_material_history=_history_acquisition,
        )
    with pytest.raises(ValueError, match="classify every material"):
        run_boq_fgis_history_research(
            research=research,
            research_manifest_sha256=_RESEARCH_MANIFEST_SHA256,
            profile=profile.model_copy(update={"line_selections": profile.line_selections[:-1]}),
            acquire_material_history=_history_acquisition,
        )
    invented = profile.line_selections[0].model_copy(update={"resource_codes": ("99.9.99-9999",)})
    with pytest.raises(ValueError, match="absent from retained KSR"):
        run_boq_fgis_history_research(
            research=research,
            research_manifest_sha256=_RESEARCH_MANIFEST_SHA256,
            profile=profile.model_copy(
                update={"line_selections": (invented, profile.line_selections[1])}
            ),
            acquire_material_history=_history_acquisition,
        )


def test_boq_fgis_history_cli_publishes_atomically_and_cleans_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    research = _research()
    research_dir = tmp_path / "research"
    (research_dir / "raw").mkdir(parents=True)
    research_manifest = canonical_json(research.result) + b"\n"
    (research_dir / "manifest.json").write_bytes(research_manifest)
    for object_hash, content in research.raw_responses:
        (research_dir / "raw" / f"{object_hash}.json").write_bytes(content)
    profile = _profile(research, hashlib.sha256(research_manifest).hexdigest())
    profile_path = tmp_path / "history-profile.json"
    profile_path.write_text(profile.model_dump_json(indent=2), encoding="utf-8")
    monkeypatch.setattr(
        cli.FgisCsPublicApi,
        "acquire_material_history",
        lambda _self, request: _history_acquisition(request),
    )

    output_dir = tmp_path / "history"
    assert (
        cli.research_boq_fgis_history(
            research_dir=research_dir,
            profile_path=profile_path,
            output_dir=output_dir,
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "BLOCKED"
    manifest = BoqFgisHistoryPackage.model_validate_json(
        (output_dir / "manifest.json").read_bytes()
    )
    assert len(tuple((output_dir / "raw").glob("*.bin"))) == len(manifest.raw_responses)

    assert (
        cli.research_boq_fgis_history(
            research_dir=research_dir,
            profile_path=profile_path,
            output_dir=output_dir,
        )
        == 2
    )
    assert json.loads(capsys.readouterr().out)["code"] == ("BOQ_FGIS_HISTORY_OUTPUT_ALREADY_EXISTS")

    def fail_source(_self, _request):
        raise FgisCsPublicApiError(code="FGIS_HTTP_429", retryable=True)

    monkeypatch.setattr(cli.FgisCsPublicApi, "acquire_material_history", fail_source)
    assert (
        cli.research_boq_fgis_history(
            research_dir=research_dir,
            profile_path=profile_path,
            output_dir=tmp_path / "source-failed-history",
        )
        == 2
    )
    source_failure = json.loads(capsys.readouterr().out)
    assert source_failure["source_error_code"] == "FGIS_HTTP_429"
    assert source_failure["source_error_retryable"] is True

    monkeypatch.setattr(
        cli.FgisCsPublicApi,
        "acquire_material_history",
        lambda _self, request: _history_acquisition(request),
    )

    original_write = cli._write_bytes_exclusive

    def fail_manifest(destination: Path, payload: bytes) -> None:
        if destination.name == "manifest.json":
            raise OSError("simulated manifest failure")
        original_write(destination, payload)

    monkeypatch.setattr(cli, "_write_bytes_exclusive", fail_manifest)
    failed_output = tmp_path / "failed-history"
    assert (
        cli.research_boq_fgis_history(
            research_dir=research_dir,
            profile_path=profile_path,
            output_dir=failed_output,
        )
        == 2
    )
    assert json.loads(capsys.readouterr().out)["package_published"] is False
    assert not failed_output.exists()
    assert not tuple(tmp_path.glob(".failed-history.staging-*"))
