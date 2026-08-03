from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import urlencode

import pytest

from tenderguard import cli
from tenderguard.application.free_source_research import (
    BoqFreeSourceLineRule,
    BoqFreeSourceResearchLine,
    BoqFreeSourceResearchProfile,
    BoqResearchEvidenceReference,
    run_boq_free_source_research,
    verify_boq_free_source_research_package,
)
from tenderguard.domain.boq_spreadsheet import (
    BoqCellEvidence,
    BoqRowCandidate,
    BoqXlsxExtractionResult,
)
from tenderguard.domain.common import content_hash
from tenderguard.integrations.fgiscs_public import (
    FGIS_CS_PUBLIC_SCHEMA_VERSION,
    FgisCsKsrCandidate,
    FgisCsKsrSearchAcquisition,
    FgisCsKsrSearchResult,
    FgisCsPublicApiError,
    FgisCsRawHttpExchange,
)


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


def _extraction(*, two_materials: bool = False) -> BoqXlsxExtractionResult:
    candidates = [
        _candidate("a", 2, "Разработка грунта", "1000 м3"),
        _candidate("b", 3, "Песок природный", "м3"),
        _candidate("c", 4, "Перевозка грунта", "т"),
    ]
    if two_materials:
        candidates.append(_candidate("d", 5, "Кабель силовой", "м"))
    return BoqXlsxExtractionResult(
        status="UNVERIFIED",
        profile_version_id="xlsx-profile-v1",
        profile_content_hash="1" * 64,
        root_object_sha256="2" * 64,
        workbook_object_sha256="3" * 64,
        archive_path="source.xlsx",
        worksheet_name="BoQ",
        candidates=tuple(candidates),
        extracted_at=datetime(2026, 8, 3, 9, 0, tzinfo=UTC),
    )


def _profile(extraction: BoqXlsxExtractionResult) -> BoqFreeSourceResearchProfile:
    nature = {
        "Разработка грунта": "WORK",
        "Песок природный": "MATERIAL",
        "Перевозка грунта": "LOGISTICS",
        "Кабель силовой": "MATERIAL",
    }
    return BoqFreeSourceResearchProfile(
        profile_version_id="research-v1",
        project_code="PROJECT-1",
        subject_name="Республика Татарстан",
        expected_extraction_content_hash=content_hash(extraction),
        expected_workbook_sha256=extraction.workbook_object_sha256,
        context_evidence=(
            BoqResearchEvidenceReference(
                label="Project title page",
                object_sha256="4" * 64,
                source_locator="project.pdf#page=1",
            ),
        ),
        line_rules=tuple(
            BoqFreeSourceLineRule(
                candidate_id=candidate.provisional_candidate_id,
                cost_nature=nature[candidate.description or ""],
                fgis_ksr_query=(
                    candidate.description
                    if nature[candidate.description or ""] == "MATERIAL"
                    else None
                ),
            )
            for candidate in extraction.candidates
        ),
    )


