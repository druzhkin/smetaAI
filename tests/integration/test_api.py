from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from tenderguard.api.main import create_app
from tenderguard.application.document_processing import DocumentProcessingService
from tenderguard.config import Settings
from tenderguard.domain.common import content_hash
from tenderguard.domain.enums import ActorRole
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.database import (
    create_database_engine,
    create_schema_for_tests,
    create_session_factory,
)
from tenderguard.infrastructure.object_store import LocalObjectStore
from tenderguard.infrastructure.orm import (
    AdapterQualificationRow,
    BoqLineRow,
    CalculationSnapshotRow,
    NomenclatureMatchRow,
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
        confirmed = client.post(
            f"/v1/projects/{project['id']}/document-set/confirm",
            headers=owner_b,
            json={
                "candidate_document_set_revision_id": candidate_id,
                "reason": "Current revision checked",
            },
        )
        assert confirmed.status_code == 200

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
                "rounding_mode": "ROUND_HALF_UP",
                "line_rounding_scale": 2,
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
        resolved_conflict = client.post(
            f"/v1/projects/{project['id']}/evidence/conflicts/{conflict_id}/resolve",
            headers=owner_b,
            json={
                "selected_observation_id": observation_ids[0],
                "resolution_reason": "Reviewer checked the native table against the signed source",
            },
        )
        assert resolved_conflict.status_code == 200, resolved_conflict.text
        assert resolved_conflict.json()["conflict"]["status"] == "VERIFIED"
        assert resolved_conflict.json()["verified_observation"]["status"] == "VERIFIED"

        approve_version(
            "approval_policy",
            "approval-policy-1",
            {
                "rules": [
                    {
                        "reason": "DOCUMENT_CONFLICT",
                        "assigned_role": "REVIEWER",
                        "required": True,
                    }
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
        catalog_version = approve_version(
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
        price_policy_version = approve_version(
            "price_policy",
            "price-policy-1",
            {
                "selection_method": "MEDIAN",
                "target_bases": {},
                "item_target_basis_ids": {},
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
                                "cost_input_id": "pipe-cost",
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
        verified_line = client.post(
            f"/v1/projects/{project['id']}/boq/lines/{line_id}/verify",
            headers=owner_b,
            json={"reason": "Independent technical review"},
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
            session.add(
                NomenclatureMatchRow(
                    id="nomenclature-match-calculation-test",
                    project_id=project["id"],
                    source_item_id="pipe",
                    canonical_item_id="pipe",
                    match_class="EXACT",
                    status="VERIFIED",
                    catalog_version_id=catalog_version["version_id"],
                    supersedes_match_id=None,
                    is_current=True,
                    payload={"test_fixture": "Calculation test pricing gate"},
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                PriceDecisionRow(
                    id="price-decision-calculation-test",
                    project_id=project["id"],
                    item_id="pipe",
                    status="VERIFIED",
                    amount_per_unit="125.50",
                    currency="RUB",
                    unit="m",
                    policy_version_id=price_policy_version["version_id"],
                    derived_observation_id=verified_observation_id,
                    supersedes_decision_id=None,
                    is_current=True,
                    payload={"test_fixture": "Calculation test pricing gate"},
                    created_at=now,
                )
            )
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

        calculation_payload = {
            "expected_row_version": current["row_version"],
            "inputs": [
                {
                    "cost_input_id": "pipe-cost",
                    "line_id": line_id,
                    "wbs_node_id": "wbs-1",
                    "semantic_key": "pipe",
                    "category": "MATERIAL",
                    "quantity": "125.50",
                    "unit": "m",
                    "unit_rate": "125.50",
                    "currency": "RUB",
                    "source_observation_id": verified_observation_id,
                },
                {
                    "cost_input_id": "risk-reserve-cost",
                    "line_id": "boq-line-risk-test",
                    "wbs_node_id": "wbs-risk",
                    "semantic_key": "risk-reserve",
                    "category": "RISK",
                    "quantity": "1",
                    "unit": "project",
                    "unit_rate": "10",
                    "currency": "RUB",
                    "risk_reserve_id": "risk-calculation-test",
                },
            ],
            "policy": {
                "policy_version": version["version_id"],
                "currency": "RUB",
                "line_rounding_scale": 2,
                "total_rounding_scale": 2,
                "rounding_mode": "ROUND_HALF_UP",
                "independent_tolerance": "0",
                "expected_semantic_keys": ["pipe", "risk-reserve"],
            },
            "reason": "Calculate reviewed atomic inputs",
        }
        fake_source_payload = {
            **calculation_payload,
            "inputs": [
                {
                    **calculation_payload["inputs"][0],
                    "source_observation_id": "invented-source",
                },
                calculation_payload["inputs"][1],
            ],
        }
        rejected = client.post(
            f"/v1/projects/{project['id']}/calculations",
            headers=operator,
            json=fake_source_payload,
        )
        assert rejected.status_code == 422
        assert "no verified observation" in rejected.json()["detail"]

        mismatched_quantity_payload = {
            **calculation_payload,
            "inputs": [
                {
                    **calculation_payload["inputs"][0],
                    "quantity": "10",
                },
                calculation_payload["inputs"][1],
            ],
        }
        rejected_quantity = client.post(
            f"/v1/projects/{project['id']}/calculations",
            headers=operator,
            json=mismatched_quantity_payload,
        )
        assert rejected_quantity.status_code == 422
        assert "current verified BoQ quantity" in rejected_quantity.json()["detail"]

        calculated = client.post(
            f"/v1/projects/{project['id']}/calculations",
            headers=operator,
            json=calculation_payload,
        )
        assert calculated.status_code == 201, calculated.text
        result = calculated.json()
        assert result["primary"]["grand_total"] == "15760.25"
        assert result["independent"]["passed"] is True
        assert result["snapshot"]["fixed"] is True
        assert result["project"]["state"] == "INDEPENDENT_VALIDATION"

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
        assert evidence["basis_id"] == verified_observation_id
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
