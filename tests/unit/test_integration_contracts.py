import base64
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tenderguard.domain.common import canonical_json, content_hash
from tenderguard.domain.integration import (
    IntegrationEventBody,
    IntegrationReceiptStatus,
    SignedIntegrationEnvelope,
    build_signed_integration_envelope,
    build_signed_integration_receipt,
    load_integration_signing_material,
    verify_signed_integration_envelope,
    verify_signed_integration_receipt,
)
from tenderguard.integrations.contracts import (
    AdapterQualification,
    ConnectorDeliveryError,
)
from tenderguard.integrations.http_json import HttpsJsonIntegrationConnector


def _private_key_b64(seed: int) -> str:
    key = Ed25519PrivateKey.from_private_bytes(bytes([seed]) * 32)
    return base64.b64encode(
        key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
    ).decode("ascii")


def _qualification() -> AdapterQualification:
    return AdapterQualification(
        adapter_name="signed-http-test",
        adapter_version="1",
        qualification_id="qualification-signed-http-test",
        approved_by="methodology-owner",
        approved_at=datetime(2026, 7, 24, tzinfo=UTC),
        valid_until=None,
        test_evidence_hash="a" * 64,
    )


def test_signed_integration_event_and_receipt_are_bound_to_exact_content() -> None:
    sender = load_integration_signing_material(
        key_id="sender-key-1",
        private_key_b64=_private_key_b64(1),
    )
    receiver = load_integration_signing_material(
        key_id="receiver-key-1",
        private_key_b64=_private_key_b64(2),
    )
    now = datetime(2026, 7, 24, 12, tzinfo=UTC)
    envelope = build_signed_integration_envelope(
        message_id="message-1",
        delivery_deduplication_key="delivery-1",
        topic="audit.event.recorded",
        aggregate_id="audit-1",
        organization_id="org-1",
        occurred_at=now - timedelta(minutes=1),
        payload={"organization_id": "org-1", "amount": "100.00"},
        signing_material=sender,
        sent_at=now,
    )
    verify_signed_integration_envelope(
        envelope,
        trusted_key_id=sender.key_id,
        trusted_public_key_b64=sender.public_key_b64,
        expected_organization_id="org-1",
        allowed_topics=frozenset({"audit.event.recorded"}),
        now=now,
        max_message_age_seconds=60,
        max_future_skew_seconds=5,
    )

    tampered_body = IntegrationEventBody(
        **{
            **envelope.body.model_dump(),
            "payload": {"organization_id": "org-1", "amount": "101.00"},
            "payload_hash": content_hash({"organization_id": "org-1", "amount": "101.00"}),
        }
    )
    tampered = SignedIntegrationEnvelope(
        body=tampered_body,
        signature=envelope.signature,
    )
    with pytest.raises(ValueError, match="verification failed"):
        verify_signed_integration_envelope(
            tampered,
            trusted_key_id=sender.key_id,
            trusted_public_key_b64=sender.public_key_b64,
            expected_organization_id="org-1",
            allowed_topics=frozenset({"audit.event.recorded"}),
        )
    with pytest.raises(ValueError, match="older"):
        verify_signed_integration_envelope(
            envelope,
            trusted_key_id=sender.key_id,
            trusted_public_key_b64=sender.public_key_b64,
            expected_organization_id="org-1",
            allowed_topics=frozenset({"audit.event.recorded"}),
            now=now + timedelta(minutes=2),
            max_message_age_seconds=60,
        )

    receipt = build_signed_integration_receipt(
        source_message_id=envelope.body.message_id,
        receiver_message_id="remote-inbox-1",
        delivery_deduplication_key=envelope.body.delivery_deduplication_key,
        payload_hash=envelope.body.payload_hash,
        receiver_id="remote-ledger",
        status=IntegrationReceiptStatus.ACCEPTED,
        signing_material=receiver,
        received_at=now,
    )
    verify_signed_integration_receipt(
        receipt,
        envelope=envelope,
        trusted_key_id=receiver.key_id,
        trusted_public_key_b64=receiver.public_key_b64,
        expected_receiver_id="remote-ledger",
    )
    with pytest.raises(ValueError, match="does not acknowledge"):
        verify_signed_integration_receipt(
            receipt.model_copy(
                update={
                    "body": receipt.body.model_copy(
                        update={"delivery_deduplication_key": "different"}
                    )
                }
            ),
            envelope=envelope,
            trusted_key_id=receiver.key_id,
            trusted_public_key_b64=receiver.public_key_b64,
            expected_receiver_id="remote-ledger",
        )


def test_https_connector_is_bounded_and_classifies_retryable_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receiver = load_integration_signing_material(
        key_id="receiver-key-1",
        private_key_b64=_private_key_b64(3),
    )
    sender = load_integration_signing_material(
        key_id="sender-key-1",
        private_key_b64=_private_key_b64(4),
    )
    now = datetime(2026, 7, 24, 12, tzinfo=UTC)
    envelope = build_signed_integration_envelope(
        message_id="message-http",
        delivery_deduplication_key="delivery-http",
        topic="audit.event.recorded",
        aggregate_id="audit-http",
        organization_id="org-1",
        occurred_at=now,
        payload={"organization_id": "org-1"},
        signing_material=sender,
        sent_at=now,
    )
    receipt = build_signed_integration_receipt(
        source_message_id=envelope.body.message_id,
        receiver_message_id="remote-http-1",
        delivery_deduplication_key=envelope.body.delivery_deduplication_key,
        payload_hash=envelope.body.payload_hash,
        receiver_id="remote-ledger",
        status=IntegrationReceiptStatus.ACCEPTED,
        signing_material=receiver,
        received_at=now,
    )

    class Headers:
        @staticmethod
        def get_content_type() -> str:
            return "application/json"

    class Response:
        status = 202
        headers = Headers()

        @staticmethod
        def read(_: int) -> bytes:
            return canonical_json(receipt)

    class Connection:
        def request(self, *args: object, **kwargs: object) -> None:
            assert kwargs["headers"]["Idempotency-Key"] == "delivery-http"

        @staticmethod
        def getresponse() -> Response:
            return Response()

        @staticmethod
        def close() -> None:
            return None

    monkeypatch.setattr(
        "tenderguard.integrations.http_json.http.client.HTTPSConnection",
        lambda *args, **kwargs: Connection(),
    )
    connector = HttpsJsonIntegrationConnector(
        qualification=_qualification(),
        endpoint="https://connector.example/events",
        allowed_hosts=frozenset({"connector.example"}),
        timeout_seconds=30,
        max_response_bytes=64_000,
    )
    assert connector.deliver(envelope) == receipt
    with pytest.raises(ValueError, match="HTTPS"):
        HttpsJsonIntegrationConnector(
            qualification=_qualification(),
            endpoint="http://connector.example/events",
            allowed_hosts=frozenset({"connector.example"}),
            timeout_seconds=30,
            max_response_bytes=64_000,
        )

    Response.status = 503
    with pytest.raises(ConnectorDeliveryError) as error:
        connector.deliver(envelope)
    assert error.value.retryable
