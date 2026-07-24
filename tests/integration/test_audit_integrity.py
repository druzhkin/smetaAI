import base64
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from sqlalchemy import update

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
    create_database_engine,
    create_schema_for_tests,
    create_session_factory,
)
from tenderguard.infrastructure.object_store import LocalObjectStore
from tenderguard.infrastructure.orm import AuditAnchorReceiptRow, AuditEventRow

AUDIT_KEY_ID = "audit-key-2026-01"
AUDIT_KEY = "test-audit-integrity-key-at-least-32-bytes"


def _anchor_material() -> tuple[Ed25519PrivateKey, str]:
    private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return private_key, base64.b64encode(public_key).decode("ascii")


def _settings(tmp_path: Path) -> tuple[Settings, Ed25519PrivateKey]:
    private_key, public_key_b64 = _anchor_material()
    return (
        Settings(
            app_env="test",
            database_url="sqlite+pysqlite://",
            local_object_store_path=tmp_path / "objects",
            allow_insecure_dev_auth=True,
            audit_signing_key_id=AUDIT_KEY_ID,
            audit_signing_key=AUDIT_KEY,
            audit_anchor_provider_id="test-transparency-provider",
            audit_anchor_provider_key_id="test-anchor-key-1",
            audit_anchor_public_key_b64=public_key_b64,
            audit_anchor_max_age_seconds=3600,
            audit_operator_organization_id="org-audit",
        ),
        private_key,
    )


def _actor(actor_id: str, *roles: ActorRole) -> Actor:
    return Actor(
        actor_id=actor_id,
        organization_id="org-audit",
        roles=frozenset(roles),
    )


def _signature(
    *,
    private_key: Ed25519PrivateKey,
    settings: Settings,
    checkpoint_hash: str,
    anchored_at: datetime,
    external_reference: str,
) -> str:
    assert settings.audit_anchor_provider_id is not None
    assert settings.audit_anchor_provider_key_id is not None
    statement = AuditAnchorStatement(
        schema_version=AUDIT_ANCHOR_RECEIPT_SCHEMA_VERSION,
        provider_id=settings.audit_anchor_provider_id,
        provider_key_id=settings.audit_anchor_provider_key_id,
        checkpoint_hash=checkpoint_hash,
        anchored_at=anchored_at,
        external_reference=external_reference,
    )
    return base64.b64encode(private_key.sign(canonical_json(statement))).decode("ascii")


def test_external_anchor_requires_four_eyes_and_detects_receipt_tampering(
    tmp_path: Path,
) -> None:
    settings, private_key = _settings(tmp_path)
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    store = LocalObjectStore(tmp_path / "objects")
    sessions = create_session_factory(engine)
    creator = _actor("admin-checkpoint", ActorRole.ADMIN)
    registrar = _actor("admin-receipt", ActorRole.ADMIN)
    estimator = _actor("estimator-audit", ActorRole.ESTIMATOR)

    with sessions.begin() as session:
        ProjectService(
            session=session,
            settings=settings,
            object_store=store,
        ).create_project(
            actor=estimator,
            code="AUD-001",
            name="Anchored audit trail",
            request_id="request-project",
            reason="Create an auditable project",
        )
    with sessions.begin() as session:
        checkpoint = AuditIntegrityService(
            session=session,
            settings=settings,
            object_store=store,
        ).create_checkpoint(
            actor=creator,
            request_id="request-checkpoint",
            reason="Create daily immutable checkpoint",
        )
    anchored_at = datetime.now(UTC)
    external_reference = "transparency-log-entry-0001"
    signature = _signature(
        private_key=private_key,
        settings=settings,
        checkpoint_hash=checkpoint.checkpoint_hash,
        anchored_at=anchored_at,
        external_reference=external_reference,
    )
    with sessions.begin() as session:
        service = AuditIntegrityService(
            session=session,
            settings=settings,
            object_store=store,
        )
        with pytest.raises(ValueError, match="different administrator"):
            service.register_receipt(
                actor=creator,
                checkpoint_id=checkpoint.checkpoint_id,
                anchored_at=anchored_at,
                external_reference=external_reference,
                signature_b64=signature,
                request_id="request-same-admin",
                reason="Must be rejected",
            )
        receipt = service.register_receipt(
            actor=registrar,
            checkpoint_id=checkpoint.checkpoint_id,
            anchored_at=anchored_at,
            external_reference=external_reference,
            signature_b64=signature,
            request_id="request-receipt",
            reason="Register independently issued receipt",
        )
        assert service.anchor_status().valid
        repeated = service.register_receipt(
            actor=registrar,
            checkpoint_id=checkpoint.checkpoint_id,
            anchored_at=anchored_at,
            external_reference=external_reference,
            signature_b64=signature,
            request_id="request-receipt-repeated",
            reason="Retry the exact receipt registration",
        )
        assert repeated.receipt_id == receipt.receipt_id
        with pytest.raises(ValueError, match="different external anchor receipt"):
            service.register_receipt(
                actor=registrar,
                checkpoint_id=checkpoint.checkpoint_id,
                anchored_at=anchored_at,
                external_reference="different-reference",
                signature_b64=signature,
                request_id="request-receipt-conflict",
                reason="Must reject conflicting retry",
            )

    with sessions.begin() as session:
        row = session.get(AuditAnchorReceiptRow, receipt.receipt_id)
        assert row is not None
        row.external_reference = "attacker-replaced-reference"
    with sessions() as session:
        status = AuditIntegrityService(
            session=session,
            settings=settings,
            object_store=store,
        ).anchor_status()
        assert not status.valid
        assert "Audit anchor receipt hash verification failed" in status.reasons
    with sessions.begin() as session:
        receipt_row = session.get(AuditAnchorReceiptRow, receipt.receipt_id)
        assert receipt_row is not None
        receipt_row.external_reference = external_reference
        project_event = session.scalar(
            AuditEventRow.__table__.select()
            .with_only_columns(AuditEventRow.id)
            .where(AuditEventRow.aggregate_type == "project")
            .order_by(AuditEventRow.sequence)
            .limit(1)
        )
        assert project_event is not None
        session.execute(
            update(AuditEventRow)
            .where(AuditEventRow.id == project_event)
            .values(reason="privileged-database-tampering")
        )
    with sessions() as session:
        status = AuditIntegrityService(
            session=session,
            settings=settings,
            object_store=store,
        ).anchor_status()
        assert not status.valid
        assert "Current audit chain verification failed" in status.reasons


