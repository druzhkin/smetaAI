from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode

import pytest

from tenderguard import cli
from tenderguard.application.boq_market_research import (
    BoqMarketLineSelection,
    BoqMarketResearchPackage,
    BoqMarketResearchProfile,
    PreparedBoqMarketResearchPackage,
    run_boq_market_research,
    verify_boq_market_research_package,
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
from tenderguard.domain.enums import PriceSourceType
from tenderguard.integrations.fgiscs_public import (
    FGIS_CS_ORIGIN,
    FGIS_CS_PUBLIC_SCHEMA_VERSION,
    FgisCsKsrCandidate,
    FgisCsKsrSearchAcquisition,
    FgisCsKsrSearchResult,
    FgisCsRawHttpExchange,
)
from tenderguard.integrations.public_market import (
    PublicMarketPageClient,
    PublicMarketPageRequest,
    PublicMarketRawHttpExchange,
)

_RESEARCH_MANIFEST_SHA256 = "e" * 64
_GOOD_URI = "https://supplier.example/products/cable"
_BROKEN_URI = "https://broken.example/products/cable"


def _candidate(identity: str, row: int, description: str) -> BoqRowCandidate:
    return BoqRowCandidate(
        provisional_candidate_id=f"boq-candidate-{identity * 24}",
        worksheet_name="BoQ",
        row_number=row,
        source_position_id=str(row),
        description=description,
        unit="m",
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
        _candidate("a", 2, "Cable APvPg 1x240/70"),
        _candidate("b", 3, "Protective pipe D160"),
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
        subject_name="Test subject",
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
        payload = json.dumps(
            [
                {
                    "id": 101 if query.startswith("Cable") else 102,
                    "title": "21.1.07.02-1144" if query.startswith("Cable") else "24.1.02-0001",
                    "description": query,
                    "name": query,
                    "unitName": "m",
                }
            ],
            separators=(",", ":"),
        ).encode()
        uri = f"{FGIS_CS_ORIGIN}/api/Ksr/TipSearch?" + urlencode({"value": query})
        exchange = FgisCsRawHttpExchange(request_uri=uri, response_body=payload)
        result = FgisCsKsrSearchResult(
            schema_version=FGIS_CS_PUBLIC_SCHEMA_VERSION,
            query=query,
            candidates=(
                FgisCsKsrCandidate(
                    source_record_id="101" if query.startswith("Cable") else "102",
                    resource_code=(
                        "21.1.07.02-1144" if query.startswith("Cable") else "24.1.02-0001"
                    ),
                    source_item_name=query,
                    unit="m",
                ),
            ),
            api_request_uri=uri,
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


def _profile(research, manifest_sha256: str = _RESEARCH_MANIFEST_SHA256):
    return BoqMarketResearchProfile(
        profile_version_id="market-v1",
        expected_research_manifest_sha256=manifest_sha256,
        line_selections=(
            BoqMarketLineSelection(
                candidate_id=research.result.lines[0].candidate_id,
                decision="DIAGNOSTIC_URLS_SELECTED",
                sources=(
                    PublicMarketPageRequest(
                        source_uri=_GOOD_URI,
                        source_type=PriceSourceType.SUPPLIER_WEBSITE,
                        display_name="Supplier",
                    ),
                    PublicMarketPageRequest(
                        source_uri=_BROKEN_URI,
                        source_type=PriceSourceType.SUPPLIER_WEBSITE,
                        display_name="Broken supplier",
                    ),
                ),
            ),
            BoqMarketLineSelection(
                candidate_id=research.result.lines[1].candidate_id,
                decision="NO_PUBLIC_SOURCE_SELECTED",
            ),
        ),
    )


def _market_exchange(request: PublicMarketPageRequest) -> PublicMarketRawHttpExchange:
    if request.source_uri == _GOOD_URI:
        html = """
        <div itemscope itemtype="https://schema.org/Product">
          <span itemprop="name">Cable APvPg 1x240/70</span>
          <meta itemprop="sku" content="CABLE-1">
          <div itemscope itemtype="https://schema.org/Offer">
            <meta itemprop="price" content="533.28">
            <meta itemprop="priceCurrency" content="RUB">
          </div>
        </div>
        """
        return PublicMarketRawHttpExchange(
            request_uri=request.source_uri,
            response_body=html.encode(),
            status_code=200,
            media_type="text/html",
            charset="utf-8",
        )
    return PublicMarketRawHttpExchange(
        request_uri=request.source_uri,
        response_body=b"<html><body>broken charset</body></html>",
        status_code=200,
        media_type="text/html",
        charset="utf-16",
    )


def _acquire_market(request: PublicMarketPageRequest):
    return PublicMarketPageClient(fetch=_market_exchange).acquire_page(
        request,
        retrieved_at=datetime(2026, 8, 3, 11, 0, tzinfo=UTC),
    )


def test_market_package_is_bound_replayable_and_fail_closed() -> None:
    research = _research()
    prepared = run_boq_market_research(
        research=research,
        research_manifest_sha256=_RESEARCH_MANIFEST_SHA256,
        profile=_profile(research),
        acquire_page=_acquire_market,
        completed_at=datetime(2026, 8, 3, 12, 0, tzinfo=UTC),
    )

    assert prepared.manifest.status == "BLOCKED"
    assert not prepared.manifest.ready_for_pricing
    assert len(prepared.manifest.raw_responses) == 2
    first, second = prepared.manifest.line_results
    assert first.offer_candidate_count == 1
    assert first.source_error_count == 1
    assert first.sources[0].page_result is not None
    assert first.sources[0].page_result.candidates[0].amount == Decimal("533.28")
    assert "MARKET_SOURCE_ERRORS_PRESENT" in first.pricing_blockers
    assert second.offer_candidate_count == 0
    assert "MARKET_PUBLIC_SOURCE_NOT_SELECTED" in second.pricing_blockers
    assert verify_boq_market_research_package(prepared) == prepared.manifest

    corrupted = (
        *prepared.raw_files[:-1],
        (prepared.raw_files[-1][0], prepared.raw_files[-1][1] + b" "),
    )
    with pytest.raises(ValueError, match="raw response differs"):
        PreparedBoqMarketResearchPackage(manifest=prepared.manifest, raw_files=corrupted)
    altered_count = prepared.manifest.model_dump(mode="python")
    altered_count["line_results"][0]["offer_candidate_count"] = 2
    with pytest.raises(ValueError, match="counters differ"):
        BoqMarketResearchPackage.model_validate(altered_count)
    altered_profile = prepared.manifest.model_dump(mode="python")
    altered_profile["profile"]["profile_version_id"] = "market-v2"
    with pytest.raises(ValueError, match="profile binding does not reproduce"):
        BoqMarketResearchPackage.model_validate(altered_profile)


def test_market_research_rejects_unbound_incomplete_or_duplicate_source_profiles() -> None:
    research = _research()
    profile = _profile(research)
    with pytest.raises(ValueError, match="not bound"):
        run_boq_market_research(
            research=research,
            research_manifest_sha256="f" * 64,
            profile=profile,
            acquire_page=_acquire_market,
        )
    with pytest.raises(ValueError, match="classify every material"):
        run_boq_market_research(
            research=research,
            research_manifest_sha256=_RESEARCH_MANIFEST_SHA256,
            profile=profile.model_copy(update={"line_selections": profile.line_selections[:-1]}),
            acquire_page=_acquire_market,
        )
    duplicate = profile.line_selections[1].model_copy(
        update={
            "decision": "DIAGNOSTIC_URLS_SELECTED",
            "sources": (profile.line_selections[0].sources[0],),
        }
    )
    with pytest.raises(ValueError, match="reuses one URL"):
        BoqMarketResearchProfile.model_validate(
            profile.model_dump(mode="python")
            | {"line_selections": (profile.line_selections[0], duplicate)}
        )


def test_market_cli_publishes_atomically_and_cleans_failed_write(
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
    profile_path = tmp_path / "market-profile.json"
    profile_path.write_text(profile.model_dump_json(indent=2), encoding="utf-8")
    original_acquire_page = PublicMarketPageClient.acquire_page

    def acquire_page(_self, request):
        return original_acquire_page(
            PublicMarketPageClient(fetch=_market_exchange),
            request,
            retrieved_at=datetime(2026, 8, 3, 11, 0, tzinfo=UTC),
        )

    monkeypatch.setattr(
        cli.PublicMarketPageClient,
        "acquire_page",
        acquire_page,
    )

    output_dir = tmp_path / "market"
    assert (
        cli.research_boq_market(
            research_dir=research_dir,
            profile_path=profile_path,
            output_dir=output_dir,
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "BLOCKED"
    manifest = BoqMarketResearchPackage.model_validate_json(
        (output_dir / "manifest.json").read_bytes()
    )
    assert len(tuple((output_dir / "raw").glob("*.bin"))) == len(manifest.raw_responses)

    assert (
        cli.research_boq_market(
            research_dir=research_dir,
            profile_path=profile_path,
            output_dir=output_dir,
        )
        == 2
    )
    assert json.loads(capsys.readouterr().out)["code"] == (
        "BOQ_MARKET_RESEARCH_OUTPUT_ALREADY_EXISTS"
    )

    original_write = cli._write_bytes_exclusive

    def fail_manifest(destination: Path, payload: bytes) -> None:
        if destination.name == "manifest.json":
            raise OSError("simulated manifest failure")
        original_write(destination, payload)

    monkeypatch.setattr(cli, "_write_bytes_exclusive", fail_manifest)
    failed_output = tmp_path / "failed-market"
    assert (
        cli.research_boq_market(
            research_dir=research_dir,
            profile_path=profile_path,
            output_dir=failed_output,
        )
        == 2
    )
    assert json.loads(capsys.readouterr().out)["package_published"] is False
    assert not failed_output.exists()
    assert not tuple(tmp_path.glob(".failed-market.staging-*"))
