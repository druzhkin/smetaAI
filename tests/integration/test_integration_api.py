import base64
from datetime import UTC, datetime
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from tenderguard.api.main import create_app
from tenderguard.config import Settings
from tenderguard.domain.common import content_hash
from tenderguard.domain.integration import (
    build_signed_integration_envelope,
    load_integration_signing_material,
)
from tenderguard.infrastructure.database import (
    create_database_engine,
    create_schema_for_tests,
    create_session_factory,
)
from tenderguard.infrastructure.object_store import LocalObjectStore
from tenderguard.infrastructure.orm import AdapterQualificationRow


def _private_key_b64(seed: int) -> str:
    key = Ed25519PrivateKey.from_private_bytes(bytes([seed]) * 32)
    return base64.b64encode(
        key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
    ).decode("ascii")


def test_integration_api_persists_replays_and_settles_signed_inbox(
    tmp_path: Path,
) -> None:
    settings = Settings(
        app_env="test",
        database_url="sqlite+pysqlite://",
        local_object_store_path=tmp_path / "objects",
        allow_insecure_dev_auth=True,
        require_idempotency_keys=True,
        audit_signing_key="integration-api-audit-key-at-least-32-bytes",
        integration_signing_key_id="tenderguard-integration-api-key",
        integration_signing_private_key_b64=_private_key_b64(20),
        integration_receiver_id="tenderguard-api",
    )
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    factory = create_session_factory(engine)
    source_signing = load_integration_signing_material(
        key_id="source-api-key",
        private_key_b64=_private_key_b64(21),
    )
    now = datetime.now(UTC)
    with factory.begin() as session:
        session.add_all(
            (
                AdapterQualificationRow(
                    id="qualification-source-api",
                    adapter_name="source-api",
                    adapter_version="1",
                    status="APPROVED",
                    valid_until=None,
                    test_evidence_hash="a" * 64,
                    payload={
                        "organization_id": "org-1",
                        "service_actor_id": "source-api-worker",
                        "supported_methods": ["INTEGRATION_INBOUND_SOURCE"],
                        "inbound_topics": ["price.quote.received"],
                        "inbound_signing_key_id": source_signing.key_id,
                        "inbound_signing_public_key_b64": (source_signing.public_key_b64),
                    },
                    approved_by="methodology-owner",
                    approved_at=now,
                ),
                AdapterQualificationRow(
                    id="qualification-handler-api",
                    adapter_name="handler-api",
                    adapter_version="1",
                    status="APPROVED",
                    valid_until=None,
                    test_evidence_hash="b" * 64,
                    payload={
                        "organization_id": "org-1",
                        "service_actor_id": "handler-api-worker",
                        "supported_methods": ["INTEGRATION_INBOX_HANDLER"],
                        "inbound_topics": ["price.quote.received"],
                    },
                    approved_by="methodology-owner",
                    approved_at=now,
                ),
            )
        )
    envelope = build_signed_integration_envelope(
        message_id="source-api-message-1",
        delivery_deduplication_key="source-api-delivery-1",
        topic="price.quote.received",
        aggregate_id="rfq-api-1",
        organization_id="org-1",
        occurred_at=now,
        payload={"organization_id": "org-1", "quote_id": "quote-api-1"},
        signing_material=source_signing,
        sent_at=now,
    )
    source_headers = {
        "X-Dev-Actor": "source-api-worker",
        "X-Dev-Organization": "org-1",
        "X-Dev-Roles": "SYSTEM",
        "Idempotency-Key": "integration-receive-api-1",
    }
    handler_headers = {
        "X-Dev-Actor": "handler-api-worker",
        "X-Dev-Organization": "org-1",
        "X-Dev-Roles": "SYSTEM",
    }
    admin_headers = {
        "X-Dev-Actor": "integration-admin-api",
        "X-Dev-Organization": "org-1",
        "X-Dev-Roles": "ADMIN",
    }
    app = create_app(
        settings,
        engine=engine,
        object_store=LocalObjectStore(tmp_path / "objects"),
    )
    with TestClient(app) as client:
        body = {
            "source_qualification_id": "qualification-source-api",
            "envelope": envelope.model_dump(mode="json"),
            "reason": "Receive signed quote through the durable inbox",
        }
        accepted = client.post(
            "/v1/integrations/inbox",
            headers=source_headers,
            json=body,
        )
        assert accepted.status_code == 202, accepted.text
        assert accepted.headers["idempotency-replayed"] == "false"
        replayed = client.post(
            "/v1/integrations/inbox",
            headers=source_headers,
            json=body,
        )
        assert replayed.status_code == 202
        assert replayed.headers["idempotency-replayed"] == "true"
        assert replayed.json() == accepted.json()

        claim_response = client.post(
            "/v1/integrations/inbox/claims",
            headers={
                **handler_headers,
                "Idempotency-Key": "integration-claim-api-1",
            },
            json={
                "handler_qualification_id": "qualification-handler-api",
                "topics": ["price.quote.received"],
                "worker_id": "handler-api-instance-1",
            },
        )
        assert claim_response.status_code == 200, claim_response.text
        claim = claim_response.json()["claim"]
        assert claim is not None
        processing_id = claim["processing_id"]
        settled = client.post(
            f"/v1/integrations/inbox/processings/{processing_id}/acknowledge",
            headers={
                **handler_headers,
                "Idempotency-Key": "integration-ack-api-1",
            },
            json={
                "claim": claim,
                "result_reference": "unverified-import:quote-api-1",
                "result_hash": content_hash({"quote_id": "quote-api-1", "status": "UNVERIFIED"}),
                "reason": "Acknowledge only after durable unverified import",
            },
        )
        assert settled.status_code == 200, settled.text
        assert settled.json()["status"] == "CONSUMED"

        message = client.get(
            f"/v1/integrations/inbox/{accepted.json()['message_id']}",
            headers=admin_headers,
        )
        assert message.status_code == 200, message.text
        assert message.json()["processings"][0]["status"] == "CONSUMED"

    engine.dispose()
