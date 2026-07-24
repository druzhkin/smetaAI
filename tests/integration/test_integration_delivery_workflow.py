import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tenderguard.application.integrations import (
    IntegrationInboxService,
    IntegrationOutboxDispatcher,
)
from tenderguard.config import Settings
from tenderguard.domain.common import content_hash, ensure_utc, utc_now
from tenderguard.domain.enums import ActorRole
from tenderguard.domain.integration import (
    IntegrationReceiptStatus,
    IntegrationSigningMaterial,
    SignedIntegrationEnvelope,
    build_signed_integration_envelope,
    build_signed_integration_receipt,
    load_integration_signing_material,
    verify_signed_integration_receipt,
)
from tenderguard.domain.jobs import DispatchDisposition
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.database import (
    create_database_engine,
    create_schema_for_tests,
    create_session_factory,
)
from tenderguard.infrastructure.object_store import LocalObjectStore
from tenderguard.infrastructure.orm import (
    AdapterQualificationRow,
    ConnectorDeliveryAttemptRow,
    IntegrationInboxMessageRow,
    IntegrationInboxProcessingRow,
    ObservationRow,
    OutboxEventRow,
    OutboxReplayRow,
)
from tenderguard.integrations.contracts import (
    AdapterQualification,
    ConnectorDeliveryError,
    ConnectorHealth,
)


def _private_key_b64(seed: int) -> str:
    key = Ed25519PrivateKey.from_private_bytes(bytes([seed]) * 32)
    return base64.b64encode(
        key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
    ).decode("ascii")


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        app_env="test",
        database_url="sqlite+pysqlite://",
        local_object_store_path=tmp_path / "objects",
        audit_signing_key="integration-audit-key-at-least-32-bytes",
        integration_signing_key_id="tenderguard-integration-key-1",
        integration_signing_private_key_b64=_private_key_b64(10),
        integration_receiver_id="tenderguard-test",
        integration_job_lease_seconds=60,
        integration_job_timeout_seconds=30,
        integration_job_max_attempts=3,
        integration_job_retry_base_seconds=1,
        integration_job_retry_max_seconds=4,
        integration_http_timeout_seconds=10,
        integration_inbound_max_message_age_seconds=3600,
        integration_inbound_max_future_skew_seconds=30,
    )


def _qualification(
    *,
    qualification_id: str,
    adapter_name: str,
    evidence_character: str,
    now: datetime,
) -> AdapterQualification:
    return AdapterQualification(
        adapter_name=adapter_name,
        adapter_version="1",
        qualification_id=qualification_id,
        approved_by="methodology-owner",
        approved_at=now,
        valid_until=None,
        test_evidence_hash=evidence_character * 64,
    )


class _FakeConnector:
    def __init__(
        self,
        *,
        qualification: AdapterQualification,
        receipt_signing: IntegrationSigningMaterial,
        fail_once: bool = False,
        corrupt_receipt: bool = False,
        invalid_error_code: bool = False,
    ) -> None:
        self.qualification = qualification
        self.receipt_signing = receipt_signing
        self.fail_once = fail_once
        self.corrupt_receipt = corrupt_receipt
        self.invalid_error_code = invalid_error_code
        self.envelopes: list[SignedIntegrationEnvelope] = []

    def deliver(self, envelope: SignedIntegrationEnvelope):
        self.envelopes.append(envelope)
        if self.invalid_error_code:
            raise ConnectorDeliveryError(
                error_code="bad error code with transport details",
                retryable=True,
            )
        if self.fail_once and len(self.envelopes) == 1:
            raise ConnectorDeliveryError(
                error_code="REMOTE_TEMPORARILY_UNAVAILABLE",
                retryable=True,
            )
        receipt = build_signed_integration_receipt(
            source_message_id=envelope.body.message_id,
            receiver_message_id="remote-inbox-1",
            delivery_deduplication_key=envelope.body.delivery_deduplication_key,
            payload_hash=envelope.body.payload_hash,
            receiver_id="remote-ledger",
            status=(
                IntegrationReceiptStatus.DUPLICATE
                if len(self.envelopes) > 1
                else IntegrationReceiptStatus.ACCEPTED
            ),
            signing_material=self.receipt_signing,
        )
        if self.corrupt_receipt:
            return receipt.model_copy(
                update={
                    "body": receipt.body.model_copy(
                        update={"delivery_deduplication_key": "wrong-delivery"}
                    )
                }
            )
        return receipt

    def health(self) -> ConnectorHealth:
        return ConnectorHealth(
            connector_name=self.qualification.adapter_name,
            healthy=True,
            checked_at=utc_now(),
            source_as_of=utc_now(),
            message="OK",
        )


