import base64
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tenderguard.api.main import create_app
from tenderguard.application.audit_integrity import AuditIntegrityService
from tenderguard.application.projects import ProjectService
from tenderguard.config import Settings
from tenderguard.domain.audit_anchor import (
    AUDIT_ANCHOR_RECEIPT_SCHEMA_VERSION,
    AuditAnchorStatement,
)
from tenderguard.domain.common import canonical_json
from tenderguard.domain.enums import ActorRole
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.database import (
    CURRENT_SCHEMA_REVISION,
    create_database_engine,
    create_schema_for_tests,
    create_session_factory,
)
from tenderguard.infrastructure.object_store import (
    LocalObjectStore,
    ObjectStoreRetentionStatus,
)
from tenderguard.infrastructure.orm import AdapterQualificationRow


class _WormTestObjectStore(LocalObjectStore):
    def retention_status(self) -> ObjectStoreRetentionStatus:
        return ObjectStoreRetentionStatus(
            versioning_enabled=True,
            object_lock_enabled=True,
            default_retention_mode="COMPLIANCE",
            default_retention_days=3650,
        )


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
    assert "external audit anchor configuration" in message
    assert "object-lock retention policy" in message
    assert "persisted idempotency keys" in message
    assert "Ed25519 export signing key" in message
    assert "Ed25519 integration signing key" in message
    assert "integration receipt receiver ID" in message
    assert "integration operator organization" in message
    assert "PostgreSQL is required" in message
    assert "qualified malware scanner binding" in message
    assert "qualified isolated document processor binding" in message
    assert "OIDC web client ID" in message


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
        (
            {"integration_job_lease_seconds": 30, "integration_job_timeout_seconds": 30},
            "Integration job timeout must be shorter",
        ),
        (
            {
                "integration_job_timeout_seconds": 30,
                "integration_http_timeout_seconds": 31,
            },
            "Integration HTTP timeout",
        ),
        (
            {
                "integration_job_retry_base_seconds": 60,
                "integration_job_retry_max_seconds": 30,
            },
            "retry maximum",
        ),
    ],
)
def test_job_timing_configuration_fails_closed(
    overrides: dict[str, int],
    expected: str,
) -> None:
    with pytest.raises(ValidationError, match=expected):
        Settings(**overrides)


