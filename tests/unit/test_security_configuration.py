from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tenderguard.api.main import create_app
from tenderguard.config import Settings
from tenderguard.infrastructure.database import (
    create_database_engine,
    create_schema_for_tests,
)
from tenderguard.infrastructure.object_store import LocalObjectStore


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
        s3_access_key="access",
        s3_secret_key="secret",
        audit_signing_key="production-test-signing-key-32-bytes-minimum",
        export_signing_key_id="test-export-key-1",
        export_signing_private_key_b64="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        trusted_hosts=["testserver"],
    )
    test_db_settings = Settings(
        app_env="test",
        database_url="sqlite+pysqlite://",
    )
    engine = create_database_engine(test_db_settings)
    create_schema_for_tests(engine)
    app = create_app(
        settings,
        engine=engine,
        object_store=LocalObjectStore(tmp_path / "objects"),
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