def _seed_qualifications(
    *,
    factory,
    now: datetime,
    remote_receipt: IntegrationSigningMaterial,
    source_signing: IntegrationSigningMaterial,
) -> tuple[AdapterQualification, AdapterQualificationRow, AdapterQualificationRow]:
    outbound = AdapterQualificationRow(
        id="qualification-outbound",
        adapter_name="remote-ledger-connector",
        adapter_version="1",
        status="APPROVED",
        valid_until=None,
        test_evidence_hash="a" * 64,
        payload={
            "organization_id": "org-1",
            "service_actor_id": "outbound-worker",
            "supported_methods": ["INTEGRATION_OUTBOUND_DELIVERY"],
            "outbound_topics": ["integration.test"],
            "receipt_signing_key_id": remote_receipt.key_id,
            "receipt_public_key_b64": remote_receipt.public_key_b64,
            "receiver_id": "remote-ledger",
        },
        approved_by="methodology-owner",
        approved_at=now,
    )
    source = AdapterQualificationRow(
        id="qualification-source",
        adapter_name="remote-source",
        adapter_version="1",
        status="APPROVED",
        valid_until=None,
        test_evidence_hash="b" * 64,
        payload={
            "organization_id": "org-1",
            "service_actor_id": "source-worker",
            "supported_methods": ["INTEGRATION_INBOUND_SOURCE"],
            "inbound_topics": ["price.quote.received"],
            "inbound_signing_key_id": source_signing.key_id,
            "inbound_signing_public_key_b64": source_signing.public_key_b64,
        },
        approved_by="methodology-owner",
        approved_at=now,
    )
    handler = AdapterQualificationRow(
        id="qualification-handler",
        adapter_name="price-inbox-handler",
        adapter_version="1",
        status="APPROVED",
        valid_until=None,
        test_evidence_hash="c" * 64,
        payload={
            "organization_id": "org-1",
            "service_actor_id": "handler-worker",
            "supported_methods": ["INTEGRATION_INBOX_HANDLER"],
            "inbound_topics": ["price.quote.received"],
        },
        approved_by="methodology-owner",
        approved_at=now,
    )
    with factory.begin() as session:
        session.add_all((outbound, source, handler))
    return (
        _qualification(
            qualification_id=outbound.id,
            adapter_name=outbound.adapter_name,
            evidence_character="a",
            now=now,
        ),
        source,
        handler,
    )


