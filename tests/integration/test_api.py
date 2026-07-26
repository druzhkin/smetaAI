from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from tenderguard.api.main import create_app
from tenderguard.application.document_processing import DocumentProcessingService
from tenderguard.application.pricing import (
    NomenclatureAssessmentDraft,
    NormalizePriceCommand,
    PriceQuoteDraft,
    PricingService,
)
from tenderguard.config import Settings
from tenderguard.domain.common import content_hash
from tenderguard.domain.enums import (
    ActorRole,
    EvidenceMethod,
    PriceEvidenceClass,
    VatBasis,
    VerificationStatus,
)
from tenderguard.domain.models import (
    CommercialBasis,
    EvidenceLocation,
    Observation,
)
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.database import (
    create_database_engine,
    create_schema_for_tests,
    create_session_factory,
)
from tenderguard.infrastructure.object_store import LocalObjectStore
from tenderguard.infrastructure.orm import (
    AdapterQualificationRow,
    ApprovalTaskRow,
    BoqLineRow,
    CalculationSnapshotRow,
    ObservationRow,
    PriceDecisionRow,
    QuantityRow,
    RiskCalculationRow,
    RiskItemRow,
)


def _intake_settings(tmp_path: Path) -> Settings:
    return Settings(
        app_env="test",
        database_url="sqlite+pysqlite://",
        local_object_store_path=tmp_path / "objects",
        local_quarantine_store_path=tmp_path / "quarantine",
        allow_insecure_dev_auth=True,
        audit_signing_key="test-audit-signing-key-at-least-32-bytes",
        malware_scanner_adapter="qualified-test-scanner",
        malware_scanner_qualification_id="qualification-malware-test",
        document_processor_adapter="qualified-test-intake",
        document_processor_qualification_id="qualification-intake-test",
        document_worker_actor_id="document-worker",
    )


def _seed_intake_qualifications(engine: Engine) -> None:
    now = datetime.now(UTC)
    with create_session_factory(engine).begin() as session:
        session.add_all(
            [
                AdapterQualificationRow(
                    id="qualification-malware-test",
                    adapter_name="qualified-test-scanner",
                    adapter_version="test-1",
                    status="APPROVED",
                    valid_until=None,
                    test_evidence_hash="a" * 64,
                    payload={
                        "organization_id": "org-1",
                        "supported_methods": ["MALWARE_SCAN"],
                        "independence_domain": "test-malware-engine",
                        "service_actor_id": "malware-scanner",
                    },
                    approved_by="methodology-owner-b",
                    approved_at=now,
                ),
                AdapterQualificationRow(
                    id="qualification-intake-test",
                    adapter_name="qualified-test-intake",
                    adapter_version="test-1",
                    status="APPROVED",
                    valid_until=None,
                    test_evidence_hash="b" * 64,
                    payload={
                        "organization_id": "org-1",
                        "supported_methods": ["DOCUMENT_INTAKE"],
                        "independence_domain": "test-isolated-intake-worker",
                        "service_actor_id": "document-worker",
                    },
                    approved_by="methodology-owner-b",
                    approved_at=now,
                ),
            ]
        )


def _scan_and_process_upload(
    *,
    client: TestClient,
    engine: Engine,
    settings: Settings,
    upload: dict[str, object],
) -> dict[str, object]:
    object_hash = str(upload["object_hash"])
    report = {
        "engine": "qualified-test-scanner",
        "object_hash": object_hash,
        "verdict": "CLEAN",
    }
    system_headers = {
        "X-Dev-Actor": "malware-scanner",
        "X-Dev-Organization": "org-1",
        "X-Dev-Roles": "SYSTEM",
    }
    scan = client.post(
        (
            f"/v1/projects/{upload['project_id']}/document-uploads/"
            f"{upload['upload_id']}/scan-results"
        ),
        headers=system_headers,
        json={
            "result": {
                "scanner_run_id": f"scan-{upload['upload_id']}",
                "adapter_qualification_id": "qualification-malware-test",
                "scanned_object_hash": object_hash,
                "verdict": "CLEAN",
                "definitions_version": "test-definitions-1",
                "detected_threats": [],
                "report": report,
                "report_hash": content_hash(report),
                "completed_at": datetime.now(UTC).isoformat(),
            },
            "reason": "Test-qualified malware scan completed",
        },
    )
    assert scan.status_code == 200, scan.text
    assert scan.json()["status"] == "CLEAN"
    result = DocumentProcessingService(
        session_factory=create_session_factory(engine),
        settings=settings,
        evidence_store=LocalObjectStore(settings.local_object_store_path),
        quarantine_store=LocalObjectStore(settings.local_quarantine_store_path),
    ).process(
        actor=Actor(
            actor_id="document-worker",
            organization_id="org-1",
            roles=frozenset({ActorRole.SYSTEM}),
        ),
        upload_id=str(upload["upload_id"]),
        request_id=f"worker-{upload['upload_id']}",
        reason="Execute in the test worker boundary",
    )
    payload = result.model_dump(mode="json")
    payload["document_id"] = payload["processed_document_id"]
    payload["document_revision_id"] = payload["processed_document_revision_id"]
    return payload


def test_api_scopes_projects_and_exposes_fail_closed_release_gates(tmp_path: Path) -> None:
    settings = Settings(
        app_env="test",
        database_url="sqlite+pysqlite://",
        local_object_store_path=tmp_path / "objects",
        allow_insecure_dev_auth=True,
        audit_signing_key="test-audit-signing-key-at-least-32-bytes",
    )
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    app = create_app(
        settings,
        engine=engine,
        object_store=LocalObjectStore(tmp_path / "objects"),
    )
    estimator_headers = {
        "X-Dev-Actor": "estimator-1",
        "X-Dev-Organization": "org-1",
        "X-Dev-Roles": "ESTIMATOR",
    }
    with TestClient(app) as client:
        response = client.post(
            "/v1/projects",
            headers=estimator_headers,
            json={
                "code": "T-2026-001",
                "name": "Water network tender",
                "reason": "Tender registered",
            },
        )
        assert response.status_code == 201
        project = response.json()
        project_id = project["id"]
        assert project["state"] == "DRAFT"
        assert response.headers["X-Request-Id"].startswith("request-")

        gates = client.get(
            f"/v1/projects/{project_id}/release-gates",
            headers=estimator_headers,
        )
        assert gates.status_code == 200
        decision = gates.json()["decision"]
        assert not decision["allowed"]
        codes = {finding["code"] for finding in decision["findings"]}
        assert "CURRENT_DOCUMENT_SET_NOT_CONFIRMED" in codes
        assert "NORMATIVE_ENGINE_UNAVAILABLE" in codes
        assert "PRODUCTION_QUALIFICATION_INCOMPLETE" in codes

        other_org = client.get(
            f"/v1/projects/{project_id}",
            headers={
                **estimator_headers,
                "X-Dev-Organization": "org-2",
            },
        )
        assert other_org.status_code == 404

        admin_release = client.post(
            f"/v1/projects/{project_id}/release/bid",
            headers={
                **estimator_headers,
                "X-Dev-Actor": "infrastructure-admin",
                "X-Dev-Roles": "ADMIN",
            },
            json={
                "expected_row_version": project["row_version"],
                "gate_hash": gates.json()["gate_hash"],
                "reason": "Infrastructure administrator must not release a bid",
            },
        )
        assert admin_release.status_code == 403


def test_corrupt_archive_moves_draft_project_to_documents_incomplete(
    tmp_path: Path,
) -> None:
    settings = _intake_settings(tmp_path)
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    _seed_intake_qualifications(engine)
    quarantine_store = LocalObjectStore(tmp_path / "quarantine")
    app = create_app(
        settings,
        engine=engine,
        object_store=LocalObjectStore(tmp_path / "objects"),
        quarantine_store=quarantine_store,
    )
    headers = {
        "X-Dev-Actor": "estimator-1",
        "X-Dev-Organization": "org-1",
        "X-Dev-Roles": "ESTIMATOR",
    }
    with TestClient(app) as client:
        admin_governance = client.post(
            "/v1/governance/versions",
            headers={
                "X-Dev-Actor": "infrastructure-admin",
                "X-Dev-Organization": "org-1",
                "X-Dev-Roles": "ADMIN",
            },
            json={
                "kind": "calculation_model",
                "version_label": "unauthorised",
                "payload": {},
                "reason": "Infrastructure administrator must not own methodology",
            },
        )
        assert admin_governance.status_code == 403
        project = client.post(
            "/v1/projects",
            headers=headers,
            json={"code": "T-2", "name": "Broken input", "reason": "Register"},
        ).json()
        upload_headers = {
            **headers,
            "Idempotency-Key": "corrupt-archive-upload-0001",
        }
        upload_form = {
            "logical_key": "tender-package",
            "title": "Tender package",
            "document_type": "TENDER_PACKAGE",
            "revision_label": "1",
            "reason": "Initial upload",
            "critical": "true",
        }
        response = client.post(
            f"/v1/projects/{project['id']}/documents",
            headers=upload_headers,
            data=upload_form,
            files={"upload": ("package.zip", b"not-a-valid-zip", "application/zip")},
        )
        assert response.status_code == 202
        payload = response.json()
        assert payload["status"] == "QUARANTINED"
        replay = client.post(
            f"/v1/projects/{project['id']}/documents",
            headers=upload_headers,
            data=upload_form,
            files={"upload": ("package.zip", b"not-a-valid-zip", "application/zip")},
        )
        assert replay.status_code == 202
        assert replay.json() == payload
        assert replay.headers["Idempotency-Replayed"] == "true"
        processed = _scan_and_process_upload(
            client=client,
            engine=engine,
            settings=settings,
            upload=payload,
        )
        assert processed["status"] == "PROCESSED"
        manifest = processed["manifest"]
        assert isinstance(manifest, dict)
        assert not manifest["all_files_processed"]
        assert any(finding["code"] == "CORRUPT_ARCHIVE" for finding in manifest["findings"])
        project_state = client.get(
            f"/v1/projects/{project['id']}",
            headers=headers,
        ).json()["state"]
        assert project_state == "DOCUMENTS_INCOMPLETE"


