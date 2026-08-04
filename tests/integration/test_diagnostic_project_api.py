# ruff: noqa: RUF001

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tenderguard.api.main import create_app
from tenderguard.application.diagnostic_project import (
    DIAGNOSTIC_PROJECT_CURSOR,
    DiagnosticProject,
    DiagnosticProjectLoadError,
)
from tenderguard.application.free_source_research import (
    BoqFreeSourceLineRule,
    BoqFreeSourceResearchProfile,
    BoqResearchEvidenceReference,
    run_boq_free_source_research,
)
from tenderguard.config import Settings
from tenderguard.domain.boq_spreadsheet import (
    BoqCellEvidence,
    BoqRowCandidate,
    BoqXlsxExtractionResult,
)
from tenderguard.domain.common import canonical_json, content_hash
from tenderguard.infrastructure.database import (
    create_database_engine,
    create_schema_for_tests,
)


def _write_diagnostic_project(tmp_path: Path) -> Path:
    extraction = BoqXlsxExtractionResult(
        status="BLOCKED",
        profile_version_id="alabuga-test-profile-v1",
        profile_content_hash="a" * 64,
        root_object_sha256="b" * 64,
        workbook_object_sha256="c" * 64,
        archive_path="Алабуга.zip!/Ведомость объёмов работ.xlsx",
        worksheet_name="ВОР",
        candidates=(
            BoqRowCandidate(
                provisional_candidate_id="boq-candidate-" + "d" * 24,
                worksheet_name="ВОР",
                row_number=9,
                source_position_id=None,
                description="Разработка грунта",
                unit="1000 м3",
                quantity=Decimal("0.6912"),
                cells={
                    "description": BoqCellEvidence(
                        coordinate="B9",
                        value_kind="TEXT",
                        source_literal="Разработка грунта",
                    ),
                    "quantity": BoqCellEvidence(
                        coordinate="D9",
                        value_kind="NUMBER",
                        source_literal="0.6912",
                    ),
                },
                blockers=(
                    "MISSING_STABLE_POSITION_ID",
                    "POSITION_ID_FORMULA_NOT_ALLOWED",
                ),
            ),
        ),
        global_blockers=("INTAKE_MANIFEST_BLOCKED",),
        extracted_at=datetime(2026, 7, 31, 12, 0, tzinfo=UTC),
    )
    extraction_path = tmp_path / "alabuga-extraction.json"
    extraction_payload = extraction.model_dump_json(indent=2).encode("utf-8")
    extraction_path.write_bytes(extraction_payload)
    manifest_path = tmp_path / "alabuga-project.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "tenderguard.diagnostic-project/v1",
                "project_id": "diagnostic-alabuga-4527946",
                "organization_id": "org-1",
                "code": "АЛАБУГА-4527946",
                "name": "Алабуга 4527946 — диагностический импорт",
                "extraction_path": extraction_path.name,
                "extraction_sha256": hashlib.sha256(extraction_payload).hexdigest(),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return manifest_path


def _write_researched_diagnostic_project(tmp_path: Path) -> Path:
    manifest_path = _write_diagnostic_project(tmp_path)
    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    extraction_path = tmp_path / manifest_payload["extraction_path"]
    extraction = BoqXlsxExtractionResult.model_validate_json(
        extraction_path.read_text(encoding="utf-8")
    )
    candidate = extraction.candidates[0]
    profile = BoqFreeSourceResearchProfile(
        profile_version_id="diagnostic-research-v1",
        project_code="ALABUGA-TEST",
        subject_name="Республика Татарстан",
        expected_extraction_content_hash=content_hash(extraction),
        expected_workbook_sha256=extraction.workbook_object_sha256,
        context_evidence=(
            BoqResearchEvidenceReference(
                label="Project title page",
                object_sha256="e" * 64,
                source_locator="project.pdf#page=1",
            ),
        ),
        line_rules=(
            BoqFreeSourceLineRule(
                candidate_id=candidate.provisional_candidate_id,
                cost_nature="WORK",
            ),
        ),
    )

    def unexpected_fgis_acquisition(_query: str):
        raise AssertionError("Work-only diagnostic research must not call FGIS")

    research = run_boq_free_source_research(
        extraction=extraction,
        profile=profile,
        acquire_ksr_search=unexpected_fgis_acquisition,
        completed_at=datetime(2026, 8, 4, 9, 0, tzinfo=UTC),
    )
    research_dir = tmp_path / "research"
    research_dir.mkdir()
    research_payload = canonical_json(research.result) + b"\n"
    (research_dir / "manifest.json").write_bytes(research_payload)
    manifest_payload.update(
        {
            "schema_version": "tenderguard.diagnostic-project/v2",
            "research": {
                "free_source_research": {
                    "path": "research/manifest.json",
                    "sha256": hashlib.sha256(research_payload).hexdigest(),
                }
            },
        }
    )
    manifest_path.write_text(
        json.dumps(manifest_payload, ensure_ascii=False),
        encoding="utf-8",
    )
    return manifest_path