def test_signed_outbox_delivery_retries_records_receipts_and_replays_dead_letters(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    factory = create_session_factory(engine)
    now = datetime.now(UTC)
    remote_receipt = load_integration_signing_material(
        key_id="remote-receipt-key-1",
        private_key_b64=_private_key_b64(11),
    )
    source_signing = load_integration_signing_material(
        key_id="remote-source-key-1",
        private_key_b64=_private_key_b64(12),
    )
    outbound_qualification, _, _ = _seed_qualifications(
        factory=factory,
        now=now,
        remote_receipt=remote_receipt,
        source_signing=source_signing,
    )
    with factory.begin() as session:
        session.add_all(
            (
                OutboxEventRow(
                    id="outbox-deliver",
                    deduplication_key="outbox-internal-deliver",
                    delivery_deduplication_key="external-delivery-1",
                    topic="integration.test",
                    aggregate_id="aggregate-1",
                    payload={"organization_id": "org-1", "value": "100.00"},
                    attempts=0,
                    available_at=now,
                    published_at=None,
                    last_error=None,
                    locked_by=None,
                    lease_token=None,
                    lease_expires_at=None,
                    last_attempt_at=None,
                    dead_lettered_at=None,
                    created_at=now,
                ),
                OutboxEventRow(
                    id="outbox-invalid-receipt",
                    deduplication_key="outbox-internal-invalid",
                    delivery_deduplication_key="external-delivery-invalid",
                    topic="integration.test",
                    aggregate_id="aggregate-invalid",
                    payload={"organization_id": "org-1", "value": "200.00"},
                    attempts=0,
                    available_at=now,
                    published_at=None,
                    last_error=None,
                    locked_by=None,
                    lease_token=None,
                    lease_expires_at=None,
                    last_attempt_at=None,
                    dead_lettered_at=None,
                    created_at=now + timedelta(seconds=1),
                ),
            )
        )

    connector = _FakeConnector(
        qualification=outbound_qualification,
        receipt_signing=remote_receipt,
        fail_once=True,
    )
    mismatched_dispatcher = IntegrationOutboxDispatcher(
        session_factory=factory,
        settings=settings,
        connector=connector,
        connector_qualification_id=outbound_qualification.qualification_id,
        organization_id="org-1",
        service_actor_id="unqualified-worker",
    )
    with pytest.raises(ValueError, match="service actor"):
        mismatched_dispatcher.dispatch_next(worker_id="dispatcher-unqualified")
    dispatcher = IntegrationOutboxDispatcher(
        session_factory=factory,
        settings=settings,
        connector=connector,
        connector_qualification_id=outbound_qualification.qualification_id,
        organization_id="org-1",
        service_actor_id="outbound-worker",
    )
    first = dispatcher.dispatch_next(worker_id="dispatcher-1")
    assert first.disposition is DispatchDisposition.RETRY_SCHEDULED
    with factory() as session:
        scheduled = session.get(OutboxEventRow, "outbox-deliver")
        assert scheduled is not None
        available_at = ensure_utc(scheduled.available_at)
        last_attempt_at = ensure_utc(scheduled.last_attempt_at)
        assert available_at is not None and last_attempt_at is not None
        assert timedelta(seconds=1) <= available_at - last_attempt_at < timedelta(seconds=5)
    with factory.begin() as session:
        row = session.get(OutboxEventRow, "outbox-deliver")
        assert row is not None
        row.available_at = utc_now() - timedelta(seconds=1)
    second = dispatcher.dispatch_next(worker_id="dispatcher-2")
    assert second.disposition is DispatchDisposition.PROCESSED
    assert second.receipt is not None
    assert second.receipt.body.status is IntegrationReceiptStatus.DUPLICATE
    assert {item.body.delivery_deduplication_key for item in connector.envelopes} == {
        "external-delivery-1"
    }
    with factory() as session:
        row = session.get(OutboxEventRow, "outbox-deliver")
        assert row is not None and row.published_at is not None
        attempts = list(
            session.query(ConnectorDeliveryAttemptRow)
            .filter_by(outbox_event_id=row.id)
            .order_by(ConnectorDeliveryAttemptRow.attempt_number)
        )
        assert [item.status for item in attempts] == [
            "RETRYABLE_FAILURE",
            "DUPLICATE",
        ]
        assert attempts[1].receipt_hash == content_hash(second.receipt)

    invalid_connector = _FakeConnector(
        qualification=outbound_qualification,
        receipt_signing=remote_receipt,
        corrupt_receipt=True,
    )
    invalid_dispatcher = IntegrationOutboxDispatcher(
        session_factory=factory,
        settings=settings,
        connector=invalid_connector,
        connector_qualification_id=outbound_qualification.qualification_id,
        organization_id="org-1",
        service_actor_id="outbound-worker",
    )
    invalid = invalid_dispatcher.dispatch_next(worker_id="dispatcher-invalid")
    assert invalid.disposition is DispatchDisposition.DEAD_LETTERED
    assert invalid.error_code == "CONNECTOR_PROTOCOL_INVALID"

    admin = Actor("integration-admin", "org-1", frozenset({ActorRole.ADMIN}))
    with factory.begin() as session:
        replay_id = IntegrationInboxService(
            session=session,
            settings=settings,
            object_store=LocalObjectStore(tmp_path / "objects"),
        ).replay_outbox_dead_letter(
            actor=admin,
            outbox_event_id="outbox-invalid-receipt",
            request_id="request-replay-invalid-receipt",
            reason="Replay with the corrected qualified remote connector",
        )
    with factory() as session:
        source = session.get(OutboxEventRow, "outbox-invalid-receipt")
        replay = session.get(OutboxEventRow, replay_id)
        assert source is not None and replay is not None
        assert replay.deduplication_key != source.deduplication_key
        assert replay.delivery_deduplication_key == source.delivery_deduplication_key
        assert replay.payload == source.payload
        assert (
            session.query(OutboxReplayRow)
            .filter_by(
                source_outbox_event_id=source.id,
                replay_outbox_event_id=replay.id,
            )
            .one()
        )

    engine.dispose()


def test_invalid_connector_error_code_is_permanently_dead_lettered(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    factory = create_session_factory(engine)
    now = datetime.now(UTC)
    remote_receipt = load_integration_signing_material(
        key_id="remote-receipt-key-invalid-error",
        private_key_b64=_private_key_b64(21),
    )
    source_signing = load_integration_signing_material(
        key_id="remote-source-key-invalid-error",
        private_key_b64=_private_key_b64(22),
    )
    outbound_qualification, _, _ = _seed_qualifications(
        factory=factory,
        now=now,
        remote_receipt=remote_receipt,
        source_signing=source_signing,
    )
    with factory.begin() as session:
        session.add(
            OutboxEventRow(
                id="outbox-invalid-error-code",
                deduplication_key="outbox-internal-invalid-error-code",
                delivery_deduplication_key="external-invalid-error-code",
                topic="integration.test",
                aggregate_id="aggregate-invalid-error-code",
                payload={"organization_id": "org-1"},
                attempts=0,
                available_at=now,
                published_at=None,
                last_error=None,
                locked_by=None,
                lease_token=None,
                lease_expires_at=None,
                last_attempt_at=None,
                dead_lettered_at=None,
                created_at=now,
            )
        )

    result = IntegrationOutboxDispatcher(
        session_factory=factory,
        settings=settings,
        connector=_FakeConnector(
            qualification=outbound_qualification,
            receipt_signing=remote_receipt,
            invalid_error_code=True,
        ),
        connector_qualification_id=outbound_qualification.qualification_id,
        organization_id="org-1",
        service_actor_id="outbound-worker",
    ).dispatch_next(worker_id="dispatcher-invalid-error-code")

    assert result.disposition is DispatchDisposition.DEAD_LETTERED
    assert result.error_code == "CONNECTOR_ERROR_CODE_INVALID"
    with factory() as session:
        event = session.get(OutboxEventRow, "outbox-invalid-error-code")
        attempt = (
            session.query(ConnectorDeliveryAttemptRow)
            .filter_by(outbox_event_id="outbox-invalid-error-code")
            .one()
        )
        assert event is not None
        assert event.dead_lettered_at is not None
        assert event.last_error == "CONNECTOR_ERROR_CODE_INVALID"
        assert attempt.status == "PERMANENT_FAILURE"
        assert attempt.error_code == "CONNECTOR_ERROR_CODE_INVALID"

    engine.dispose()


def test_inbox_deduplicates_before_processing_and_requires_qualified_handler(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    factory = create_session_factory(engine)
    store = LocalObjectStore(tmp_path / "objects")
    now = datetime.now(UTC)
    remote_receipt = load_integration_signing_material(
        key_id="remote-receipt-key-1",
        private_key_b64=_private_key_b64(13),
    )
    source_signing = load_integration_signing_material(
        key_id="remote-source-key-1",
        private_key_b64=_private_key_b64(14),
    )
    _seed_qualifications(
        factory=factory,
        now=now,
        remote_receipt=remote_receipt,
        source_signing=source_signing,
    )
    source_actor = Actor(
        "source-worker",
        "org-1",
        frozenset({ActorRole.SYSTEM}),
    )
    handler_actor = Actor(
        "handler-worker",
        "org-1",
        frozenset({ActorRole.SYSTEM}),
    )
    envelope = build_signed_integration_envelope(
        message_id="source-message-1",
        delivery_deduplication_key="source-delivery-1",
        topic="price.quote.received",
        aggregate_id="rfq-1",
        organization_id="org-1",
        occurred_at=now,
        payload={
            "organization_id": "org-1",
            "item_id": "pump-1",
            "quoted_amount": "1000.00",
        },
        signing_material=source_signing,
        sent_at=now,
    )
    with (
        factory.begin() as session,
        pytest.raises(ValueError, match="size limit"),
    ):
        IntegrationInboxService(
            session=session,
            settings=settings.model_copy(update={"integration_max_event_bytes": 1}),
            object_store=store,
        ).receive(
            actor=source_actor,
            source_qualification_id="qualification-source",
            envelope=envelope,
            request_id="request-inbox-oversize",
            reason="Reject an event above the configured transport limit",
        )
    with factory.begin() as session:
        accepted = IntegrationInboxService(
            session=session,
            settings=settings,
            object_store=store,
        ).receive(
            actor=source_actor,
            source_qualification_id="qualification-source",
            envelope=envelope,
            request_id="request-inbox-accepted",
            reason="Receive signed quote transport message",
        )
    local_signing = load_integration_signing_material(
        key_id=settings.integration_signing_key_id or "",
        private_key_b64=(
            settings.integration_signing_private_key_b64.get_secret_value()
            if settings.integration_signing_private_key_b64
            else ""
        ),
    )
    verify_signed_integration_receipt(
        accepted.receipt,
        envelope=envelope,
        trusted_key_id=local_signing.key_id,
        trusted_public_key_b64=local_signing.public_key_b64,
        expected_receiver_id="tenderguard-test",
    )
    retry = build_signed_integration_envelope(
        message_id=envelope.body.message_id,
        delivery_deduplication_key=envelope.body.delivery_deduplication_key,
        topic=envelope.body.topic,
        aggregate_id=envelope.body.aggregate_id,
        organization_id=envelope.body.organization_id,
        occurred_at=envelope.body.occurred_at,
        payload=envelope.body.payload,
        signing_material=source_signing,
        sent_at=now + timedelta(seconds=1),
    )
    with factory.begin() as session:
        duplicate = IntegrationInboxService(
            session=session,
            settings=settings,
            object_store=store,
        ).receive(
            actor=source_actor,
            source_qualification_id="qualification-source",
            envelope=retry,
            request_id="request-inbox-duplicate",
            reason="Retry the exact signed quote delivery",
        )
    assert duplicate.duplicate
    assert duplicate.message_id == accepted.message_id
    assert duplicate.receipt == accepted.receipt

    conflicting = build_signed_integration_envelope(
        message_id=envelope.body.message_id,
        delivery_deduplication_key=envelope.body.delivery_deduplication_key,
        topic=envelope.body.topic,
        aggregate_id=envelope.body.aggregate_id,
        organization_id=envelope.body.organization_id,
        occurred_at=envelope.body.occurred_at,
        payload={
            "organization_id": "org-1",
            "item_id": "pump-1",
            "quoted_amount": "9999.00",
        },
        signing_material=source_signing,
        sent_at=now + timedelta(seconds=2),
    )
    with (
        factory.begin() as session,
        pytest.raises(ValueError, match="different content"),
    ):
        IntegrationInboxService(
            session=session,
            settings=settings,
            object_store=store,
        ).receive(
            actor=source_actor,
            source_qualification_id="qualification-source",
            envelope=conflicting,
            request_id="request-inbox-conflict",
            reason="Must reject a deduplication collision",
        )

    with factory.begin() as session:
        service = IntegrationInboxService(
            session=session,
            settings=settings,
            object_store=store,
        )
        claim = service.claim_next(
            actor=handler_actor,
            handler_qualification_id="qualification-handler",
            topics=frozenset({"price.quote.received"}),
            worker_id="handler-instance-1",
        )
        assert claim is not None
        retry_settlement = service.reject(
            actor=handler_actor,
            claim=claim,
            error_code="ERP_TEMPORARILY_UNAVAILABLE",
            request_id="request-inbox-retry",
            reason="Retry after a transient downstream failure",
        )
        assert not retry_settlement.dead_lettered
    with factory.begin() as session:
        processing = session.get(
            IntegrationInboxProcessingRow,
            accepted.processing_id,
        )
        assert processing is not None
        processing.available_at = utc_now() - timedelta(seconds=1)
    with factory.begin() as session:
        service = IntegrationInboxService(
            session=session,
            settings=settings,
            object_store=store,
        )
        claim = service.claim_next(
            actor=handler_actor,
            handler_qualification_id="qualification-handler",
            topics=frozenset({"price.quote.received"}),
            worker_id="handler-instance-2",
        )
        assert claim is not None and claim.delivery_attempt == 2
        consumed = service.acknowledge(
            actor=handler_actor,
            claim=claim,
            result_reference="unverified-price-import:quote-1",
            result_hash=content_hash({"quote_id": "quote-1", "status": "UNVERIFIED"}),
            request_id="request-inbox-consumed",
            reason="Persist unverified domain import before acknowledging transport",
        )
        assert consumed.status == "CONSUMED"

    second_envelope = build_signed_integration_envelope(
        message_id="source-message-2",
        delivery_deduplication_key="source-delivery-2",
        topic="price.quote.received",
        aggregate_id="rfq-2",
        organization_id="org-1",
        occurred_at=now,
        payload={"organization_id": "org-1", "item_id": "pump-2"},
        signing_material=source_signing,
        sent_at=now,
    )
    with factory.begin() as session:
        service = IntegrationInboxService(
            session=session,
            settings=settings,
            object_store=store,
        )
        second = service.receive(
            actor=source_actor,
            source_qualification_id="qualification-source",
            envelope=second_envelope,
            request_id="request-inbox-second",
            reason="Receive a second signed transport message",
        )
        claim = service.claim_next(
            actor=handler_actor,
            handler_qualification_id="qualification-handler",
            topics=frozenset({"price.quote.received"}),
            worker_id="handler-instance-dead",
        )
        assert claim is not None and claim.message_id == second.message_id
        dead = service.reject(
            actor=handler_actor,
            claim=claim,
            error_code="PAYLOAD_SCHEMA_UNSUPPORTED",
            force_dead_letter=True,
            request_id="request-inbox-dead",
            reason="Permanently reject an unsupported source schema",
        )
        assert dead.dead_lettered

    admin = Actor("integration-admin", "org-1", frozenset({ActorRole.ADMIN}))
    with factory.begin() as session:
        replay = IntegrationInboxService(
            session=session,
            settings=settings,
            object_store=store,
        ).replay_dead_letter(
            actor=admin,
            processing_id=second.processing_id,
            request_id="request-inbox-replay",
            reason="Replay after deploying a qualified schema handler",
        )
        assert replay.generation == 2
        assert replay.status == "PENDING"
    with factory() as session:
        assert session.query(IntegrationInboxMessageRow).count() == 2
        assert session.query(ObservationRow).count() == 0
        processings = list(
            session.query(IntegrationInboxProcessingRow)
            .filter_by(message_id=second.message_id)
            .order_by(IntegrationInboxProcessingRow.generation)
        )
        assert [item.status for item in processings] == [
            "DEAD_LETTERED",
            "PENDING",
        ]

    with factory.begin() as session:
        stale_claim = IntegrationInboxService(
            session=session,
            settings=settings,
            object_store=store,
        ).claim_next(
            actor=handler_actor,
            handler_qualification_id="qualification-handler",
            topics=frozenset({"price.quote.received"}),
            worker_id="handler-instance-stale",
        )
        assert stale_claim is not None and stale_claim.generation == 2
    with factory.begin() as session:
        stale_processing = session.get(
            IntegrationInboxProcessingRow,
            stale_claim.processing_id,
        )
        assert stale_processing is not None
        stale_processing.last_attempt_at = utc_now() - timedelta(minutes=2)
        stale_processing.lease_expires_at = utc_now() - timedelta(seconds=1)
    with (
        factory.begin() as session,
        pytest.raises(ValueError, match="invalid or expired"),
    ):
        IntegrationInboxService(
            session=session,
            settings=settings,
            object_store=store,
        ).acknowledge(
            actor=handler_actor,
            claim=stale_claim,
            result_reference="unverified-price-import:stale",
            result_hash=content_hash({"status": "MUST_NOT_PERSIST"}),
            request_id="request-inbox-stale-ack",
            reason="A stale processing lease must not settle",
        )

    engine.dispose()