def _acquisition(query: str) -> FgisCsKsrSearchAcquisition:
    payload = json.dumps(
        [
            {
                "id": 101,
                "title": "02.3.01.02-1104",
                "description": query,
                "name": query,
                "unitName": "м3" if "Песок" in query else "м",
            }
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    request_uri = "https://fgiscs.minstroyrf.ru/api/Ksr/TipSearch?" + urlencode({"value": query})
    exchange = FgisCsRawHttpExchange(request_uri=request_uri, response_body=payload)
    result = FgisCsKsrSearchResult(
        schema_version=FGIS_CS_PUBLIC_SCHEMA_VERSION,
        query=query,
        candidates=(
            FgisCsKsrCandidate(
                source_record_id="101",
                resource_code="02.3.01.02-1104",
                source_item_name=query,
                unit="м3" if "Песок" in query else "м",
            ),
        ),
        api_request_uri=request_uri,
        response_sha256=hashlib.sha256(payload).hexdigest(),
        retrieved_at=datetime(2026, 8, 3, 9, 30, tzinfo=UTC),
    )
    return FgisCsKsrSearchAcquisition(query=query, result=result, exchange=exchange)


def test_research_classifies_every_row_and_retains_raw_fgis_evidence() -> None:
    extraction = _extraction()
    calls: list[str] = []

    def acquire(query: str) -> FgisCsKsrSearchAcquisition:
        calls.append(query)
        return _acquisition(query)

    prepared = run_boq_free_source_research(
        extraction=extraction,
        profile=_profile(extraction),
        acquire_ksr_search=acquire,
        completed_at=datetime(2026, 8, 3, 10, 0, tzinfo=UTC),
    )

    assert calls == ["Песок природный"]
    assert prepared.result.status == "UNVERIFIED"
    assert not prepared.result.ready_for_pricing
    assert [line.cost_nature for line in prepared.result.lines] == [
        "WORK",
        "MATERIAL",
        "LOGISTICS",
    ]
    material = prepared.result.lines[1]
    assert material.fgis_search_result is not None
    assert material.literal_name_unit_candidate_ids == ("101",)
    assert "APPROVED_FGIS_MAPPING_REQUIRED" in material.pricing_blockers
    assert len(prepared.raw_responses) == 1
    assert prepared.result.raw_artifacts[0].sha256 == prepared.raw_responses[0][0]
    assert verify_boq_free_source_research_package(prepared) == prepared.result

    tampered_line = material.model_dump(mode="python")
    tampered_line["literal_name_unit_candidate_ids"] = ("not-returned",)
    with pytest.raises(ValueError, match="candidate identities are invalid"):
        BoqFreeSourceResearchLine.model_validate(tampered_line)

    tampered_blockers = material.model_dump(mode="python")
    tampered_blockers["pricing_blockers"] = (
        *material.pricing_blockers,
        "FGIS_KSR_EXACT_LITERAL_CANDIDATE_NOT_FOUND",
    )
    with pytest.raises(ValueError, match="blockers contradict"):
        BoqFreeSourceResearchLine.model_validate(tampered_blockers)


def test_research_rejects_incomplete_or_drifted_profile() -> None:
    extraction = _extraction()
    profile = _profile(extraction)

    with pytest.raises(ValueError, match="classify every"):
        run_boq_free_source_research(
            extraction=extraction,
            profile=profile.model_copy(update={"line_rules": profile.line_rules[:-1]}),
            acquire_ksr_search=_acquisition,
        )


def test_research_retains_source_description_but_sanitizes_external_queries() -> None:
    original_extraction = _extraction()
    extraction = original_extraction.model_copy(
        update={
            "candidates": (
                _candidate("a", 2, "Разработка  грунта\nв траншее", "1000 м3"),
                *original_extraction.candidates[1:],
            )
        }
    )
    profile = _profile(original_extraction).model_copy(
        update={"expected_extraction_content_hash": content_hash(extraction)}
    )

    prepared = run_boq_free_source_research(
        extraction=extraction,
        profile=profile,
        acquire_ksr_search=_acquisition,
    )

    line = prepared.result.lines[0]
    assert line.boq_description == "Разработка  грунта\nв траншее"
    assert line.market_query == "Разработка грунта в траншее"
    assert line.eis_query == "Разработка грунта в траншее"
    drifted_extraction = extraction.model_copy(
        update={
            "candidates": (
                extraction.candidates[0].model_copy(update={"unit": "м3"}),
                *extraction.candidates[1:],
            )
        }
    )
    with pytest.raises(ValueError, match="not bound to the supplied extraction"):
        run_boq_free_source_research(
            extraction=drifted_extraction,
            profile=profile,
            acquire_ksr_search=_acquisition,
        )


def test_retryable_fgis_failure_stops_later_material_requests() -> None:
    extraction = _extraction(two_materials=True)
    calls: list[str] = []

    def fail(query: str) -> FgisCsKsrSearchAcquisition:
        calls.append(query)
        raise FgisCsPublicApiError(code="FGIS_HTTP_429", retryable=True)

    prepared = run_boq_free_source_research(
        extraction=extraction,
        profile=_profile(extraction),
        acquire_ksr_search=fail,
    )

    assert calls == ["Песок природный"]
    assert prepared.result.status == "BLOCKED"
    material_lines = [line for line in prepared.result.lines if line.cost_nature == "MATERIAL"]
    assert [line.acquisition_error_code for line in material_lines] == [
        "FGIS_HTTP_429",
        "FGIS_QUERY_NOT_ATTEMPTED_AFTER_RETRYABLE_FAILURE",
    ]
    assert not prepared.raw_responses


def test_cli_publishes_complete_package_and_cleans_failed_staging(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    extraction = _extraction()
    profile = _profile(extraction)
    input_path = tmp_path / "extraction.json"
    profile_path = tmp_path / "profile.json"
    input_path.write_text(extraction.model_dump_json(indent=2), encoding="utf-8")
    profile_path.write_text(profile.model_dump_json(indent=2), encoding="utf-8")
    monkeypatch.setattr(
        "tenderguard.integrations.fgiscs_public.FgisCsPublicApi.acquire_ksr_search",
        lambda _self, query: _acquisition(query),
    )

    output_dir = tmp_path / "research"
    assert (
        cli.research_boq_free_sources(
            input_path=input_path,
            profile_path=profile_path,
            output_dir=output_dir,
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "UNVERIFIED"
    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["ready_for_pricing"] is False
    assert len(tuple((output_dir / "raw").glob("*.json"))) == 1

    failed_output = tmp_path / "failed-research"
    original_write = cli._write_bytes_exclusive

    def fail_manifest(destination, payload) -> None:
        if destination.name == "manifest.json":
            raise OSError("simulated manifest failure")
        original_write(destination, payload)

    monkeypatch.setattr(cli, "_write_bytes_exclusive", fail_manifest)
    assert (
        cli.research_boq_free_sources(
            input_path=input_path,
            profile_path=profile_path,
            output_dir=failed_output,
        )
        == 2
    )
    assert json.loads(capsys.readouterr().out)["package_published"] is False
    assert not failed_output.exists()
    assert not tuple(tmp_path.glob(".failed-research.staging-*"))