def _settings(tmp_path: Path, manifest_path: Path) -> Settings:
    return Settings(
        app_env="test",
        database_url="sqlite+pysqlite://",
        local_object_store_path=tmp_path / "objects",
        local_quarantine_store_path=tmp_path / "quarantine",
        allow_insecure_dev_auth=True,
        audit_signing_key="diagnostic-test-audit-signing-key-32-bytes",
        diagnostic_project_manifest_path=manifest_path,
    )


def _headers(*, organization_id: str = "org-1") -> dict[str, str]:
    return {
        "X-Dev-Actor": "estimator-a",
        "X-Dev-Organization": organization_id,
        "X-Dev-Roles": "ESTIMATOR",
    }


def test_real_extraction_shape_is_visible_but_every_financial_value_is_blocked(
    tmp_path: Path,
) -> None:
    manifest_path = _write_diagnostic_project(tmp_path)
    settings = _settings(tmp_path, manifest_path)
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)

    with TestClient(create_app(settings, engine=engine)) as client:
        portfolio = client.get("/v1/projects", headers=_headers())
        assert portfolio.status_code == 200, portfolio.text
        item = portfolio.json()["items"][0]
        assert item["project"]["id"] == "diagnostic-alabuga-4527946"
        assert item["project"]["state"] == "BLOCKED"
        assert item["latest_total"] is None

        workbench = client.get(
            "/v1/projects/diagnostic-alabuga-4527946/workbench",
            headers=_headers(),
        )
        assert workbench.status_code == 200, workbench.text
        body = workbench.json()
        assert body["release_decision"]["allowed"] is False
        assert body["release_decision"]["resulting_state"] == "BLOCKED"
        assert body["latest_total"] is None
        assert next(
            metric for metric in body["metrics"] if metric["code"] == "EXTRACTED_ROWS"
        ) == {
            "code": "EXTRACTED_ROWS",
            "label": "Извлечённые строки",
            "value": 1,
            "blocking": 1,
        }

        matrix = client.get(
            "/v1/projects/diagnostic-alabuga-4527946/boq/pricing-matrix",
            headers=_headers(),
        )
        assert matrix.status_code == 200, matrix.text
        matrix_body = matrix.json()
        assert matrix_body["blocked_row_count"] == 1
        assert len(matrix_body["rows"]) == 1
        row = matrix_body["rows"][0]
        assert row["boq_item_name"] == "Разработка грунта"
        assert row["quantity"] == "0.6912"
        assert row["quantity_status"] == "UNVERIFIED"
        assert row["row_status"] == "BLOCKED"
        assert row["name_match"] is None
        assert row["won_tender_prices"] == []
        assert row["fgis_cs_prices"] == []
        assert row["market_prices"] == []
        assert row["proposed_price"]["workflow_status"] == "DIAGNOSTIC_ONLY"
        assert row["proposed_price"]["amount_per_unit"] is None
        assert "CONTROLLED_IMPORT_WORKFLOW_REQUIRED" in row["blockers"]
        assert "PRICE_DECISION_MISSING" in row["blockers"]


