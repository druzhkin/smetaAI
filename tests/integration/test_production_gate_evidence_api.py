from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tenderguard.api.main import create_app
from tenderguard.config import Settings
from tenderguard.infrastructure.database import (
    create_database_engine,
    create_schema_for_tests,
)
from tenderguard.infrastructure.object_store import LocalObjectStore


def test_production_gate_evidence_api_is_wired_and_fail_closed(
    tmp_path: Path,
) -> None:
    settings = Settings(
        app_env="test",
        application_build_reference="git:" + ("7" * 40),
        database_url="sqlite+pysqlite://",
        local_object_store_path=tmp_path / "objects",
        local_quarantine_store_path=tmp_path / "quarantine",
        allow_insecure_dev_auth=True,
        audit_signing_key="production-evidence-api-audit-key-at-least-32-bytes",
    )
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    app = create_app(
        settings,
        engine=engine,
        object_store=LocalObjectStore(tmp_path / "objects"),
        quarantine_store=LocalObjectStore(tmp_path / "quarantine"),
    )
    expected_paths = {
        "/v1/qualification/production-evidence/packages",
        "/v1/qualification/production-evidence/packages/{package_id}",
        "/v1/qualification/production-evidence/packages/{package_id}/review",
        "/v1/qualification/production-evidence/packages/{package_id}/revoke",
    }
    assert expected_paths <= set(app.openapi()["paths"])
    headers = {
        "X-Dev-Actor": "qualification-auditor",
        "X-Dev-Organization": "org-api",
        "X-Dev-Roles": "AUDITOR",
        "Idempotency-Key": "production-evidence-api-invalid-hash",
    }
    with TestClient(app) as client:
        invalid = client.post(
            "/v1/qualification/production-evidence/packages",
            headers=headers,
            json={
                "submission": {
                    "profile_version_id": "profile-1",
                    "profile_content_hash": "not-a-hash",
                    "environment": "qualification",
                    "executed_by": "control-team",
                    "started_at": "2026-07-24T10:00:00Z",
                    "completed_at": "2026-07-24T11:00:00Z",
                    "artifacts": [],
                    "claims": {},
                },
                "reason": "Invalid unbound evidence must be rejected",
            },
        )
        assert invalid.status_code == 422

        missing = client.get(
            "/v1/qualification/production-evidence/packages/missing-package",
            headers=headers,
        )
        assert missing.status_code == 404