def test_production_docs_are_disabled_and_security_headers_are_present(
    tmp_path: Path,
) -> None:
    anchor_private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    anchor_public_key_b64 = base64.b64encode(
        anchor_private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    ).decode("ascii")
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
        audit_signing_key_id="production-audit-key-1",
        audit_anchor_provider_id="test-transparency-provider",
        audit_anchor_provider_key_id="test-transparency-key-1",
        audit_anchor_public_key_b64=anchor_public_key_b64,
        audit_anchor_max_age_seconds=3600,
        audit_operator_organization_id="org-1",
        require_idempotency_keys=True,
        export_signing_key_id="test-export-key-1",
        export_signing_private_key_b64="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        integration_signing_key_id="test-integration-key-1",
        integration_signing_private_key_b64="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        integration_receiver_id="tenderguard-production",
        integration_operator_organization_id="org-1",
        s3_required_object_lock_mode="GOVERNANCE",
        s3_minimum_retention_days=365,
        normative_adapter="production-normative-engine",
        normative_adapter_qualification_id="qualification-normative-production",
        malware_scanner_adapter="production-scanner",
        malware_scanner_qualification_id="qualification-malware-production",
        document_processor_adapter="production-intake",
        document_processor_qualification_id="qualification-intake-production",
        document_worker_actor_id="document-worker",
        trusted_hosts=["testserver"],
        operator_ui_enabled=False,
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
                    id="qualification-normative-production",
                    adapter_name="production-normative-engine",
                    adapter_version="1",
                    status="APPROVED",
                    valid_until=None,
                    test_evidence_hash="c" * 64,
                    payload={
                        "organization_id": "org-1",
                        "supported_methods": ["NORMATIVE_CALCULATION"],
                        "service_actor_id": "normative-engine",
                    },
                    approved_by="owner-2",
                    approved_at=now,
                ),
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
                AdapterQualificationRow(
                    id="qualification-integration-production",
                    adapter_name="production-integration-gateway",
                    adapter_version="1",
                    status="APPROVED",
                    valid_until=None,
                    test_evidence_hash="d" * 64,
                    payload={
                        "organization_id": "org-1",
                        "supported_methods": [
                            "INTEGRATION_OUTBOUND_DELIVERY",
                            "INTEGRATION_INBOUND_SOURCE",
                            "INTEGRATION_INBOX_HANDLER",
                        ],
                        "service_actor_id": "integration-worker",
                        "outbound_topics": ["audit.event.recorded"],
                        "inbound_topics": ["price.quote.received"],
                        "receipt_signing_key_id": "remote-receipt-key-1",
                        "receipt_public_key_b64": anchor_public_key_b64,
                        "receiver_id": "enterprise-integration-ledger",
                        "inbound_signing_key_id": "remote-source-key-1",
                        "inbound_signing_public_key_b64": anchor_public_key_b64,
                    },
                    approved_by="owner-2",
                    approved_at=now,
                ),
            ]
        )
    store = _WormTestObjectStore(tmp_path / "objects")
    sessions = create_session_factory(engine)
    with sessions.begin() as session:
        ProjectService(
            session=session,
            settings=settings,
            object_store=store,
        ).create_project(
            actor=Actor(
                actor_id="readiness-estimator",
                organization_id="org-1",
                roles=frozenset({ActorRole.ESTIMATOR}),
            ),
            code="READY-001",
            name="Production readiness audit source",
            request_id="request-readiness-project",
            reason="Create audit evidence for readiness",
        )
    with sessions.begin() as session:
        checkpoint = AuditIntegrityService(
            session=session,
            settings=settings,
            object_store=store,
        ).create_checkpoint(
            actor=Actor(
                actor_id="readiness-admin-one",
                organization_id="org-1",
                roles=frozenset({ActorRole.ADMIN}),
            ),
            request_id="request-readiness-checkpoint",
            reason="Create readiness checkpoint",
        )
    anchored_at = datetime.now(UTC)
    assert settings.audit_anchor_provider_id is not None
    assert settings.audit_anchor_provider_key_id is not None
    statement = AuditAnchorStatement(
        schema_version=AUDIT_ANCHOR_RECEIPT_SCHEMA_VERSION,
        provider_id=settings.audit_anchor_provider_id,
        provider_key_id=settings.audit_anchor_provider_key_id,
        checkpoint_hash=checkpoint.checkpoint_hash,
        anchored_at=anchored_at,
        external_reference="readiness-transparency-entry",
    )
    signature_b64 = base64.b64encode(anchor_private_key.sign(canonical_json(statement))).decode(
        "ascii"
    )
    with sessions.begin() as session:
        AuditIntegrityService(
            session=session,
            settings=settings,
            object_store=store,
        ).register_receipt(
            actor=Actor(
                actor_id="readiness-admin-two",
                organization_id="org-1",
                roles=frozenset({ActorRole.ADMIN}),
            ),
            checkpoint_id=checkpoint.checkpoint_id,
            anchored_at=anchored_at,
            external_reference=statement.external_reference,
            signature_b64=signature_b64,
            request_id="request-readiness-receipt",
            reason="Register independent readiness receipt",
        )
    app = create_app(
        settings,
        engine=engine,
        object_store=store,
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
        assert "default-src 'self'" in live.headers["content-security-policy"]
        assert live.headers["cross-origin-opener-policy"] == "same-origin"
        assert live.headers["cross-origin-resource-policy"] == "same-origin"
        assert live.headers["strict-transport-security"].startswith("max-age=31536000")
        ready = client.get("/health/ready")
        assert ready.status_code == 200
        assert ready.json()["ready"] is True
        assert ready.json()["schema_current"] is True
        assert ready.json()["quarantine_store"] is True
        assert ready.json()["operator_ui"] is True
        assert ready.json()["object_store_worm"] is True
        assert ready.json()["audit_anchor_valid"] is True
        assert ready.json()["idempotency_enforced"] is True
        assert ready.json()["normative_engine_qualified"] is True
        assert ready.json()["malware_scanner_qualified"] is True
        assert ready.json()["document_processor_qualified"] is True
        assert ready.json()["export_signing_configured"] is True
        assert ready.json()["integration_signing_configured"] is True
        assert ready.json()["integration_connectors_qualified"] is True
        with sessions.begin() as session:
            integration_qualification = session.get(
                AdapterQualificationRow,
                "qualification-integration-production",
            )
            assert integration_qualification is not None
            changed_payload = dict(integration_qualification.payload)
            changed_payload.pop("service_actor_id")
            integration_qualification.payload = changed_payload
        not_ready = client.get("/health/ready")
        assert not_ready.status_code == 503
        assert not_ready.json()["integration_connectors_qualified"] is False


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


def test_runtime_config_exposes_only_public_browser_authentication_settings(
    tmp_path: Path,
) -> None:
    settings = Settings(
        app_env="test",
        database_url="sqlite+pysqlite://",
        oidc_issuer="https://identity.example/realms/tenderguard",
        oidc_audience="tenderguard-api",
        oidc_jwks_url="https://identity.example/realms/tenderguard/jwks",
        oidc_web_client_id="tenderguard-operator-ui",
        audit_signing_key="private-test-value-that-must-not-be-exposed",
    )
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    app = create_app(
        settings,
        engine=engine,
        object_store=LocalObjectStore(tmp_path / "objects"),
    )
    with TestClient(app) as client:
        response = client.get("/v1/runtime-config")
        assert response.status_code == 200
        assert response.json() == {
            "environment": "test",
            "authentication_mode": "OIDC",
            "oidc_authority": "https://identity.example/realms/tenderguard",
            "oidc_client_id": "tenderguard-operator-ui",
            "oidc_scope": "openid profile email",
            "api_base_path": "/v1",
            "application_version": "0.1.0",
            "max_upload_bytes": 524288000,
        }
        assert "private-test-value" not in response.text
        assert "https://identity.example" in response.headers["content-security-policy"]
    engine.dispose()


def test_operator_ui_serves_only_declared_spa_routes_and_assets(tmp_path: Path) -> None:
    ui_dist = tmp_path / "operator-ui"
    assets = ui_dist / "assets"
    assets.mkdir(parents=True)
    (ui_dist / "index.html").write_text(
        "<!doctype html><title>TenderGuard</title>",
        encoding="utf-8",
    )
    (ui_dist / "favicon.svg").write_text("<svg></svg>", encoding="utf-8")
    (assets / "app.js").write_text("export {};", encoding="utf-8")
    settings = Settings(
        app_env="test",
        database_url="sqlite+pysqlite://",
        operator_ui_dist_path=ui_dist,
    )
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    app = create_app(
        settings,
        engine=engine,
        object_store=LocalObjectStore(tmp_path / "objects"),
    )
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/tasks").status_code == 200
        assert client.get("/tasks/approval-task-1").status_code == 200
        assert client.get("/projects/project-1/BOQ_SCOPE").status_code == 200
        assert client.get("/auth/callback").status_code == 200
        assert client.get("/assets/app.js").text == "export {};"
        assert client.get("/favicon.svg").headers["content-type"].startswith("image/svg+xml")
        assert client.get("/v1/not-an-api-route").status_code == 404
        assert client.get("/not-an-ui-route").status_code == 404
    engine.dispose()


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


def test_chunked_api_body_is_bounded_without_content_length(tmp_path: Path) -> None:
    settings = Settings(
        app_env="test",
        database_url="sqlite+pysqlite://",
        max_api_request_bytes=32,
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
        request = client.build_request(
            "POST",
            "/v1/projects",
            headers={"Content-Type": "application/json"},
            content=(chunk for chunk in (b"{" + b"x" * 20, b"x" * 20 + b"}")),
        )
        assert "content-length" not in request.headers
        response = client.send(request)
        assert response.status_code == 413