def test_hash_pinned_research_route_is_visible_but_cannot_become_a_price(
    tmp_path: Path,
) -> None:
    manifest_path = _write_researched_diagnostic_project(tmp_path)
    settings = _settings(tmp_path, manifest_path)
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)

    with TestClient(create_app(settings, engine=engine)) as client:
        matrix = client.get(
            "/v1/projects/diagnostic-alabuga-4527946/boq/pricing-matrix",
            headers=_headers(),
        )

    assert matrix.status_code == 200, matrix.text
    row = matrix.json()["rows"][0]
    assert row["research_route"]["cost_nature"] == "WORK"
    assert row["research_route"]["pricing_route"] == "NORMATIVE_ENGINE"
    assert row["research_route"]["status"] == "BLOCKED"
    assert row["won_tender_research_candidates"] == []
    assert row["fgis_cs_research_candidates"] == []
    assert row["market_research_candidates"] == []
    assert row["won_tender_prices"] == []
    assert row["fgis_cs_prices"] == []
    assert row["market_prices"] == []
    assert row["proposed_price"]["amount_per_unit"] is None


def test_hash_pinned_research_manifest_drift_stops_application_startup(
    tmp_path: Path,
) -> None:
    manifest_path = _write_researched_diagnostic_project(tmp_path)
    research_path = tmp_path / "research" / "manifest.json"
    research_path.write_bytes(research_path.read_bytes() + b" ")

    with pytest.raises(
        DiagnosticProjectLoadError,
        match="differs from the hash-pinned project manifest",
    ):
        DiagnosticProject.load(manifest_path, max_extraction_bytes=1024 * 1024)


def test_diagnostic_project_is_organization_scoped_and_not_listed_to_outsiders(
    tmp_path: Path,
) -> None:
    manifest_path = _write_diagnostic_project(tmp_path)
    settings = _settings(tmp_path, manifest_path)
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)

    with TestClient(create_app(settings, engine=engine)) as client:
        portfolio = client.get(
            "/v1/projects",
            headers=_headers(organization_id="other-org"),
        )
        assert portfolio.status_code == 200
        assert portfolio.json()["items"] == []

        project = client.get(
            "/v1/projects/diagnostic-alabuga-4527946",
            headers=_headers(organization_id="other-org"),
        )
        assert project.status_code == 404


def test_diagnostic_project_hash_mismatch_stops_application_startup(
    tmp_path: Path,
) -> None:
    manifest_path = _write_diagnostic_project(tmp_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["extraction_sha256"] = "f" * 64
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        DiagnosticProjectLoadError,
        match="differs from the hash-pinned manifest",
    ):
        DiagnosticProject.load(manifest_path, max_extraction_bytes=1024 * 1024)


def test_missing_diagnostic_manifest_stops_application_startup(tmp_path: Path) -> None:
    with pytest.raises(
        DiagnosticProjectLoadError,
        match="manifest does not exist",
    ):
        DiagnosticProject.load(
            tmp_path / "missing-project.json",
            max_extraction_bytes=1024 * 1024,
        )


def test_diagnostic_project_is_rejected_outside_development_and_test(
    tmp_path: Path,
) -> None:
    manifest_path = _write_diagnostic_project(tmp_path)
    with pytest.raises(ValidationError, match="diagnostic project is configured"):
        Settings(
            app_env="production",
            diagnostic_project_manifest_path=manifest_path,
        )


def test_limit_one_cursor_does_not_drop_governed_projects(tmp_path: Path) -> None:
    manifest_path = _write_diagnostic_project(tmp_path)
    settings = _settings(tmp_path, manifest_path)
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)

    with TestClient(create_app(settings, engine=engine)) as client:
        created = client.post(
            "/v1/projects",
            headers={**_headers(), "Idempotency-Key": "create-project-after-diagnostic"},
            json={
                "code": "GOVERNED-1",
                "name": "Управляемый проект",
                "reason": "Проверка пагинации диагностического проекта",
            },
        )
        assert created.status_code == 201, created.text

        first = client.get("/v1/projects?limit=1", headers=_headers())
        assert first.status_code == 200, first.text
        assert first.json()["items"][0]["project"]["id"] == (
            "diagnostic-alabuga-4527946"
        )
        assert first.json()["next_cursor"] == DIAGNOSTIC_PROJECT_CURSOR

        second = client.get(
            "/v1/projects",
            params={"limit": 1, "cursor": first.json()["next_cursor"]},
            headers=_headers(),
        )
        assert second.status_code == 200, second.text
        assert second.json()["items"][0]["project"]["id"] == created.json()["id"]