def test_checkpoint_creation_blocks_a_tampered_audit_chain(tmp_path: Path) -> None:
    settings, _ = _settings(tmp_path)
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    store = LocalObjectStore(tmp_path / "objects")
    sessions = create_session_factory(engine)
    with sessions.begin() as session:
        ProjectService(
            session=session,
            settings=settings,
            object_store=store,
        ).create_project(
            actor=_actor("estimator-audit", ActorRole.ESTIMATOR),
            code="AUD-002",
            name="Tamper test",
            request_id="request-project",
            reason="Create an auditable project",
        )
    with sessions.begin() as session:
        first_event_id = session.scalar(
            AuditEventRow.__table__.select()
            .with_only_columns(AuditEventRow.id)
            .order_by(AuditEventRow.sequence)
            .limit(1)
        )
        assert first_event_id is not None
        session.execute(
            update(AuditEventRow)
            .where(AuditEventRow.id == first_event_id)
            .values(reason="attacker-mutated-reason")
        )
    with (
        sessions.begin() as session,
        pytest.raises(ValueError, match="audit chain does not verify"),
    ):
        AuditIntegrityService(
            session=session,
            settings=settings,
            object_store=store,
        ).create_checkpoint(
            actor=_actor("admin-audit", ActorRole.ADMIN),
            request_id="request-checkpoint",
            reason="Must fail closed",
        )


def test_audit_anchor_api_rejects_non_admin_and_invalid_signature(tmp_path: Path) -> None:
    settings, _ = _settings(tmp_path)
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    store = LocalObjectStore(tmp_path / "objects")
    sessions = create_session_factory(engine)
    with sessions.begin() as session:
        ProjectService(
            session=session,
            settings=settings,
            object_store=store,
        ).create_project(
            actor=_actor("estimator-api", ActorRole.ESTIMATOR),
            code="AUD-003",
            name="Audit API test",
            request_id="request-project",
            reason="Create an auditable project",
        )
    app = create_app(settings, engine=engine, object_store=store)
    with TestClient(app) as client:
        non_admin = client.post(
            "/v1/audit/checkpoints",
            headers={
                "X-Dev-Actor": "estimator-api",
                "X-Dev-Organization": "org-audit",
                "X-Dev-Roles": "ESTIMATOR",
            },
            json={"reason": "Not authorized"},
        )
        assert non_admin.status_code == 403
        wrong_organization = client.post(
            "/v1/audit/checkpoints",
            headers={
                "X-Dev-Actor": "admin-wrong-organization",
                "X-Dev-Organization": "org-other",
                "X-Dev-Roles": "ADMIN",
            },
            json={"reason": "Must not control global audit integrity"},
        )
        assert wrong_organization.status_code == 403
        created = client.post(
            "/v1/audit/checkpoints",
            headers={
                "X-Dev-Actor": "admin-one",
                "X-Dev-Organization": "org-audit",
                "X-Dev-Roles": "ADMIN",
            },
            json={"reason": "Create checkpoint through governed API"},
        )
        assert created.status_code == 201
        rejected = client.post(
            f"/v1/audit/checkpoints/{created.json()['checkpoint_id']}/receipts",
            headers={
                "X-Dev-Actor": "admin-two",
                "X-Dev-Organization": "org-audit",
                "X-Dev-Roles": "ADMIN",
            },
            json={
                "anchored_at": datetime.now(UTC).isoformat(),
                "external_reference": "invalid-signature-test",
                "signature_b64": base64.b64encode(b"x" * 64).decode("ascii"),
                "reason": "Must fail signature verification",
            },
        )
        assert rejected.status_code == 422