def test_quarantine_fails_closed_before_scan_and_rejects_forged_or_infected_results(
    tmp_path: Path,
) -> None:
    settings = _intake_settings(tmp_path)
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    _seed_intake_qualifications(engine)
    evidence_store = LocalObjectStore(tmp_path / "objects")
    quarantine_store = LocalObjectStore(tmp_path / "quarantine")
    app = create_app(
        settings,
        engine=engine,
        object_store=evidence_store,
        quarantine_store=quarantine_store,
    )
    estimator = {
        "X-Dev-Actor": "estimator-1",
        "X-Dev-Organization": "org-1",
        "X-Dev-Roles": "ESTIMATOR",
    }
    system = {
        "X-Dev-Actor": "malware-scanner",
        "X-Dev-Organization": "org-1",
        "X-Dev-Roles": "SYSTEM",
    }
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            headers=estimator,
            json={"code": "T-Q", "name": "Quarantine test", "reason": "Register"},
        ).json()
        invalid_logical_key = client.post(
            f"/v1/projects/{project['id']}/documents",
            headers=estimator,
            data={
                "logical_key": "terms invalid",
                "title": "Invalid logical key",
                "document_type": "TENDER_TERMS",
                "revision_label": "1",
                "reason": "Must fail before storing untrusted bytes",
                "critical": "true",
            },
            files={"upload": ("invalid.txt", b"must not persist", "text/plain")},
        )
        assert invalid_logical_key.status_code == 422
        assert not [path for path in (tmp_path / "quarantine").rglob("*") if path.is_file()]

        response = client.post(
            f"/v1/projects/{project['id']}/documents",
            headers=estimator,
            data={
                "logical_key": "terms",
                "title": "Tender terms",
                "document_type": "TENDER_TERMS",
                "revision_label": "1",
                "reason": "Submit untrusted document",
                "critical": "true",
            },
            files={"upload": ("terms.txt", b"untrusted input", "text/plain")},
        )
        assert response.status_code == 202
        upload = response.json()
        assert upload["status"] == "QUARANTINED"
        assert (
            client.get(f"/v1/projects/{project['id']}", headers=estimator).json()["state"]
            == "DOCUMENTS_INCOMPLETE"
        )
        assert not [path for path in (tmp_path / "objects").rglob("*") if path.is_file()]
        assert len([path for path in (tmp_path / "quarantine").rglob("*") if path.is_file()]) == 1
        duplicate_pending = client.post(
            f"/v1/projects/{project['id']}/documents",
            headers=estimator,
            data={
                "logical_key": "terms",
                "title": "Tender terms",
                "document_type": "TENDER_TERMS",
                "revision_label": "2",
                "reason": "Must not overtake an unresolved upload",
                "critical": "true",
            },
            files={"upload": ("terms-v2.txt", b"second input", "text/plain")},
        )
        assert duplicate_pending.status_code == 422
        assert "unresolved quarantined upload" in duplicate_pending.json()["detail"]

        report = {
            "engine": "qualified-test-scanner",
            "object_hash": upload["object_hash"],
            "verdict": "CLEAN",
        }
        scan_request = {
            "result": {
                "scanner_run_id": "scan-forged",
                "adapter_qualification_id": "qualification-malware-test",
                "scanned_object_hash": upload["object_hash"],
                "verdict": "CLEAN",
                "definitions_version": "test-definitions-1",
                "detected_threats": [],
                "report": report,
                "report_hash": "f" * 64,
                "completed_at": datetime.now(UTC).isoformat(),
            },
            "reason": "Attempt to submit forged scanner evidence",
        }
        unauthorized = client.post(
            (f"/v1/projects/{project['id']}/document-uploads/{upload['upload_id']}/scan-results"),
            headers=estimator,
            json=scan_request,
        )
        assert unauthorized.status_code == 403
        forged = client.post(
            (f"/v1/projects/{project['id']}/document-uploads/{upload['upload_id']}/scan-results"),
            headers=system,
            json=scan_request,
        )
        assert forged.status_code == 422
        assert "does not reproduce" in forged.json()["detail"]

        infected_report = {
            "engine": "qualified-test-scanner",
            "object_hash": upload["object_hash"],
            "verdict": "INFECTED",
            "threats": ["Test.Malware"],
        }
        infected = client.post(
            (f"/v1/projects/{project['id']}/document-uploads/{upload['upload_id']}/scan-results"),
            headers=system,
            json={
                "result": {
                    "scanner_run_id": "scan-infected",
                    "adapter_qualification_id": "qualification-malware-test",
                    "scanned_object_hash": upload["object_hash"],
                    "verdict": "INFECTED",
                    "definitions_version": "test-definitions-1",
                    "detected_threats": ["Test.Malware"],
                    "report": infected_report,
                    "report_hash": content_hash(infected_report),
                    "completed_at": datetime.now(UTC).isoformat(),
                },
                "reason": "Record malware detection",
            },
        )
        assert infected.status_code == 200, infected.text
        assert infected.json()["status"] == "REJECTED"
        assert infected.json()["failure_code"] == "MALWARE_DETECTED"

        with pytest.raises(ValueError, match="Only a qualified CLEAN"):
            DocumentProcessingService(
                session_factory=create_session_factory(engine),
                settings=settings,
                evidence_store=evidence_store,
                quarantine_store=quarantine_store,
            ).process(
                actor=Actor(
                    actor_id="document-worker",
                    organization_id="org-1",
                    roles=frozenset({ActorRole.SYSTEM}),
                ),
                upload_id=upload["upload_id"],
                request_id="worker-rejected",
                reason="Must not process an infected upload",
            )
    assert not [path for path in (tmp_path / "objects").rglob("*") if path.is_file()]


