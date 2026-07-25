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


def test_business_qualification_api_is_wired_and_validates_governed_hashes(
    tmp_path: Path,
) -> None:
    settings = Settings(
        app_env="test",
        application_build_reference="git:" + ("8" * 40),
        database_url="sqlite+pysqlite://",
        local_object_store_path=tmp_path / "objects",
        local_quarantine_store_path=tmp_path / "quarantine",
        allow_insecure_dev_auth=True,
        audit_signing_key="business-qualification-api-audit-key-32-bytes",
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
        "/v1/qualification/business/campaigns",
        "/v1/qualification/business/campaigns/{campaign_id}",
        ("/v1/qualification/business/campaigns/{campaign_id}/cases/{case_id}/references/prepare"),
        ("/v1/qualification/business/campaigns/{campaign_id}/cases/{case_id}/references/verify"),
        "/v1/qualification/business/campaigns/{campaign_id}/evaluate",
        (
            "/v1/qualification/business/campaigns/{campaign_id}"
            "/discrepancies/{discrepancy_id}/review"
        ),
        "/v1/qualification/business/campaigns/{campaign_id}/approve",
    }
    assert expected_paths <= set(app.openapi()["paths"])
    headers = {
        "X-Dev-Actor": "qualification-auditor",
        "X-Dev-Organization": "org-api",
        "X-Dev-Roles": "AUDITOR",
        "Idempotency-Key": "business-qualification-api-invalid-hash",
    }
    with TestClient(app) as client:
        invalid = client.post(
            "/v1/qualification/business/campaigns",
            headers=headers,
            json={
                "profile_version_id": "profile-1",
                "profile_content_hash": "not-a-hash",
                "dataset_version_id": "dataset-1",
                "dataset_content_hash": "1" * 64,
                "reason": "Schema validation must reject unbound profile hashes",
            },
        )
        assert invalid.status_code == 422

        missing = client.get(
            "/v1/qualification/business/campaigns/missing-campaign",
            headers=headers,
        )
        assert missing.status_code == 404
