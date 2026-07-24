from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tenderguard.api.main import create_app
from tenderguard.config import Settings
from tenderguard.infrastructure.database import (
    CURRENT_SCHEMA_REVISION,
    create_database_engine,
    create_schema_for_tests,
    create_session_factory,
)
from tenderguard.infrastructure.object_store import LocalObjectStore
from tenderguard.infrastructure.orm import AdapterQualificationRow


def test_readiness_schema_revision_matches_alembic_head() -> None:
    scripts = ScriptDirectory.from_config(Config("alembic.ini"))
    assert scripts.get_current_head() == CURRENT_SCHEMA_REVISION


def test_production_rejects_development_audit_key_and_sqlite() -> None:
    with pytest.raises(ValidationError) as error:
        Settings(
            app_env="production",
            database_url="sqlite+pysqlite:///unsafe.db",
            object_store_backend="s3",
            oidc_issuer="https://id.example/realm",
            oidc_audience="tenderguard",
            oidc_jwks_url="https://id.example/jwks",
            s3_bucket="bucket",
            s3_access_key="access",
            s3_secret_key="secret",
            trusted_hosts=["tenderguard.example"],
        )
    message = str(error.value)
    assert "development audit signing key" in message
    assert "Ed25519 export signing key" in message
    assert "PostgreSQL is required" in message
    assert "qualified malware scanner binding" in message
    assert "qualified isolated document processor binding" in message


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        (
            {"document_job_lease_seconds": 30, "document_job_timeout_seconds": 30},
            "timeout must be shorter",
        ),
        (
            {
                "document_job_retry_base_seconds": 60,
                "document_job_retry_max_seconds": 30,
            },
            "retry maximum",
        ),
    ],
)
def test_document_job_timing_configuration_fails_closed(
    overrides: dict[str, int],
    expected: str,
) -> None:
    with pytest.raises(ValidationError, match=expected):
        Settings(**overrides)


def test_production_docs_are_disabled_and_security_headers_are_present(
    tmp_path: Path,
) -> None:
    settings = Settings(
        app_env="production",
        database_url="postgresql+psycopg://example.invalid/tenderguard",
        object_store_backend="s3",
        oidc_issuer="https://id.example/realm",
        oidc_audience="tenderguard",
        oidc_jwks_url="https://id.example/jwks",
        s3_bucket="bucket",
        s3_quarantine_bucket="quarantine-bucket",
        s3_access_key="access",
        s3_secret_key="secret",
        audit_signing_key="production-test-signing-key-32-bytes-minimum",
        export_signing_key_id="test-export-key-1",
        export_signing_private_key_b64="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        malware_scanner_adapter="production-scanner",
        malware_scanner_qualification_id="qualification-malware-production",
        document_processor_adapter="production-intake",
        document_processor_qualification_id="qualification-intake-production",
        document_worker_actor_id="document-worker",
        trusted_hosts=["testserver"],
    )
    test_db_settings = Settings(
        app_env="test",
        database_url="sqlite+pysqlite://",
    )
    engine = create_database_engine(test_db_settings)
    create_schema_for_tests(engine)
    now = datetime.now(UTC)
    with create_session_factory(engine).begin() as session:
        session.add_all(
            [
                AdapterQualificationRow(
                    id="qualification-malware-production",
                    adapter_name="production-scanner",
                    adapter_version="1",
                    status="APPROVED",
                    valid_until=None,
                    test_evidence_hash="a" * 64,
                    payload={
                        "organization_id": "org-1",
                        "supported_methods": ["MALWARE_SCAN"],
                        "service_actor_id": "malware-scanner",
                    },
                    approved_by="owner-2",
                    approved_at=now,
                ),
                AdapterQualificationRow(
                    id="qualification-intake-production",
                    adapter_name="production-intake",
                    adapter_version="1",
                    status="APPROVED",
                    valid_until=None,
                    test_evidence_hash="b" * 64,
                    payload={
                        "organization_id": "org-1",
                        "supported_methods": ["DOCUMENT_INTAKE"],
                        "service_actor_id": "document-worker",
                    },
                    approved_by="owner-2",
                    approved_at=now,
                ),
            ]
        )
    app = create_app(
        settings,
        engine=engine,
        object_store=LocalObjectStore(tmp_path / "objects"),
        quarantine_store=LocalObjectStore(tmp_path / "quarantine"),
    )
    with TestClient(app) as client:
        docs = client.get("/docs")
        assert docs.status_code == 404
        live = client.get("/health/live")
        assert live.status_code == 200
        assert live.headers["x-content-type-options"] == "nosniff"
        assert live.headers["x-frame-options"] == "DENY"
        assert live.headers["cache-control"] == "no-store"
        ready = client.get("/health/ready")
        assert ready.status_code == 200
        assert ready.json()["ready"] is True
        assert ready.json()["schema_current"] is True
        assert ready.json()["quarantine_store"] is True
        assert ready.json()["malware_scanner_qualified"] is True
        assert ready.json()["document_processor_qualified"] is True
        assert ready.json()["export_signing_configured"] is True


def test_readiness_returns_503_when_authentication_is_not_configured(
    tmp_path: Path,
) -> None:
    settings = Settings(
        app_env="test",
        database_url="sqlite+pysqlite://",
    )
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    app = create_app(
        settings,
        engine=engine,
        object_store=LocalObjectStore(tmp_path / "objects"),
    )
    with TestClient(app) as client:
        ready = client.get("/health/ready")
        assert ready.status_code == 503
        assert ready.json()["ready"] is False
        assert ready.json()["authentication_configured"] is False
        assert ready.json()["export_signing_configured"] is False


def test_readiness_returns_503_when_migrations_are_missing(tmp_path: Path) -> None:
    settings = Settings(
        app_env="test",
        database_url="sqlite+pysqlite://",
    )
    engine = create_database_engine(settings)
    app = create_app(
        settings,
        engine=engine,
        object_store=LocalObjectStore(tmp_path / "objects"),
        quarantine_store=LocalObjectStore(tmp_path / "quarantine"),
    )
    with TestClient(app) as client:
        ready = client.get("/health/ready")
        assert ready.status_code == 503
        assert ready.json()["database"] is True
        assert ready.json()["schema_current"] is False
        assert "database migration state is unavailable" in ready.json()["notes"]


def test_readiness_rejects_invalid_export_signing_key(tmp_path: Path) -> None:
    settings = Settings(
        app_env="test",
        database_url="sqlite+pysqlite://",
        allow_insecure_dev_auth=True,
        export_signing_key_id="invalid-export-key",
        export_signing_private_key_b64="bm90LWEtMzItYnl0ZS1rZXk=",
    )
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    app = create_app(
        settings,
        engine=engine,
        object_store=LocalObjectStore(tmp_path / "objects"),
    )
    with TestClient(app) as client:
        ready = client.get("/health/ready")
        assert ready.status_code == 503
        assert ready.json()["export_signing_configured"] is False
        assert "Ed25519 export signing key configuration is invalid" in ready.json()["notes"]


def test_scan_result_route_has_a_smaller_declared_body_limit(tmp_path: Path) -> None:
    settings = Settings(
        app_env="test",
        database_url="sqlite+pysqlite://",
        max_scan_report_bytes=128,
    )
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    app = create_app(
        settings,
        engine=engine,
        object_store=LocalObjectStore(tmp_path / "objects"),
        quarantine_store=LocalObjectStore(tmp_path / "quarantine"),
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/projects/project/document-uploads/upload/scan-results",
            headers={"Content-Length": str(128 + 64 * 1024 + 1)},
            content=b"{}",
        )
        assert response.status_code == 413