def test_governed_calculation_creates_independently_validated_snapshot(
    tmp_path: Path,
) -> None:
    settings = _intake_settings(tmp_path)
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    _seed_intake_qualifications(engine)
    app = create_app(
        settings,
        engine=engine,
        object_store=LocalObjectStore(tmp_path / "objects"),
        quarantine_store=LocalObjectStore(tmp_path / "quarantine"),
    )
    operator = {
        "X-Dev-Actor": "operator-1",
        "X-Dev-Organization": "org-1",
        "X-Dev-Roles": "ESTIMATOR,PROCUREMENT,TECHNICAL_EXPERT,REVIEWER",
    }
    owner_a = {
        "X-Dev-Actor": "owner-a",
        "X-Dev-Organization": "org-1",
        "X-Dev-Roles": "METHODOLOGY_OWNER",
    }
    owner_b = {
        "X-Dev-Actor": "owner-b",
        "X-Dev-Organization": "org-1",
        "X-Dev-Roles": "METHODOLOGY_OWNER,REVIEWER",
    }
    catalog_owner_a = {
        "X-Dev-Actor": "catalog-owner-a",
        "X-Dev-Organization": "org-1",
        "X-Dev-Roles": "CATALOG_OWNER",
    }
    catalog_owner_b = {
        "X-Dev-Actor": "catalog-owner-b",
        "X-Dev-Organization": "org-1",
        "X-Dev-Roles": "CATALOG_OWNER",
    }
    system = {
        "X-Dev-Actor": "extractor-service",
        "X-Dev-Organization": "org-1",
        "X-Dev-Roles": "SYSTEM",
    }
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            headers=operator,
            json={"code": "T-CALC", "name": "Calculated tender", "reason": "Register"},
        ).json()
        for principal_id, roles in (
            ("owner-a", ["METHODOLOGY_OWNER"]),
            ("owner-b", ["METHODOLOGY_OWNER", "REVIEWER"]),
            ("catalog-owner-a", ["CATALOG_OWNER"]),
            ("catalog-owner-b", ["CATALOG_OWNER"]),
        ):
            membership = client.post(
                f"/v1/projects/{project['id']}/members",
                headers=operator,
                json={
                    "principal_id": principal_id,
                    "roles": roles,
                    "access_level": "MEMBER",
                    "reason": "Assign explicit project-scoped workflow roles",
                },
            )
            assert membership.status_code == 200, membership.text
        uploaded = client.post(
            f"/v1/projects/{project['id']}/documents",
            headers=operator,
            data={
                "logical_key": "terms",
                "title": "Tender terms",
                "document_type": "TENDER_TERMS",
                "revision_label": "1",
                "reason": "Upload current terms",
                "critical": "true",
            },
            files={"upload": ("terms.txt", b"current tender terms", "text/plain")},
        )
        assert uploaded.status_code == 202
        uploaded_payload = _scan_and_process_upload(
            client=client,
            engine=engine,
            settings=settings,
            upload=uploaded.json(),
        )
        candidate_id = uploaded_payload["candidate_document_set_revision_id"]
        candidate = client.get(
            f"/v1/projects/{project['id']}/document-sets/{candidate_id}",
            headers=owner_b,
        )
        assert candidate.status_code == 200, candidate.text
        assert candidate.json()["status"] == "DRAFT"
        assert candidate.json()["created_by"] == "operator-1"
        assert candidate.json()["revision_ids"] == [uploaded_payload["document_revision_id"]]
        document_records = client.get(
            f"/v1/projects/{project['id']}/records",
            headers=owner_b,
            params={"section": "DOCUMENTS", "limit": 100},
        )
        assert document_records.status_code == 200, document_records.text
        candidate_record = next(
            item
            for item in document_records.json()["items"]
            if item["kind"] == "DOCUMENT_SET_REVISION" and item["id"] == candidate_id
        )
        assert candidate_record["status"] == "DRAFT"
        assert candidate_record["attributes"]["manifest_hash"] == candidate.json()["manifest_hash"]
        same_actor_confirmation = client.post(
            f"/v1/projects/{project['id']}/document-set/confirm",
            headers=operator,
            json={
                "candidate_document_set_revision_id": candidate_id,
                "reason": "The submitting actor must not self-confirm the current set",
            },
        )
        assert same_actor_confirmation.status_code == 422
        assert "different from the submitter" in same_actor_confirmation.json()["detail"]
        confirmed = client.post(
            f"/v1/projects/{project['id']}/document-set/confirm",
            headers=owner_b,
            json={
                "candidate_document_set_revision_id": candidate_id,
                "reason": "Current revision checked",
            },
        )
        assert confirmed.status_code == 200
        confirmed_candidate = client.get(
            f"/v1/projects/{project['id']}/document-sets/{candidate_id}",
            headers=owner_b,
        ).json()
        assert confirmed_candidate["status"] == "CONFIRMED"
        assert confirmed_candidate["confirmed_by"] == "owner-b"

        def approve_version(
            kind: str,
            label: str,
            version_payload: dict[str, object],
            *,
            purpose: str | None = None,
        ) -> dict[str, object]:
            is_catalog_content = kind in {
                "catalog",
                "nomenclature_catalog",
                "nomenclature_equivalence_rules",
                "equivalence_rules",
            }
            created = client.post(
                "/v1/governance/versions",
                headers=catalog_owner_a if is_catalog_content else owner_a,
                json={
                    "kind": kind,
                    "version_label": label,
                    "payload": version_payload,
                    "reason": f"Register {kind}",
                },
            )
            assert created.status_code == 201, created.text
            controlled = created.json()
            approved = client.post(
                f"/v1/governance/versions/{controlled['version_id']}/approve",
                headers=catalog_owner_b if is_catalog_content else owner_b,
                json={"reason": f"Independently review {kind}"},
            )
            assert approved.status_code == 200, approved.text
            if purpose:
                bound = client.post(
                    f"/v1/projects/{project['id']}/controlled-versions/bind",
                    headers=catalog_owner_b if is_catalog_content else owner_b,
                    json={
                        "version_id": controlled["version_id"],
                        "purpose": purpose,
                        "reason": f"Bind approved {kind}",
                    },
                )
                assert bound.status_code == 204, bound.text
            return controlled

        version = approve_version(
            "calculation_model",
            "calc-1",
            {
                "policy": {
                    "currency": "RUB",
                    "line_rounding_scale": 2,
                    "total_rounding_scale": 2,
                    "rounding_mode": "ROUND_HALF_UP",
                    "independent_tolerance": "0",
                }
            },
            purpose="calculation_model",
        )
        invalid_service_adapter = approve_version(
            "adapter_qualification",
            "invalid-service-identity-1",
            {
                "adapter_name": "invalid-service-identity",
                "adapter_version": "1.0",
                "test_evidence_hash": "d" * 64,
                "valid_until": "2027-07-23",
                "supported_methods": ["TABLE_PARSER"],
                "independence_domain": "invalid-service-identity",
                "service_actor_id": " extractor-service",
            },
        )
        invalid_activation = client.post(
            (
                f"/v1/governance/versions/"
                f"{invalid_service_adapter['version_id']}/activate-adapter-qualification"
            ),
            headers=owner_b,
            json={"reason": "Reject ambiguous runtime identity binding"},
        )
        assert invalid_activation.status_code == 422
        parser_adapter = approve_version(
            "adapter_qualification",
            "table-parser-1",
            {
                "adapter_name": "qualified-table-parser",
                "adapter_version": "1.0",
                "test_evidence_hash": "e" * 64,
                "valid_until": "2027-07-23",
                "supported_methods": ["TABLE_PARSER"],
                "supported_price_evidence_classes": [item.value for item in PriceEvidenceClass],
                "independence_domain": "deterministic-table-parser",
                "service_actor_id": "extractor-service",
            },
        )
        visual_adapter = approve_version(
            "adapter_qualification",
            "visual-model-1",
            {
                "adapter_name": "qualified-visual-model",
                "adapter_version": "1.0",
                "test_evidence_hash": "f" * 64,
                "valid_until": "2027-07-23",
                "supported_methods": ["VISUAL_MODEL"],
                "supported_price_evidence_classes": [item.value for item in PriceEvidenceClass],
                "independence_domain": "isolated-visual-provider",
                "service_actor_id": "extractor-service",
            },
        )
        for adapter in (parser_adapter, visual_adapter):
            activated = client.post(
                (f"/v1/governance/versions/{adapter['version_id']}/activate-adapter-qualification"),
                headers=owner_b,
                json={"reason": "Activate independently approved adapter qualification"},
            )
            assert activated.status_code == 200, activated.text
        reconciliation_rules = approve_version(
            "reconciliation_rules",
            "reconciliation-1",
            {"mode": "EXACT_INDEPENDENT_AGREEMENT"},
            purpose="reconciliation_rules",
        )

        invalid_basis_draft = {
            "field_name": "normalized_unit_rate",
            "value": "125.50",
            "unit": "m",
            "method": "TABLE_PARSER",
            "method_version": "1.0",
            "source_priority": 1,
            "location": {
                "document_id": uploaded_payload["document_id"],
                "document_revision_id": uploaded_payload["document_revision_id"],
                "original_object_hash": uploaded_payload["manifest"]["root_sha256"],
                "locator_kind": "structured_region",
                "locator": "table:prices[row=invalid]",
            },
            "observed_at": "2026-07-23T09:59:00Z",
            "adapter_qualification_id": parser_adapter["version_id"],
        }
        reserved_basis_key = client.post(
            f"/v1/projects/{project['id']}/evidence/observations",
            headers=system,
            json={
                "draft": {
                    **invalid_basis_draft,
                    "basis_metadata": {
                        "basis_type": "NORMALIZED_PRICE",
                        "currency": "RUB",
                        "unit": "m",
                        "observation": "attempted-reserved-payload-overwrite",
                    },
                },
                "reason": "Reserved payload keys must fail closed",
            },
        )
        assert reserved_basis_key.status_code == 422
        assert "Unsupported evidence basis metadata" in reserved_basis_key.text
        inconsistent_basis_unit = client.post(
            f"/v1/projects/{project['id']}/evidence/observations",
            headers=system,
            json={
                "draft": {
                    **invalid_basis_draft,
                    "basis_metadata": {
                        "basis_type": "NORMALIZED_PRICE",
                        "currency": "RUB",
                        "unit": "kg",
                    },
                },
                "reason": "Commercial basis must match the observed unit",
            },
        )
        assert inconsistent_basis_unit.status_code == 422
        assert "must match the observation unit" in inconsistent_basis_unit.text

        observation_ids: list[str] = []
        for method, locator, adapter in (
            ("TABLE_PARSER", "table:prices[row=1]", parser_adapter),
            (
                "VISUAL_MODEL",
                "image-region:x1=10,y1=10,x2=50,y2=50",
                visual_adapter,
            ),
        ):
            observation_request = {
                "draft": {
                    "field_name": "normalized_unit_rate",
                    "value": "125.50",
                    "unit": "m",
                    "method": method,
                    "method_version": "1.0",
                    "source_priority": 1,
                    "location": {
                        "document_id": uploaded_payload["document_id"],
                        "document_revision_id": uploaded_payload["document_revision_id"],
                        "original_object_hash": uploaded_payload["manifest"]["root_sha256"],
                        "locator_kind": "structured_region",
                        "locator": locator,
                    },
                    "observed_at": "2026-07-23T10:00:00Z",
                    "adapter_qualification_id": adapter["version_id"],
                    "basis_metadata": {
                        "basis_type": "NORMALIZED_PRICE",
                        "currency": "RUB",
                        "unit": "m",
                    },
                },
                "reason": f"Record independent {method} observation",
            }
            if method == "TABLE_PARSER":
                spoofed = client.post(
                    f"/v1/projects/{project['id']}/evidence/observations",
                    headers=operator,
                    json=observation_request,
                )
                assert spoofed.status_code == 403
            recorded = client.post(
                f"/v1/projects/{project['id']}/evidence/observations",
                headers=system,
                json=observation_request,
            )
            assert recorded.status_code == 201, recorded.text
            observation_ids.append(recorded.json()["observation_id"])
        reconciliation_context = client.get(
            f"/v1/projects/{project['id']}/evidence/reconciliation-context",
            headers=owner_b,
            params={"field_name": "normalized_unit_rate", "limit": 100},
        )
        assert reconciliation_context.status_code == 200, reconciliation_context.text
        reconciliation_payload = reconciliation_context.json()
        assert (
            reconciliation_payload["reconciliation_version_id"]
            == (reconciliation_rules["version_id"])
        )
        assert (
            reconciliation_payload["document_set_revision_id"]
            == (confirmed.json()["current_document_set_revision_id"])
        )
        assert {
            candidate["observation"]["observation_id"]
            for candidate in reconciliation_payload["candidates"]
        } == set(observation_ids)
        assert all(
            candidate["eligible"] and candidate["adapter_status"] == "APPROVED"
            for candidate in reconciliation_payload["candidates"]
        )
        with create_session_factory(engine).begin() as session:
            tampered_row = session.get(ObservationRow, observation_ids[0])
            assert tampered_row is not None
            original_payload = tampered_row.payload
            tampered_observation = {
                **original_payload["observation"],
                "observation_id": "payload-identity-substitution",
            }
            tampered_row.payload = {
                **original_payload,
                "observation": tampered_observation,
            }
        tampered_context = client.get(
            f"/v1/projects/{project['id']}/evidence/reconciliation-context",
            headers=owner_b,
            params={"field_name": "normalized_unit_rate", "limit": 100},
        )
        assert tampered_context.status_code == 200, tampered_context.text
        tampered_candidate = next(
            candidate
            for candidate in tampered_context.json()["candidates"]
            if candidate["observation"]["observation_id"] == "payload-identity-substitution"
        )
        assert not tampered_candidate["eligible"]
        assert "EVIDENCE_INTEGRITY_FAILED" in tampered_candidate["blockers"]
        tampered_reconciliation = client.post(
            f"/v1/projects/{project['id']}/evidence/reconcile",
            headers=owner_b,
            json={
                "observation_ids": observation_ids,
                "reconciliation_version_id": reconciliation_rules["version_id"],
                "reason": "Reject a stored observation whose payload identity was substituted",
            },
        )
        assert tampered_reconciliation.status_code == 422
        with create_session_factory(engine).begin() as session:
            tampered_row = session.get(ObservationRow, observation_ids[0])
            assert tampered_row is not None
            tampered_row.payload = original_payload
        reconciled = client.post(
            f"/v1/projects/{project['id']}/evidence/reconcile",
            headers=owner_b,
            json={
                "observation_ids": observation_ids,
                "reconciliation_version_id": reconciliation_rules["version_id"],
                "reason": "Two qualified independent methods agree",
            },
        )
        assert reconciled.status_code == 200, reconciled.text
        verified_observation_id = reconciled.json()["verified_observation_id"]
        assert verified_observation_id
        clean_replay = client.post(
            f"/v1/projects/{project['id']}/evidence/reconcile",
            headers=owner_b,
            json={
                "observation_ids": observation_ids,
                "reconciliation_version_id": reconciliation_rules["version_id"],
                "reason": "Replay the same independently reproduced agreement",
            },
        )
        assert clean_replay.status_code == 200, clean_replay.text
        assert clean_replay.json()["verified_observation_id"] == verified_observation_id
        with create_session_factory(engine).begin() as session:
            derived_row = session.get(ObservationRow, verified_observation_id)
            assert derived_row is not None
            derived_row.field_name = "tampered-derived-field"
        corrupted_replay = client.post(
            f"/v1/projects/{project['id']}/evidence/reconcile",
            headers=owner_b,
            json={
                "observation_ids": observation_ids,
                "reconciliation_version_id": reconciliation_rules["version_id"],
                "reason": "Reject a corrupted stored deterministic reconciliation result",
            },
        )
        assert corrupted_replay.status_code == 422
        with create_session_factory(engine).begin() as session:
            derived_row = session.get(ObservationRow, verified_observation_id)
            assert derived_row is not None
            derived_row.field_name = "normalized_unit_rate"

        conflicting_visual = client.post(
            f"/v1/projects/{project['id']}/evidence/observations",
            headers=system,
            json={
                "draft": {
                    "field_name": "normalized_unit_rate",
                    "value": "130.00",
                    "unit": "m",
                    "method": "VISUAL_MODEL",
                    "method_version": "1.0",
                    "source_priority": 1,
                    "location": {
                        "document_id": uploaded_payload["document_id"],
                        "document_revision_id": uploaded_payload["document_revision_id"],
                        "original_object_hash": uploaded_payload["manifest"]["root_sha256"],
                        "locator_kind": "structured_region",
                        "locator": "image-region:conflicting-price",
                    },
                    "observed_at": "2026-07-23T10:01:00Z",
                    "adapter_qualification_id": visual_adapter["version_id"],
                    "basis_metadata": {
                        "basis_type": "NORMALIZED_PRICE",
                        "currency": "RUB",
                        "unit": "m",
                    },
                },
                "reason": "Record an intentionally conflicting visual extraction",
            },
        )
        assert conflicting_visual.status_code == 201, conflicting_visual.text
        conflicted = client.post(
            f"/v1/projects/{project['id']}/evidence/reconcile",
            headers=owner_b,
            json={
                "observation_ids": [
                    observation_ids[0],
                    conflicting_visual.json()["observation_id"],
                ],
                "reconciliation_version_id": reconciliation_rules["version_id"],
                "reason": "Detect disagreement without automatic merging",
            },
        )
        assert conflicted.status_code == 200, conflicted.text
        conflict_id = conflicted.json()["conflict"]["conflict_id"]
        with create_session_factory(engine).begin() as session:
            conflict_task = (
                session.query(ApprovalTaskRow)
                .filter(ApprovalTaskRow.entity_id == conflict_id)
                .one()
            )
            conflict_task.assigned_role = ActorRole.APPROVER.value
        corrupted_conflict_replay = client.post(
            f"/v1/projects/{project['id']}/evidence/reconcile",
            headers=owner_b,
            json={
                "observation_ids": [
                    observation_ids[0],
                    conflicting_visual.json()["observation_id"],
                ],
                "reconciliation_version_id": reconciliation_rules["version_id"],
                "reason": "Reject a conflict whose mandatory review task was corrupted",
            },
        )
        assert corrupted_conflict_replay.status_code == 422
        with create_session_factory(engine).begin() as session:
            conflict_task = (
                session.query(ApprovalTaskRow)
                .filter(ApprovalTaskRow.entity_id == conflict_id)
                .one()
            )
            conflict_task.assigned_role = ActorRole.REVIEWER.value
        creator_review = client.get(
            f"/v1/projects/{project['id']}/evidence/conflicts/{conflict_id}",
            headers=owner_b,
        )
        assert creator_review.status_code == 200, creator_review.text
        assert creator_review.json()["resolution_allowed"] is False
        assert "FOUR_EYES_TASK_CREATOR" in creator_review.json()["resolution_blockers"]
        conflict_task_id = creator_review.json()["task_id"]
        creator_resolution = client.post(
            f"/v1/projects/{project['id']}/evidence/conflicts/{conflict_id}/resolve",
            headers=owner_b,
            json={
                "selected_observation_id": observation_ids[0],
                "resolution_reason": "The conflict creator must not self-resolve",
                "expected_conflict_updated_at": creator_review.json()["conflict_updated_at"],
                "expected_task_updated_at": creator_review.json()["task_updated_at"],
            },
        )
        assert creator_resolution.status_code == 422
        assert "conflict creator" in creator_resolution.json()["detail"]
        generic_decision = client.post(
            f"/v1/projects/{project['id']}/approvals/{conflict_task_id}/decision",
            headers=operator,
            json={
                "decision": "APPROVED",
                "reason": "Generic approval must not bypass conflict resolution",
                "expected_task_updated_at": creator_review.json()["task_updated_at"],
                "evidence_ids": [observation_ids[0]],
            },
        )
        assert generic_decision.status_code == 422
        assert "dedicated workflow" in generic_decision.json()["detail"]
        with create_session_factory(engine).begin() as session:
            conflict_task = session.get(ApprovalTaskRow, conflict_task_id)
            assert conflict_task is not None
            conflict_task.required = False
        corrupted_task_review = client.get(
            f"/v1/projects/{project['id']}/evidence/conflicts/{conflict_id}",
            headers=operator,
        )
        assert corrupted_task_review.status_code == 200
        assert "CONFLICT_TASK_NOT_REQUIRED" in corrupted_task_review.json()["resolution_blockers"]
        corrupted_task_resolution = client.post(
            f"/v1/projects/{project['id']}/evidence/conflicts/{conflict_id}/resolve",
            headers=operator,
            json={
                "selected_observation_id": observation_ids[0],
                "resolution_reason": "A non-mandatory conflict task must fail closed",
                "expected_conflict_updated_at": corrupted_task_review.json()["conflict_updated_at"],
                "expected_task_updated_at": corrupted_task_review.json()["task_updated_at"],
            },
        )
        assert corrupted_task_resolution.status_code == 422
        assert "integrity check failed" in corrupted_task_resolution.json()["detail"]
        with create_session_factory(engine).begin() as session:
            conflict_task = session.get(ApprovalTaskRow, conflict_task_id)
            assert conflict_task is not None
            conflict_task.required = True
            parser_qualification = session.get(
                AdapterQualificationRow,
                parser_adapter["version_id"],
            )
            assert parser_qualification is not None
            parser_qualification.status = "REVOKED"
        revoked_qualification_review = client.get(
            f"/v1/projects/{project['id']}/evidence/conflicts/{conflict_id}",
            headers=operator,
        )
        assert revoked_qualification_review.status_code == 200
        assert (
            "CONFLICT_INDEPENDENCE_INVALID"
            in revoked_qualification_review.json()["resolution_blockers"]
        )
        revoked_qualification_resolution = client.post(
            f"/v1/projects/{project['id']}/evidence/conflicts/{conflict_id}/resolve",
            headers=operator,
            json={
                "selected_observation_id": observation_ids[0],
                "resolution_reason": "A revoked extraction qualification must fail closed",
                "expected_conflict_updated_at": revoked_qualification_review.json()[
                    "conflict_updated_at"
                ],
                "expected_task_updated_at": revoked_qualification_review.json()["task_updated_at"],
            },
        )
        assert revoked_qualification_resolution.status_code == 422
        assert "independence domains" in revoked_qualification_resolution.json()["detail"]
        with create_session_factory(engine).begin() as session:
            parser_qualification = session.get(
                AdapterQualificationRow,
                parser_adapter["version_id"],
            )
            assert parser_qualification is not None
            parser_qualification.status = "APPROVED"
            parser_observation = session.get(ObservationRow, observation_ids[0])
            assert parser_observation is not None
            parser_observation.payload = {
                **parser_observation.payload,
                "unit": "kg",
            }
        invalid_basis_review = client.get(
            f"/v1/projects/{project['id']}/evidence/conflicts/{conflict_id}",
            headers=operator,
        )
        assert invalid_basis_review.status_code == 200
        assert (
            "CONFLICT_COMMERCIAL_BASIS_INVALID"
            in invalid_basis_review.json()["resolution_blockers"]
        )
        invalid_basis_resolution = client.post(
            f"/v1/projects/{project['id']}/evidence/conflicts/{conflict_id}/resolve",
            headers=operator,
            json={
                "selected_observation_id": conflicting_visual.json()["observation_id"],
                "resolution_reason": "Any corrupted source basis must fail the conflict set closed",
                "expected_conflict_updated_at": invalid_basis_review.json()["conflict_updated_at"],
                "expected_task_updated_at": invalid_basis_review.json()["task_updated_at"],
            },
        )
        assert invalid_basis_resolution.status_code == 422
        assert "differs from its observation unit" in invalid_basis_resolution.json()["detail"]
        with create_session_factory(engine).begin() as session:
            parser_observation = session.get(ObservationRow, observation_ids[0])
            assert parser_observation is not None
            parser_observation.payload = {
                **parser_observation.payload,
                "unit": "m",
            }
        independent_review = client.get(
            f"/v1/projects/{project['id']}/evidence/conflicts/{conflict_id}",
            headers=operator,
        )
        assert independent_review.status_code == 200, independent_review.text
        assert independent_review.json()["resolution_allowed"] is True
        assert len(independent_review.json()["observations"]) == 2
        reviewed_observation = next(
            observation
            for observation in independent_review.json()["observations"]
            if observation["method"] == "TABLE_PARSER"
        )
        assert reviewed_observation["adapter_qualification_id"] == parser_adapter["version_id"]
        assert reviewed_observation["adapter_qualification_status"] == "APPROVED"
        assert reviewed_observation["independence_domain"] == "deterministic-table-parser"
        assert reviewed_observation["basis_metadata"] == {
            "basis_type": "NORMALIZED_PRICE",
            "currency": "RUB",
            "unit": "m",
        }
        dedicated_task = client.get(
            f"/v1/work-items/{conflict_task_id}",
            headers=operator,
        )
        assert dedicated_task.status_code == 200, dedicated_task.text
        assert dedicated_task.json()["decision_allowed"] is False
        assert dedicated_task.json()["decision_blockers"] == ["DEDICATED_WORKFLOW_REQUIRED"]
        stale_resolution = client.post(
            f"/v1/projects/{project['id']}/evidence/conflicts/{conflict_id}/resolve",
            headers=operator,
            json={
                "selected_observation_id": observation_ids[0],
                "resolution_reason": "A stale screen must not resolve the conflict",
                "expected_conflict_updated_at": "2026-01-01T00:00:00Z",
                "expected_task_updated_at": independent_review.json()["task_updated_at"],
            },
        )
        assert stale_resolution.status_code == 409
        stale_task_resolution = client.post(
            f"/v1/projects/{project['id']}/evidence/conflicts/{conflict_id}/resolve",
            headers=operator,
            json={
                "selected_observation_id": observation_ids[0],
                "resolution_reason": "A stale task version must not resolve the conflict",
                "expected_conflict_updated_at": independent_review.json()["conflict_updated_at"],
                "expected_task_updated_at": "2026-01-01T00:00:00Z",
            },
        )
        assert stale_task_resolution.status_code == 409
        resolved_conflict = client.post(
            f"/v1/projects/{project['id']}/evidence/conflicts/{conflict_id}/resolve",
            headers=operator,
            json={
                "selected_observation_id": observation_ids[0],
                "resolution_reason": "Reviewer checked the native table against the signed source",
                "expected_conflict_updated_at": independent_review.json()["conflict_updated_at"],
                "expected_task_updated_at": independent_review.json()["task_updated_at"],
            },
        )
        assert resolved_conflict.status_code == 200, resolved_conflict.text
        assert resolved_conflict.json()["conflict"]["status"] == "VERIFIED"
        assert resolved_conflict.json()["verified_observation"]["status"] == "VERIFIED"
        resolved_observation_id = resolved_conflict.json()["verified_observation"]["observation_id"]
        with create_session_factory(engine)() as session:
            resolved_observation = session.get(ObservationRow, resolved_observation_id)
            assert resolved_observation is not None
            assert resolved_observation.payload == {
                "observation": resolved_conflict.json()["verified_observation"],
                "source_observation_ids": [observation_ids[0]],
                "conflict_id": conflict_id,
                "derivation_type": "CONFLICT_RESOLUTION",
                "basis_type": "NORMALIZED_PRICE",
                "unit_rate": "125.50",
                "currency": "RUB",
                "unit": "m",
            }
        resolved_review = client.get(
            f"/v1/projects/{project['id']}/evidence/conflicts/{conflict_id}",
            headers=operator,
        )
        assert resolved_review.status_code == 200, resolved_review.text
        assert resolved_review.json()["task_status"] == "APPROVED"
        assert "CONFLICT_NOT_OPEN" in resolved_review.json()["resolution_blockers"]
        workbench_after_resolution = client.get(
            f"/v1/projects/{project['id']}/workbench",
            headers=operator,
        )
        assert workbench_after_resolution.status_code == 200
        conflict_metric = next(
            metric
            for metric in workbench_after_resolution.json()["metrics"]
            if metric["code"] == "CONFLICTS"
        )
        assert conflict_metric == {
            "code": "CONFLICTS",
            "label": "Unresolved conflicts",
            "value": 0,
            "blocking": 0,
        }

        approve_version(
            "approval_policy",
            "approval-policy-1",
            {
                "rules": [
                    {
                        "reason": "DOCUMENT_CONFLICT",
                        "assigned_role": "REVIEWER",
                        "required": True,
                    },
                    {
                        "reason": "HIGH_PRICE_SPREAD",
                        "assigned_role": "REVIEWER",
                        "threshold": "0.50",
                        "threshold_kind": "RELATIVE_SPREAD",
                        "required": True,
                    },
                ]
            },
            purpose="approval_policy",
        )
        approve_version(
            "document_requirements",
            "document-requirements-1",
            {
                "passport": {
                    "required_fields": [],
                    "independently_verified_fields": [],
                }
            },
            purpose="document_requirements",
        )
        approve_version(
            "quantity_policy",
            "quantity-policy-1",
            {
                "policy": {
                    "absolute_tolerance": "0.01",
                    "relative_tolerance": "0.001",
                    "allow_zero": False,
                    "allow_negative": False,
                }
            },
            purpose="quantity_policy",
        )
        approve_version(
            "quantity_formula_rules",
            "quantity-formula-rules-1",
            {"allowed_operations": ["SUM", "PRODUCT"]},
            purpose="quantity_formula_rules",
        )
        approve_version(
            "manual_change_policy",
            "manual-change-policy-1",
            {
                "policy": {
                    "rules": [
                        {
                            "entity_type": "quantity",
                            "field_name": "record",
                            "critical": True,
                            "assigned_role": "REVIEWER",
                        }
                    ]
                }
            },
            purpose="manual_change_policy",
        )
        approve_version(
            "scope_rules",
            "scope-rules-1",
            {
                "rules": [
                    {
                        "rule_id": "non-applicable-test-rule",
                        "trigger_any_work_codes": ["NEVER_APPLICABLE"],
                        "required_work_codes": ["NEVER_APPLICABLE"],
                        "rationale": "Exercise an approved rule pack without a matching trigger",
                    }
                ]
            },
            purpose="scope_rules",
        )
        approve_version(
            "catalog",
            "catalog-1",
            {
                "items": {
                    "pipe": {
                        "attributes": {"type": "pipe"},
                        "critical_attributes": ["type"],
                        "critical_price": False,
                    }
                }
            },
            purpose="catalog",
        )
        approve_version(
            "price_policy",
            "price-policy-1",
            {
                "selection_method": "MEDIAN",
                "normalization_rounding_scale": 2,
                "normalization_rounding_mode": "ROUND_HALF_UP",
                "target_bases": {
                    "pipe-delivered-rub": {
                        "currency": "RUB",
                        "vat_basis": "INCLUSIVE",
                        "vat_rate": "0.20",
                        "unit": "m",
                        "package_quantity": "1",
                        "party_quantity": "1000",
                        "region": "Moscow",
                        "delivery_included": True,
                        "unloading_included": True,
                        "payment_terms": "30 days",
                    }
                },
                "item_target_basis_ids": {"pipe": "pipe-delivered-rub"},
                "unit_conversions": {},
                "fx_rates": {},
                "adjustments": {},
                "region_adjustments": {},
                "party_adjustments": {},
                "payment_adjustments": {},
            },
            purpose="price_policy",
        )
        approve_version(
            "scenario_policy",
            "scenario-policy-1",
            {
                "scenarios": {
                    "supplier-stress": {
                        "name": "Supplier stress case",
                        "overrides": [
                            {
                                "semantic_key": "pipe",
                                "unit_rate": "150",
                                "factor_values": {},
                                "evidence_or_assumption_id": "BOUND_SCENARIO_POLICY",
                                "reason": "Approved supplier stress assumption",
                            }
                        ],
                    }
                }
            },
            purpose="scenario_policy",
        )
        risk_model = approve_version(
            "risk_model",
            "risk-model-1",
            {
                "policy": {
                    "method": "THREE_POINT_EXPECTED_VALUE",
                    "currency": "RUB",
                    "rounding_scale": 2,
                    "rounding_mode": "ROUND_HALF_UP",
                },
                "minimum_risk_items": 1,
                "independently_verified_risk_keys": [],
                "reserve_unit": "project",
                "reserve_cost_component": {
                    "line_id": "boq-line-risk-test",
                    "semantic_key": "risk-reserve",
                },
            },
            purpose="risk_model",
        )
        approve_version(
            "contract_risk_rules",
            "contract-risk-rules-1",
            {
                "contract": {
                    "required_term_kinds": [],
                    "independently_verified_term_kinds": [],
                }
            },
            purpose="contract_risk_rules",
        )
        approval_plan = client.post(
            f"/v1/projects/{project['id']}/approvals/plan",
            headers=operator,
            json={
                "subjects": [
                    {
                        "entity_type": "evidence_set",
                        "entity_id": verified_observation_id,
                        "reasons": ["DOCUMENT_CONFLICT"],
                    }
                ],
                "reason": "Plan policy-mandated independent review",
            },
        )
        assert approval_plan.status_code == 200, approval_plan.text
        task_ids = approval_plan.json()["task_ids_by_key"]
        assert len(task_ids) == 1
        approval_task_id = next(iter(task_ids.values()))
        approval_task = client.get(
            f"/v1/work-items/{approval_task_id}",
            headers=owner_b,
        )
        assert approval_task.status_code == 200, approval_task.text
        approval = client.post(
            f"/v1/projects/{project['id']}/approvals/{approval_task_id}/decision",
            headers=owner_b,
            json={
                "decision": "APPROVED",
                "reason": "Independent evidence reviewed",
                "expected_task_updated_at": approval_task.json()["item"]["updated_at"],
                "evidence_ids": observation_ids,
            },
        )
        assert approval.status_code == 200, approval.text
        assert approval.json()["decision"] == "APPROVED"

        structured_observation_ids: list[str] = []
        for method, locator, adapter in (
            ("TABLE_PARSER", "table:boq[row=1]", parser_adapter),
            ("VISUAL_MODEL", "image-region:boq-row-1", visual_adapter),
        ):
            recorded = client.post(
                f"/v1/projects/{project['id']}/evidence/observations",
                headers=system,
                json={
                    "draft": {
                        "field_name": "boq_line",
                        "value": {
                            "work_code": "UNIT_RATE_ITEM",
                            "unit": "m",
                        },
                        "unit": None,
                        "method": method,
                        "method_version": "1.0",
                        "source_priority": 1,
                        "location": {
                            "document_id": uploaded_payload["document_id"],
                            "document_revision_id": uploaded_payload["document_revision_id"],
                            "original_object_hash": uploaded_payload["manifest"]["root_sha256"],
                            "locator_kind": "structured_region",
                            "locator": locator,
                        },
                        "observed_at": "2026-07-23T10:05:00Z",
                        "adapter_qualification_id": adapter["version_id"],
                        "basis_metadata": {},
                    },
                    "reason": f"Record BoQ line through {method}",
                },
            )
            assert recorded.status_code == 201, recorded.text
            structured_observation_ids.append(recorded.json()["observation_id"])
        structured_reconciliation = client.post(
            f"/v1/projects/{project['id']}/evidence/reconcile",
            headers=owner_b,
            json={
                "observation_ids": structured_observation_ids,
                "reconciliation_version_id": reconciliation_rules["version_id"],
                "reason": "Independent BoQ extraction agrees",
            },
        )
        assert structured_reconciliation.status_code == 200, structured_reconciliation.text
        boq_observation_id = structured_reconciliation.json()["verified_observation_id"]

        current = client.get(f"/v1/projects/{project['id']}", headers=operator).json()
        for state in (
            "EXTRACTION_IN_PROGRESS",
            "EXTRACTION_REVIEW",
            "BOQ_IN_PROGRESS",
        ):
            response = client.post(
                f"/v1/projects/{project['id']}/transitions",
                headers=operator,
                json={
                    "to_state": state,
                    "expected_row_version": current["row_version"],
                    "reason": f"Advance to {state}",
                },
            )
            assert response.status_code == 200, response.text
            current = response.json()

        authoring_context = client.get(
            f"/v1/projects/{project['id']}/boq/authoring-context",
            headers=operator,
            params={"evidence_field_name": "boq_line", "limit": 100},
        )
        assert authoring_context.status_code == 200, authoring_context.text
        assert authoring_context.json()["project_state"] == "BOQ_IN_PROGRESS"
        assert (
            authoring_context.json()["document_set_revision_id"]
            == (current["current_document_set_revision_id"])
        )
        assert boq_observation_id in {
            candidate["observation"]["observation_id"]
            for candidate in authoring_context.json()["evidence_candidates"]
        }
        created_line = client.post(
            f"/v1/projects/{project['id']}/boq/lines",
            headers=operator,
            json={
                "draft": {
                    "line_key": "unit-rate-item",
                    "wbs_node_id": "wbs-1",
                    "work_code": "UNIT_RATE_ITEM",
                    "description": "Verified unit-rate item",
                    "unit": "m",
                    "evidence_observation_ids": [boq_observation_id],
                    "cost_components": [
                        {
                            "semantic_key": "pipe",
                            "category": "MATERIAL",
                            "basis_kind": "MARKET",
                        }
                    ],
                    "critical_quantity": False,
                },
                "reason": "Build calculation BoQ",
            },
        )
        assert created_line.status_code == 201, created_line.text
        line_id = created_line.json()["line_id"]
        line_review = client.get(
            f"/v1/projects/{project['id']}/boq/lines/{line_id}/review",
            headers=owner_b,
        )
        assert line_review.status_code == 200, line_review.text
        assert line_review.json()["verification_allowed"] is True
        assert line_review.json()["line"]["updated_at"] == created_line.json()["updated_at"]
        stale_line_verification = client.post(
            f"/v1/projects/{project['id']}/boq/lines/{line_id}/verify",
            headers=owner_b,
            json={
                "expected_line_updated_at": "2020-01-01T00:00:00Z",
                "reason": "A stale operator decision must fail closed",
            },
        )
        assert stale_line_verification.status_code == 409
        verified_line = client.post(
            f"/v1/projects/{project['id']}/boq/lines/{line_id}/verify",
            headers=owner_b,
            json={
                "expected_line_updated_at": created_line.json()["updated_at"],
                "reason": "Independent technical review",
            },
        )
        assert verified_line.status_code == 200, verified_line.text
        quantity = client.post(
            f"/v1/projects/{project['id']}/boq/lines/{line_id}/quantities",
            headers=operator,
            json={
                "submission": {
                    "draft": {
                        "value": "125.50",
                        "unit": "m",
                        "source_observation_ids": [verified_observation_id],
                        "source_priority": 1,
                        "rounding_scale": 2,
                        "waste_factor": "0",
                    }
                },
                "reason": "Record verified test quantity",
            },
        )
        assert quantity.status_code == 201, quantity.text
        assert quantity.json()["validation"]["passed"]
        quantity_change_context = client.get(
            (f"/v1/projects/{project['id']}/boq/lines/{line_id}/quantity-change-context"),
            headers=operator,
        )
        assert quantity_change_context.status_code == 200, quantity_change_context.text
        assert (
            quantity_change_context.json()["current_quantity_id"]
            == (quantity.json()["quantity"]["quantity_id"])
        )
        assert quantity_change_context.json()["manual_change_policy_version_id"]
        quantity_change = client.post(
            (f"/v1/projects/{project['id']}/boq/lines/{line_id}/quantity-change-proposals"),
            headers=operator,
            json={
                "submission": {
                    "draft": {
                        "value": "125.50",
                        "unit": "m",
                        "source_observation_ids": [verified_observation_id],
                        "source_priority": 2,
                        "rounding_scale": 2,
                        "waste_factor": "0",
                    }
                },
                "reason": "Correct the governed source priority",
            },
        )
        assert quantity_change.status_code == 201, quantity_change.text
        quantity_change_payload = quantity_change.json()
        assert quantity_change_payload["status"] == "PENDING_APPROVAL"
        quantity_change_id = quantity_change_payload["change_id"]
        quantity_change_task_id = quantity_change_payload["approval_task_id"]
        pending_change_transition = client.post(
            f"/v1/projects/{project['id']}/transitions",
            headers=operator,
            json={
                "to_state": "BOQ_REVIEW",
                "expected_row_version": current["row_version"],
                "reason": "A pending quantity correction must block the BoQ gate",
            },
        )
        assert pending_change_transition.status_code == 422
        assert f"manual-change:{quantity_change_id}:unapplied" in pending_change_transition.text
        premature_apply = client.post(
            (f"/v1/projects/{project['id']}/manual-changes/{quantity_change_id}/apply"),
            headers=operator,
            json={"reason": "An unapproved change must fail closed"},
        )
        assert premature_apply.status_code == 422
        assert "approval task is incomplete" in premature_apply.text
        quantity_change_task = client.get(
            f"/v1/work-items/{quantity_change_task_id}",
            headers=owner_b,
        )
        assert quantity_change_task.status_code == 200, quantity_change_task.text
        quantity_change_approval = client.post(
            (f"/v1/projects/{project['id']}/approvals/{quantity_change_task_id}/decision"),
            headers=owner_b,
            json={
                "decision": "APPROVED",
                "reason": "Verified the exact source observation and proposed record",
                "expected_task_updated_at": quantity_change_task.json()["item"]["updated_at"],
                "evidence_ids": [verified_observation_id],
            },
        )
        assert quantity_change_approval.status_code == 200, quantity_change_approval.text
        applied_quantity_change = client.post(
            (f"/v1/projects/{project['id']}/manual-changes/{quantity_change_id}/apply"),
            headers=operator,
            json={"reason": "Apply the independently approved exact revision"},
        )
        assert applied_quantity_change.status_code == 201, applied_quantity_change.text
        assert applied_quantity_change.json()["validation"]["passed"] is True
        reviewed_quantity_change = client.get(
            f"/v1/projects/{project['id']}/manual-changes/{quantity_change_id}",
            headers=owner_b,
        )
        assert reviewed_quantity_change.status_code == 200
        assert reviewed_quantity_change.json()["status"] == "APPLIED"
        boq_records = client.get(
            f"/v1/projects/{project['id']}/records",
            headers=operator,
            params={"section": "BOQ_SCOPE", "statuses": "APPLIED"},
        )
        assert boq_records.status_code == 200, boq_records.text
        manual_change_record = next(
            record for record in boq_records.json()["items"] if record["id"] == quantity_change_id
        )
        assert manual_change_record["kind"] == "MANUAL_CHANGE"
        assert (
            manual_change_record["attributes"]["applied_quantity_id"]
            == (applied_quantity_change.json()["quantity"]["quantity_id"])
        )

        for state in ("BOQ_REVIEW",):
            response = client.post(
                f"/v1/projects/{project['id']}/transitions",
                headers=operator,
                json={
                    "to_state": state,
                    "expected_row_version": current["row_version"],
                    "reason": f"Advance to {state}",
                },
            )
            assert response.status_code == 200, response.text
            current = response.json()
        scope = client.post(
            f"/v1/projects/{project['id']}/boq/scope-evaluations",
            headers=owner_b,
            json={"wbs_node_id": "wbs-1", "reason": "Run approved scope rules"},
        )
        assert scope.status_code == 200, scope.text
        assert scope.json()["evaluation"]["findings"] == []
        for state in ("PRICING_IN_PROGRESS",):
            response = client.post(
                f"/v1/projects/{project['id']}/transitions",
                headers=operator,
                json={
                    "to_state": state,
                    "expected_row_version": current["row_version"],
                    "reason": f"Advance to {state}",
                },
            )
            assert response.status_code == 200, response.text
            current = response.json()

        now = datetime(2026, 7, 23, tzinfo=UTC)
        with create_session_factory(engine).begin() as session:
            session.add(
                BoqLineRow(
                    id="boq-line-risk-test",
                    project_id=project["id"],
                    line_key="risk",
                    wbs_node_id="wbs-risk",
                    work_code="PROJECT_RISK_RESERVE",
                    description="Governed project risk reserve",
                    unit="project",
                    status="VERIFIED",
                    payload={
                        "cost_components": [
                            {
                                "semantic_key": "risk-reserve",
                                "category": "RISK",
                                "basis_kind": "RISK_MODEL",
                            }
                        ]
                    },
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                QuantityRow(
                    id="quantity-risk-test",
                    boq_line_id="boq-line-risk-test",
                    value="1",
                    unit="project",
                    status="VERIFIED",
                    supersedes_quantity_id=None,
                    is_current=True,
                    payload={"test_fixture": "Risk reserve quantity"},
                    created_at=now,
                    updated_at=now,
                )
            )
            risk_item_id = "risk-item-calculation-test"
            session.add(
                RiskItemRow(
                    id=risk_item_id,
                    project_id=project["id"],
                    risk_key="test-risk",
                    status="VERIFIED",
                    currency="RUB",
                    expected_impact="10",
                    supersedes_risk_id=None,
                    is_current=True,
                    payload={"test_fixture": "Calculation test risk register"},
                    created_at=now,
                    updated_at=now,
                )
            )
            risk_reference = {
                "line_id": "boq-line-risk-test",
                "semantic_key": "risk-reserve",
            }
            risk_signature = content_hash(
                {
                    "risk_item_ids": [risk_item_id],
                    "risk_model_version_id": risk_model["version_id"],
                    "reserve_cost_component": risk_reference,
                }
            )
            session.add(
                RiskCalculationRow(
                    id="risk-calculation-test",
                    project_id=project["id"],
                    policy_version_id=risk_model["version_id"],
                    status="VALIDATED",
                    expected_reserve="10",
                    currency="RUB",
                    unit="project",
                    supersedes_calculation_id=None,
                    is_current=True,
                    payload={
                        "input_signature": risk_signature,
                        "reserve_cost_component": risk_reference,
                        "basis_type": "RISK_RESERVE",
                        "unit_rate": "10",
                        "currency": "RUB",
                        "unit": "project",
                    },
                    created_at=now,
                )
            )
            attributes_observation = Observation(
                observation_id="observation-technical-attributes-pipe",
                field_name="technical_attributes",
                value={"type": "pipe"},
                method=EvidenceMethod.RULE_ENGINE,
                method_version=reconciliation_rules["version_id"],
                source_priority=1,
                location=EvidenceLocation(
                    document_id=uploaded_payload["document_id"],
                    document_revision_id=uploaded_payload["document_revision_id"],
                    original_object_hash=uploaded_payload["manifest"]["root_sha256"],
                    locator_kind="structured_region",
                    locator="specification-pipe",
                ),
                observed_at=now,
                actor_id="owner-b",
                status=VerificationStatus.VERIFIED,
            )
            session.add(
                ObservationRow(
                    id=attributes_observation.observation_id,
                    project_id=project["id"],
                    document_revision_id=uploaded_payload["document_revision_id"],
                    field_name=attributes_observation.field_name,
                    method=attributes_observation.method.value,
                    method_version=attributes_observation.method_version,
                    status=VerificationStatus.VERIFIED.value,
                    payload={"observation": attributes_observation.model_dump(mode="json")},
                    created_at=now,
                )
            )
            price_basis = CommercialBasis(
                currency="RUB",
                vat_basis=VatBasis.INCLUSIVE,
                vat_rate=Decimal("0.20"),
                unit="m",
                package_quantity=Decimal("1"),
                party_quantity=Decimal("1000"),
                region="Moscow",
                delivery_included=True,
                unloading_included=True,
                payment_terms="30 days",
            )
            price_drafts: list[PriceQuoteDraft] = []
            for index, (evidence_class, source_origin) in enumerate(
                (
                    (
                        PriceEvidenceClass.OFFICIAL_OR_PRIMARY,
                        "manufacturer-primary",
                    ),
                    (
                        PriceEvidenceClass.INDEPENDENT_MARKET,
                        "independent-market-index",
                    ),
                ),
                start=1,
            ):
                source_observation_id = f"observation-price-{index}"
                draft = PriceQuoteDraft(
                    item_id="pipe",
                    supplier_id=f"price-source-{index}",
                    evidence_class=evidence_class,
                    source_observation_id=source_observation_id,
                    technical_attributes={"type": "pipe"},
                    amount=Decimal("125.50"),
                    basis=price_basis,
                    quote_date=date(2026, 7, 20),
                    valid_until=date(2026, 8, 20),
                    lead_time_days=10,
                    available=True,
                    source_reliability=Decimal("0.95"),
                )
                price_drafts.append(draft)
                leaf_ids: list[str] = []
                for suffix, method, qualification_id in (
                    (
                        "parser",
                        EvidenceMethod.TABLE_PARSER,
                        parser_adapter["version_id"],
                    ),
                    (
                        "visual",
                        EvidenceMethod.VISUAL_MODEL,
                        visual_adapter["version_id"],
                    ),
                ):
                    leaf_id = f"{source_observation_id}-{suffix}"
                    leaf_ids.append(leaf_id)
                    leaf = Observation(
                        observation_id=leaf_id,
                        field_name=f"price_quote:pipe:{index}",
                        value=draft.evidence_value(),
                        method=method,
                        method_version="1.0",
                        source_priority=1,
                        location=EvidenceLocation(
                            document_id=uploaded_payload["document_id"],
                            document_revision_id=uploaded_payload["document_revision_id"],
                            original_object_hash=uploaded_payload["manifest"]["root_sha256"],
                            locator_kind="structured_region",
                            locator=f"price-quote-{index}-{suffix}",
                        ),
                        observed_at=now,
                        actor_id="extractor-service",
                    )
                    session.add(
                        ObservationRow(
                            id=leaf_id,
                            project_id=project["id"],
                            document_revision_id=uploaded_payload["document_revision_id"],
                            field_name=leaf.field_name,
                            method=leaf.method.value,
                            method_version=leaf.method_version,
                            status=leaf.status.value,
                            payload={
                                "observation": leaf.model_dump(mode="json"),
                                "adapter_qualification_id": qualification_id,
                                "source_origin_id": source_origin,
                            },
                            created_at=now,
                        )
                    )
                reconciled = Observation(
                    observation_id=source_observation_id,
                    field_name=f"price_quote:pipe:{index}",
                    value=draft.evidence_value(),
                    method=EvidenceMethod.RULE_ENGINE,
                    method_version=reconciliation_rules["version_id"],
                    source_priority=1,
                    location=EvidenceLocation(
                        document_id=uploaded_payload["document_id"],
                        document_revision_id=uploaded_payload["document_revision_id"],
                        original_object_hash=uploaded_payload["manifest"]["root_sha256"],
                        locator_kind="structured_region",
                        locator=f"price-quote-{index}",
                    ),
                    observed_at=now,
                    actor_id="owner-b",
                    status=VerificationStatus.VERIFIED,
                )
                session.add(
                    ObservationRow(
                        id=reconciled.observation_id,
                        project_id=project["id"],
                        document_revision_id=uploaded_payload["document_revision_id"],
                        field_name=reconciled.field_name,
                        method=reconciled.method.value,
                        method_version=reconciled.method_version,
                        status=reconciled.status.value,
                        payload={
                            "observation": reconciled.model_dump(mode="json"),
                            "source_observation_ids": leaf_ids,
                        },
                        created_at=now,
                    )
                )
            session.flush()
            pricing = PricingService(
                session=session,
                settings=settings,
                object_store=LocalObjectStore(tmp_path / "objects"),
            )
            pricing_actor = Actor(
                "operator-1",
                "org-1",
                frozenset(
                    {
                        ActorRole.ESTIMATOR,
                        ActorRole.PROCUREMENT,
                        ActorRole.TECHNICAL_EXPERT,
                        ActorRole.REVIEWER,
                    }
                ),
            )
            nomenclature_assessment = pricing.assess_nomenclature(
                actor=pricing_actor,
                project_id=project["id"],
                draft=NomenclatureAssessmentDraft(
                    source_item_id="pipe",
                    canonical_item_id="pipe",
                    source_attributes_observation_id=attributes_observation.observation_id,
                ),
                request_id="calculation-nomenclature-assessment",
                reason="Assess calculation item against controlled catalog",
            )
            for index, draft in enumerate(price_drafts, start=1):
                quote = pricing.record_quote_from_observation(
                    actor=pricing_actor,
                    project_id=project["id"],
                    item_id="pipe",
                    source_observation_id=draft.source_observation_id,
                    request_id=f"calculation-price-quote-{index}",
                    reason="Record governed calculation price source",
                )
                pricing.normalize_price(
                    actor=pricing_actor,
                    project_id=project["id"],
                    command=NormalizePriceCommand(
                        quote_id=quote.quote.quote_id,
                    ),
                    request_id=f"calculation-normalized-price-{index}",
                    reason="Normalize governed calculation price source",
                )
            price_decision = pricing.evaluate_item_price(
                actor=pricing_actor,
                project_id=project["id"],
                item_id="pipe",
                as_of=date(2026, 7, 23),
                request_id="calculation-price-evaluation",
                reason="Evaluate governed calculation price sources",
            )
            assert price_decision.status.value == "VERIFIED"
            assert price_decision.derived_observation_id is not None
            verified_price_observation_id = price_decision.derived_observation_id
        nomenclature_context = client.get(
            f"/v1/projects/{project['id']}/nomenclature/context",
            headers=operator,
            params={
                "catalog_query": "pipe",
                "evidence_field_name": "technical_attributes",
                "limit": 100,
            },
        )
        assert nomenclature_context.status_code == 200, nomenclature_context.text
        assert nomenclature_context.json()["catalog_version_id"]
        assert {
            item["canonical_item_id"] for item in nomenclature_context.json()["catalog_items"]
        } == {"pipe"}
        assert attributes_observation.observation_id in {
            candidate["observation"]["observation_id"]
            for candidate in nomenclature_context.json()["evidence_candidates"]
        }
        nomenclature_review = client.get(
            (
                f"/v1/projects/{project['id']}/nomenclature/"
                f"{nomenclature_assessment.match.match_id}/review"
            ),
            headers=operator,
        )
        assert nomenclature_review.status_code == 200, nomenclature_review.text
        assert nomenclature_review.json()["match"]["status"] == "VERIFIED"
        assert nomenclature_review.json()["match"]["match"]["match_class"] == "EXACT"
        response = client.post(
            f"/v1/projects/{project['id']}/transitions",
            headers=operator,
            json={
                "to_state": "CALCULATION_IN_PROGRESS",
                "expected_row_version": current["row_version"],
                "reason": "Advance after test fixture pricing verification",
            },
        )
        assert response.status_code == 200, response.text
        current = response.json()

        calculation_context = client.get(
            f"/v1/projects/{project['id']}/calculation-context",
            headers=operator,
        )
        assert calculation_context.status_code == 200, calculation_context.text
        context_payload = calculation_context.json()
        assert context_payload["blockers"] == []
        candidate = context_payload["candidate"]
        assert candidate is not None
        assert candidate["project_row_version"] == current["row_version"]
        assert candidate["calculation_model_version_id"] == version["version_id"]
        assert {item["semantic_key"] for item in candidate["inputs"]} == {
            "pipe",
            "risk-reserve",
        }
        calculation_payload = {
            "expected_row_version": current["row_version"],
            "inputs": candidate["inputs"],
            "policy": candidate["policy"],
            "reason": "Calculate reviewed atomic inputs",
        }
        pipe_input = next(
            item for item in calculation_payload["inputs"] if item["semantic_key"] == "pipe"
        )
        risk_input = next(
            item for item in calculation_payload["inputs"] if item["semantic_key"] == "risk-reserve"
        )
        fake_source_payload = {
            **calculation_payload,
            "inputs": [
                {
                    **pipe_input,
                    "source_observation_id": "invented-source",
                },
                risk_input,
            ],
        }
        rejected = client.post(
            f"/v1/projects/{project['id']}/calculations",
            headers=operator,
            json=fake_source_payload,
        )
        assert rejected.status_code == 422
        assert "server-generated evidence candidate" in rejected.json()["detail"]

        mismatched_quantity_payload = {
            **calculation_payload,
            "inputs": [
                {
                    **pipe_input,
                    "quantity": "10",
                },
                risk_input,
            ],
        }
        rejected_quantity = client.post(
            f"/v1/projects/{project['id']}/calculations",
            headers=operator,
            json=mismatched_quantity_payload,
        )
        assert rejected_quantity.status_code == 422
        assert "server-generated evidence candidate" in rejected_quantity.json()["detail"]

        tampered_policy = client.post(
            f"/v1/projects/{project['id']}/calculations",
            headers=operator,
            json={
                **calculation_payload,
                "policy": {
                    **calculation_payload["policy"],
                    "independent_tolerance": "999999",
                },
            },
        )
        assert tampered_policy.status_code == 422
        assert "current approved calculation model" in tampered_policy.json()["detail"]

        stale_candidate = client.post(
            f"/v1/projects/{project['id']}/calculations/current",
            headers=operator,
            json={
                "expected_row_version": current["row_version"],
                "candidate_hash": "f" * 64,
                "reason": "A stale candidate hash must fail closed",
            },
        )
        assert stale_candidate.status_code == 422
        assert "candidate changed" in stale_candidate.json()["detail"]

        calculated = client.post(
            f"/v1/projects/{project['id']}/calculations/current",
            headers=operator,
            json={
                "expected_row_version": current["row_version"],
                "candidate_hash": candidate["candidate_hash"],
                "reason": "Calculate the exact server-generated evidence candidate",
            },
        )
        assert calculated.status_code == 201, calculated.text
        result = calculated.json()
        assert result["primary"]["grand_total"] == "15760.25"
        assert result["independent"]["passed"] is True
        assert result["snapshot"]["fixed"] is True
        assert result["project"]["state"] == "INDEPENDENT_VALIDATION"

        fixed_context = client.get(
            f"/v1/projects/{project['id']}/calculation-context",
            headers=operator,
        )
        assert fixed_context.status_code == 200, fixed_context.text
        fixed_payload = fixed_context.json()
        assert fixed_payload["candidate"] is None
        fixed = fixed_payload["latest_fixed_calculation"]
        assert fixed["snapshot_id"] == result["snapshot"]["snapshot_id"]
        assert fixed["calculation_run_id"].startswith("calculation-run-")
        assert fixed["document_set_revision_id"] == candidate_id
        assert fixed["calculation_model_version_id"] == version["version_id"]
        assert fixed["status"] == "VALIDATED"
        assert fixed["currency"] == "RUB"
        assert fixed["grand_total"] == "15760.25"
        assert fixed["independent_validation_passed"] is True
        assert fixed["snapshot_hash"] == result["snapshot"]["snapshot_hash"]
        assert fixed["created_by"] == "operator-1"
        assert fixed["created_at"] == result["snapshot"]["created_at"]
        assert fixed["integrity_valid"] is True
        assert fixed["integrity_error"] is None

        lineage = client.get(
            (f"/v1/projects/{project['id']}/snapshots/{result['snapshot']['snapshot_id']}/lineage"),
            headers=operator,
        )
        assert lineage.status_code == 200, lineage.text
        lineage_payload = lineage.json()
        assert lineage_payload["calculation"]["grand_total"] == "15760.250000000000"
        assert len(lineage_payload["cost_inputs"]) == 2
        pipe_lineage = next(
            item for item in lineage_payload["cost_inputs"] if item["semantic_key"] == "pipe"
        )
        evidence = pipe_lineage["evidence"]
        assert evidence["basis_id"] == verified_price_observation_id
        assert evidence["document"]["revision_id"] == uploaded_payload["document_revision_id"]
        assert len(evidence["source_observations"]) == 2

        scenario = client.post(
            f"/v1/projects/{project['id']}/scenarios/calculate",
            headers=operator,
            json={
                "command": {
                    "snapshot_id": result["snapshot"]["snapshot_id"],
                    "scenario_key": "supplier-stress",
                },
                "reason": "Evaluate the approved supplier stress case",
            },
        )
        assert scenario.status_code == 201, scenario.text
        assert scenario.json()["result"]["primary"]["grand_total"] == "18835.00"
        assert scenario.json()["result"]["independent"]["passed"] is True

        replacement = client.post(
            f"/v1/projects/{project['id']}/documents",
            headers=operator,
            data={
                "logical_key": "terms",
                "title": "Tender terms",
                "document_type": "TENDER_TERMS",
                "revision_label": "2",
                "reason": "Register a newer tender revision",
                "critical": "true",
            },
            files={"upload": ("terms-v2.txt", b"revised tender terms", "text/plain")},
        )
        assert replacement.status_code == 202, replacement.text
        replacement_payload = _scan_and_process_upload(
            client=client,
            engine=engine,
            settings=settings,
            upload=replacement.json(),
        )
        assert replacement_payload["status"] == "PROCESSED"
        blocked_project = client.get(
            f"/v1/projects/{project['id']}",
            headers=operator,
        )
        assert blocked_project.json()["state"] == "BLOCKED"
        with create_session_factory(engine).begin() as session:
            invalidated_line = session.get(BoqLineRow, line_id)
            invalidated_quantity = (
                session.query(QuantityRow)
                .filter(
                    QuantityRow.boq_line_id == line_id,
                    QuantityRow.is_current.is_(True),
                )
                .one_or_none()
            )
            invalidated_price = (
                session.query(PriceDecisionRow)
                .filter(
                    PriceDecisionRow.project_id == project["id"],
                    PriceDecisionRow.is_current.is_(True),
                )
                .one_or_none()
            )
            invalidated_risk = session.get(RiskItemRow, "risk-item-calculation-test")
            invalidated_risk_calculation = (
                session.query(RiskCalculationRow)
                .filter(
                    RiskCalculationRow.project_id == project["id"],
                    RiskCalculationRow.is_current.is_(True),
                )
                .one_or_none()
            )
            assert invalidated_line is not None and invalidated_line.status == "IN_REVIEW"
            assert invalidated_quantity is not None and invalidated_quantity.status == "IN_REVIEW"
            assert invalidated_price is not None and invalidated_price.status == "EXPIRED"
            assert invalidated_risk is not None and invalidated_risk.status == "IN_REVIEW"
            assert (
                invalidated_risk_calculation is not None
                and invalidated_risk_calculation.status == "STALE"
            )
        stale_gates = client.get(
            f"/v1/projects/{project['id']}/release-gates",
            headers=operator,
        )
        assert stale_gates.status_code == 200
        stale_codes = {finding["code"] for finding in stale_gates.json()["decision"]["findings"]}
        assert "CURRENT_DOCUMENT_SET_NOT_CONFIRMED" in stale_codes
        assert "CALCULATION_SNAPSHOT_STALE" in stale_codes

        with create_session_factory(engine).begin() as session:
            snapshot_row = session.get(
                CalculationSnapshotRow,
                result["snapshot"]["snapshot_id"],
            )
            assert snapshot_row is not None
            snapshot_path = settings.local_object_store_path / snapshot_row.object_key
        snapshot_path.write_bytes(snapshot_path.read_bytes() + b" ")
        with pytest.raises(
            RuntimeError,
            match="SHA-256 verification",
        ):
            client.get(
                (
                    f"/v1/projects/{project['id']}/snapshots/"
                    f"{result['snapshot']['snapshot_id']}/lineage"
                ),
                headers=operator,
            )
