from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from tenderguard.application.outbox import (
    OutboxDeliveryPolicy,
    OutboxDeliveryService,
)
from tenderguard.application.projects import ProjectService
from tenderguard.config import Settings
from tenderguard.domain.common import (
    canonical_data,
    canonical_json,
    content_hash,
    ensure_utc,
    utc_now,
)
from tenderguard.domain.enums import ActorRole
from tenderguard.domain.integration import (
    IntegrationReceiptStatus,
    IntegrationSigningMaterial,
    SignedIntegrationEnvelope,
    SignedIntegrationReceipt,
    build_signed_integration_envelope,
    build_signed_integration_receipt,
    integration_envelope_core_hash,
    load_integration_signing_material,
    validate_integration_public_key,
    verify_signed_integration_envelope,
    verify_signed_integration_receipt,
)
from tenderguard.domain.jobs import DispatchDisposition, OutboxClaim
from tenderguard.domain.models import DomainModel
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.object_store import ObjectStore
from tenderguard.infrastructure.orm import (
    AdapterQualificationRow,
    ConnectorDeliveryAttemptRow,
    IntegrationInboxMessageRow,
    IntegrationInboxProcessingRow,
    OutboxEventRow,
    OutboxReplayRow,
)
from tenderguard.integrations.contracts import (
    ConnectorDeliveryError,
    IntegrationConnector,
)

OUTBOUND_DELIVERY_METHOD = "INTEGRATION_OUTBOUND_DELIVERY"
INBOUND_SOURCE_METHOD = "INTEGRATION_INBOUND_SOURCE"
INBOX_HANDLER_METHOD = "INTEGRATION_INBOX_HANDLER"
_ERROR_CODE = re.compile(r"^[A-Z0-9_.:-]{1,200}$")


@dataclass(frozen=True)
class _OutboundBinding:
    row: AdapterQualificationRow
    topics: frozenset[str]
    receipt_key_id: str
    receipt_public_key_b64: str
    receiver_id: str


@dataclass(frozen=True)
class _InboundSourceBinding:
    row: AdapterQualificationRow
    topics: frozenset[str]
    signing_key_id: str
    signing_public_key_b64: str


@dataclass(frozen=True)
class _InboxHandlerBinding:
    row: AdapterQualificationRow
    topics: frozenset[str]


class ConnectorDispatchResult(DomainModel):
    disposition: DispatchDisposition
    outbox_event_id: str | None = None
    attempt_id: str | None = None
    connector_qualification_id: str | None = None
    receipt: SignedIntegrationReceipt | None = None
    error_code: str | None = None


class IntegrationInboxReceiptResult(DomainModel):
    message_id: str
    processing_id: str
    duplicate: bool
    receipt: SignedIntegrationReceipt


class IntegrationInboxClaim(DomainModel):
    processing_id: str
    message_id: str
    generation: int
    topic: str
    aggregate_id: str
    envelope: SignedIntegrationEnvelope
    delivery_attempt: int
    handler_qualification_id: str
    worker_id: str
    lease_token: str
    lease_expires_at: datetime
    processing_deadline_at: datetime


class IntegrationInboxSettlement(DomainModel):
    processing_id: str
    status: str
    dead_lettered: bool
    next_available_at: datetime | None = None


class IntegrationInboxProcessingView(DomainModel):
    processing_id: str
    generation: int
    status: str
    attempts: int
    available_at: datetime
    handler_qualification_id: str | None
    result_reference: str | None
    result_hash: str | None
    last_error: str | None
    consumed_at: datetime | None
    dead_lettered_at: datetime | None
    created_at: datetime


class IntegrationInboxMessageView(DomainModel):
    message_id: str
    source_qualification_id: str
    organization_id: str
    source_message_id: str
    delivery_deduplication_key: str
    topic: str
    aggregate_id: str
    schema_version: str
    core_hash: str
    payload_hash: str
    envelope: SignedIntegrationEnvelope
    receipt: SignedIntegrationReceipt
    qualification_snapshot: dict[str, Any]
    received_at: datetime
    processings: tuple[IntegrationInboxProcessingView, ...]


class IntegrationOutboxDispatcher:
    """Deliver signed outbox events without holding a DB transaction over I/O."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        settings: Settings,
        connector: IntegrationConnector,
        connector_qualification_id: str,
        organization_id: str,
        service_actor_id: str,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.connector = connector
        self.connector_qualification_id = connector_qualification_id
        self.organization_id = organization_id
        self.service_actor_id = _required_string(
            service_actor_id,
            "service_actor_id",
            128,
        )
        self.signing_material = _integration_signing_material(settings)

    def dispatch_next(self, *, worker_id: str) -> ConnectorDispatchResult:
        binding = self._binding()
        claim = self._claim(binding=binding, worker_id=worker_id)
        if claim is None:
            return ConnectorDispatchResult(
                disposition=DispatchDisposition.IDLE,
                connector_qualification_id=binding.row.id,
            )
        started_at = utc_now()
        envelope: SignedIntegrationEnvelope | None = None
        try:
            payload_organization_id = claim.payload.get("organization_id")
            if payload_organization_id != self.organization_id:
                raise ValueError("Outbox event has no matching organization identity")
            envelope = build_signed_integration_envelope(
                message_id=claim.event_id,
                delivery_deduplication_key=claim.delivery_deduplication_key,
                topic=claim.topic,
                aggregate_id=claim.aggregate_id,
                organization_id=self.organization_id,
                occurred_at=claim.occurred_at,
                payload=claim.payload,
                signing_material=self.signing_material,
            )
            if len(canonical_json(envelope)) > self.settings.integration_max_event_bytes:
                raise ValueError("Signed integration event exceeds the configured size limit")
            receipt = self.connector.deliver(envelope)
            if utc_now() > started_at + timedelta(
                seconds=self.settings.integration_job_timeout_seconds
            ):
                raise ConnectorDeliveryError(
                    error_code="CONNECTOR_DELIVERY_TIMEOUT",
                    retryable=True,
                )
            if not isinstance(receipt, SignedIntegrationReceipt):
                raise ValueError("Connector returned no typed signed receipt")
            verify_signed_integration_receipt(
                receipt,
                envelope=envelope,
                trusted_key_id=binding.receipt_key_id,
                trusted_public_key_b64=binding.receipt_public_key_b64,
                expected_receiver_id=binding.receiver_id,
            )
        except ConnectorDeliveryError as error:
            try:
                connector_error_code = _error_code(error.error_code)
                permanent_error = not error.retryable
            except ValueError:
                connector_error_code = "CONNECTOR_ERROR_CODE_INVALID"
                permanent_error = True
            return self._settle_failure(
                claim=claim,
                binding=binding,
                started_at=started_at,
                envelope=envelope,
                error_code=connector_error_code,
                permanent=permanent_error,
            )
        except ValueError:
            return self._settle_failure(
                claim=claim,
                binding=binding,
                started_at=started_at,
                envelope=envelope,
                error_code="CONNECTOR_PROTOCOL_INVALID",
                permanent=True,
            )
        except Exception:
            return self._settle_failure(
                claim=claim,
                binding=binding,
                started_at=started_at,
                envelope=envelope,
                error_code="CONNECTOR_DELIVERY_FAILED",
                permanent=False,
            )
        return self._settle_success(
            claim=claim,
            binding=binding,
            envelope=envelope,
            receipt=receipt,
            started_at=started_at,
        )

    def _binding(self) -> _OutboundBinding:
        with self.session_factory() as session:
            row = _active_qualification(
                session,
                qualification_id=self.connector_qualification_id,
                organization_id=self.organization_id,
                required_method=OUTBOUND_DELIVERY_METHOD,
                service_actor_id=self.service_actor_id,
            )
            topics = _topics(row, "outbound_topics")
            receipt_key_id = _payload_string(row, "receipt_signing_key_id", 200)
            receipt_public_key = _payload_string(row, "receipt_public_key_b64", 200)
            validate_integration_public_key(receipt_public_key)
            receiver_id = _payload_string(row, "receiver_id", 200)
            qualification = self.connector.qualification
            if (
                qualification.qualification_id != row.id
                or qualification.adapter_name != row.adapter_name
                or qualification.adapter_version != row.adapter_version
                or qualification.test_evidence_hash != row.test_evidence_hash
            ):
                raise ValueError("Runtime connector differs from its approved qualification")
            return _OutboundBinding(
                row=row,
                topics=topics,
                receipt_key_id=receipt_key_id,
                receipt_public_key_b64=receipt_public_key,
                receiver_id=receiver_id,
            )

    def _claim(
        self,
        *,
        binding: _OutboundBinding,
        worker_id: str,
    ) -> OutboxClaim | None:
        with self.session_factory.begin() as session:
            return self._delivery_service(session).claim_next(
                topics=binding.topics,
                worker_id=worker_id,
            )

    def _settle_success(
        self,
        *,
        claim: OutboxClaim,
        binding: _OutboundBinding,
        envelope: SignedIntegrationEnvelope,
        receipt: SignedIntegrationReceipt,
        started_at: datetime,
    ) -> ConnectorDispatchResult:
        attempt_id = f"connector-attempt-{uuid4()}"
        with self.session_factory.begin() as session:
            session.add(
                ConnectorDeliveryAttemptRow(
                    id=attempt_id,
                    outbox_event_id=claim.event_id,
                    connector_qualification_id=binding.row.id,
                    attempt_number=claim.delivery_attempt,
                    status=receipt.body.status.value,
                    envelope_hash=content_hash(envelope),
                    receipt_hash=content_hash(receipt),
                    external_message_id=receipt.body.receiver_message_id,
                    error_code=None,
                    payload={
                        "envelope": canonical_data(envelope),
                        "receipt": canonical_data(receipt),
                        "connector_qualification": _qualification_snapshot(binding.row),
                    },
                    started_at=started_at,
                    completed_at=utc_now(),
                )
            )
            self._delivery_service(session).acknowledge(claim)
        return ConnectorDispatchResult(
            disposition=DispatchDisposition.PROCESSED,
            outbox_event_id=claim.event_id,
            attempt_id=attempt_id,
            connector_qualification_id=binding.row.id,
            receipt=receipt,
        )

    def _settle_failure(
        self,
        *,
        claim: OutboxClaim,
        binding: _OutboundBinding,
        started_at: datetime,
        envelope: object | None,
        error_code: str,
        permanent: bool,
    ) -> ConnectorDispatchResult:
        attempt_id = f"connector-attempt-{uuid4()}"
        with self.session_factory.begin() as session:
            session.add(
                ConnectorDeliveryAttemptRow(
                    id=attempt_id,
                    outbox_event_id=claim.event_id,
                    connector_qualification_id=binding.row.id,
                    attempt_number=claim.delivery_attempt,
                    status=("PERMANENT_FAILURE" if permanent else "RETRYABLE_FAILURE"),
                    envelope_hash=(
                        content_hash(envelope)
                        if isinstance(envelope, SignedIntegrationEnvelope)
                        else content_hash(
                            {
                                "event_id": claim.event_id,
                                "delivery_attempt": claim.delivery_attempt,
                            }
                        )
                    ),
                    receipt_hash=None,
                    external_message_id=None,
                    error_code=error_code,
                    payload={
                        "envelope": (
                            canonical_data(envelope)
                            if isinstance(envelope, SignedIntegrationEnvelope)
                            else None
                        ),
                        "connector_qualification": _qualification_snapshot(binding.row),
                    },
                    started_at=started_at,
                    completed_at=utc_now(),
                )
            )
            settlement = self._delivery_service(session).reject(
                claim,
                error_code=error_code,
                force_dead_letter=permanent,
            )
        return ConnectorDispatchResult(
            disposition=(
                DispatchDisposition.DEAD_LETTERED
                if settlement.dead_lettered
                else DispatchDisposition.RETRY_SCHEDULED
            ),
            outbox_event_id=claim.event_id,
            attempt_id=attempt_id,
            connector_qualification_id=binding.row.id,
            error_code=error_code,
        )

    def _delivery_service(self, session: Session) -> OutboxDeliveryService:
        return OutboxDeliveryService(
            session=session,
            settings=self.settings,
            policy=OutboxDeliveryPolicy.integration(self.settings),
        )


class IntegrationInboxService:
    def __init__(
        self,
        *,
        session: Session,
        settings: Settings,
        object_store: ObjectStore,
    ) -> None:
        self.session = session
        self.settings = settings
        self.object_store = object_store
        self.signing_material = _integration_signing_material(settings)

    def receive(
        self,
        *,
        actor: Actor,
        source_qualification_id: str,
        envelope: SignedIntegrationEnvelope,
        request_id: str,
        reason: str,
    ) -> IntegrationInboxReceiptResult:
        _require_system_actor(actor)
        if len(canonical_json(envelope)) > self.settings.integration_max_event_bytes:
            raise ValueError("Signed integration event exceeds the configured size limit")
        binding = self._source_binding(
            actor=actor,
            qualification_id=source_qualification_id,
        )
        verify_signed_integration_envelope(
            envelope,
            trusted_key_id=binding.signing_key_id,
            trusted_public_key_b64=binding.signing_public_key_b64,
            expected_organization_id=actor.organization_id,
            allowed_topics=binding.topics,
        )
        core_hash = integration_envelope_core_hash(envelope)
        existing = self._existing_message(
            source_qualification_id=source_qualification_id,
            source_message_id=envelope.body.message_id,
            delivery_deduplication_key=envelope.body.delivery_deduplication_key,
        )
        if existing is not None:
            self._require_same_message(existing, core_hash)
            processing = self._first_processing(existing.id)
            return IntegrationInboxReceiptResult(
                message_id=existing.id,
                processing_id=processing.id,
                duplicate=True,
                receipt=SignedIntegrationReceipt.model_validate(existing.receipt),
            )
        verify_signed_integration_envelope(
            envelope,
            trusted_key_id=binding.signing_key_id,
            trusted_public_key_b64=binding.signing_public_key_b64,
            expected_organization_id=actor.organization_id,
            allowed_topics=binding.topics,
            max_message_age_seconds=(self.settings.integration_inbound_max_message_age_seconds),
            max_future_skew_seconds=(self.settings.integration_inbound_max_future_skew_seconds),
        )
        now = utc_now()
        message_id = (
            "integration-inbox-"
            + content_hash(
                {
                    "source_qualification_id": source_qualification_id,
                    "delivery_deduplication_key": (envelope.body.delivery_deduplication_key),
                }
            )[:24]
        )
        processing_id = _processing_id(message_id, 1)
        receipt = build_signed_integration_receipt(
            source_message_id=envelope.body.message_id,
            receiver_message_id=message_id,
            delivery_deduplication_key=envelope.body.delivery_deduplication_key,
            payload_hash=envelope.body.payload_hash,
            receiver_id=self._receiver_id(),
            status=IntegrationReceiptStatus.ACCEPTED,
            signing_material=self.signing_material,
            received_at=now,
        )
        values = {
            "id": message_id,
            "source_qualification_id": source_qualification_id,
            "organization_id": actor.organization_id,
            "source_message_id": envelope.body.message_id,
            "delivery_deduplication_key": (envelope.body.delivery_deduplication_key),
            "topic": envelope.body.topic,
            "aggregate_id": envelope.body.aggregate_id,
            "schema_version": envelope.body.schema_version,
            "core_hash": core_hash,
            "payload_hash": envelope.body.payload_hash,
            "envelope": canonical_data(envelope),
            "receipt": canonical_data(receipt),
            "qualification_snapshot": _qualification_snapshot(binding.row),
            "received_at": now,
        }
        dialect = self.session.get_bind().dialect.name
        if dialect == "postgresql":
            statement = (
                postgresql_insert(IntegrationInboxMessageRow)
                .values(**values)
                .on_conflict_do_nothing()
                .returning(IntegrationInboxMessageRow.id)
            )
        elif dialect == "sqlite":
            statement = (
                sqlite_insert(IntegrationInboxMessageRow)
                .values(**values)
                .on_conflict_do_nothing()
                .returning(IntegrationInboxMessageRow.id)
            )
        else:
            raise RuntimeError("Integration inbox supports only PostgreSQL and SQLite")
        inserted_id = self.session.scalar(statement)
        if inserted_id is None:
            existing = self._existing_message(
                source_qualification_id=source_qualification_id,
                source_message_id=envelope.body.message_id,
                delivery_deduplication_key=envelope.body.delivery_deduplication_key,
            )
            if existing is None:
                raise RuntimeError("Concurrent inbox insert did not expose its durable row")
            self._require_same_message(existing, core_hash)
            processing = self._first_processing(existing.id)
            return IntegrationInboxReceiptResult(
                message_id=existing.id,
                processing_id=processing.id,
                duplicate=True,
                receipt=SignedIntegrationReceipt.model_validate(existing.receipt),
            )
        self.session.add(
            IntegrationInboxProcessingRow(
                id=processing_id,
                message_id=message_id,
                generation=1,
                status="PENDING",
                attempts=0,
                available_at=now,
                locked_by=None,
                lease_token=None,
                lease_expires_at=None,
                last_attempt_at=None,
                last_error=None,
                handler_qualification_id=None,
                result_reference=None,
                result_hash=None,
                consumed_at=None,
                dead_lettered_at=None,
                created_at=now,
            )
        )
        self.session.flush()
        self._audit(
            actor=actor,
            aggregate_id=message_id,
            event_type="integration_inbox_message_received",
            request_id=request_id,
            reason=reason,
            payload={
                "source_qualification_id": source_qualification_id,
                "source_message_id": envelope.body.message_id,
                "delivery_deduplication_key": (envelope.body.delivery_deduplication_key),
                "topic": envelope.body.topic,
                "aggregate_id": envelope.body.aggregate_id,
                "payload_hash": envelope.body.payload_hash,
                "core_hash": core_hash,
                "processing_id": processing_id,
            },
        )
        return IntegrationInboxReceiptResult(
            message_id=message_id,
            processing_id=processing_id,
            duplicate=False,
            receipt=receipt,
        )

    def claim_next(
        self,
        *,
        actor: Actor,
        handler_qualification_id: str,
        topics: frozenset[str],
        worker_id: str,
    ) -> IntegrationInboxClaim | None:
        _require_system_actor(actor)
        binding = self._handler_binding(
            actor=actor,
            qualification_id=handler_qualification_id,
        )
        selected_topics = frozenset(_required_topic(item) for item in topics)
        if not selected_topics:
            raise ValueError("At least one inbox topic must be requested")
        if not selected_topics.issubset(binding.topics):
            raise ValueError("Inbox handler requested a topic outside its qualification")
        normalized_worker = _required_string(worker_id, "worker_id", 128)
        now = utc_now()
        row = self.session.scalar(
            select(IntegrationInboxProcessingRow)
            .join(
                IntegrationInboxMessageRow,
                IntegrationInboxMessageRow.id == IntegrationInboxProcessingRow.message_id,
            )
            .where(
                IntegrationInboxProcessingRow.status == "PENDING",
                IntegrationInboxProcessingRow.available_at <= now,
                or_(
                    IntegrationInboxProcessingRow.lease_expires_at.is_(None),
                    IntegrationInboxProcessingRow.lease_expires_at <= now,
                ),
                or_(
                    IntegrationInboxProcessingRow.handler_qualification_id.is_(None),
                    IntegrationInboxProcessingRow.handler_qualification_id
                    == handler_qualification_id,
                ),
                IntegrationInboxMessageRow.organization_id == actor.organization_id,
                IntegrationInboxMessageRow.topic.in_(tuple(selected_topics)),
            )
            .order_by(
                IntegrationInboxProcessingRow.available_at,
                IntegrationInboxProcessingRow.created_at,
                IntegrationInboxProcessingRow.id,
            )
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if row is None:
            return None
        message = self.session.get(IntegrationInboxMessageRow, row.message_id)
        if message is None:
            raise RuntimeError("Inbox processing has no source message")
        lease_token = f"inbox-lease-{uuid4()}"
        row.attempts += 1
        row.locked_by = normalized_worker
        row.lease_token = lease_token
        row.lease_expires_at = now + timedelta(seconds=self.settings.integration_job_lease_seconds)
        processing_deadline_at = now + timedelta(
            seconds=self.settings.integration_job_timeout_seconds
        )
        row.last_attempt_at = now
        row.handler_qualification_id = binding.row.id
        self.session.flush()
        return IntegrationInboxClaim(
            processing_id=row.id,
            message_id=message.id,
            generation=row.generation,
            topic=message.topic,
            aggregate_id=message.aggregate_id,
            envelope=SignedIntegrationEnvelope.model_validate(message.envelope),
            delivery_attempt=row.attempts,
            handler_qualification_id=binding.row.id,
            worker_id=normalized_worker,
            lease_token=lease_token,
            lease_expires_at=row.lease_expires_at,
            processing_deadline_at=processing_deadline_at,
        )

    def acknowledge(
        self,
        *,
        actor: Actor,
        claim: IntegrationInboxClaim,
        result_reference: str,
        result_hash: str,
        request_id: str,
        reason: str,
    ) -> IntegrationInboxSettlement:
        _require_system_actor(actor)
        self._handler_binding(
            actor=actor,
            qualification_id=claim.handler_qualification_id,
        )
        reference = _required_string(result_reference, "result_reference", 500)
        _sha256(result_hash, "result_hash")
        row = self._locked_processing(claim.processing_id)
        self._require_processing_owner(row, claim)
        now = utc_now()
        row.status = "CONSUMED"
        row.result_reference = reference
        row.result_hash = result_hash
        row.consumed_at = now
        self._clear_processing_lease(row)
        self.session.flush()
        self._audit(
            actor=actor,
            aggregate_id=row.message_id,
            event_type="integration_inbox_message_consumed",
            request_id=request_id,
            reason=reason,
            payload={
                "processing_id": row.id,
                "generation": row.generation,
                "handler_qualification_id": claim.handler_qualification_id,
                "result_reference": reference,
                "result_hash": result_hash,
            },
        )
        return IntegrationInboxSettlement(
            processing_id=row.id,
            status=row.status,
            dead_lettered=False,
        )

    def reject(
        self,
        *,
        actor: Actor,
        claim: IntegrationInboxClaim,
        error_code: str,
        request_id: str,
        reason: str,
        force_dead_letter: bool = False,
    ) -> IntegrationInboxSettlement:
        _require_system_actor(actor)
        self._handler_binding(
            actor=actor,
            qualification_id=claim.handler_qualification_id,
        )
        normalized_error = _error_code(error_code)
        row = self._locked_processing(claim.processing_id)
        self._require_processing_owner(row, claim)
        now = utc_now()
        row.last_error = normalized_error
        dead_lettered = (
            force_dead_letter or row.attempts >= self.settings.integration_job_max_attempts
        )
        next_available_at = None
        if dead_lettered:
            row.status = "DEAD_LETTERED"
            row.dead_lettered_at = now
        else:
            exponent = min(max(row.attempts - 1, 0), 30)
            delay = min(
                self.settings.integration_job_retry_base_seconds * (2**exponent),
                self.settings.integration_job_retry_max_seconds,
            )
            next_available_at = now + timedelta(seconds=delay)
            row.available_at = next_available_at
        self._clear_processing_lease(row)
        self.session.flush()
        if dead_lettered:
            self._audit(
                actor=actor,
                aggregate_id=row.message_id,
                event_type="integration_inbox_message_dead_lettered",
                request_id=request_id,
                reason=reason,
                payload={
                    "processing_id": row.id,
                    "generation": row.generation,
                    "handler_qualification_id": claim.handler_qualification_id,
                    "error_code": normalized_error,
                    "attempts": row.attempts,
                },
            )
        return IntegrationInboxSettlement(
            processing_id=row.id,
            status=row.status,
            dead_lettered=dead_lettered,
            next_available_at=next_available_at,
        )

    def replay_dead_letter(
        self,
        *,
        actor: Actor,
        processing_id: str,
        request_id: str,
        reason: str,
    ) -> IntegrationInboxProcessingView:
        actor.require_any(ActorRole.ADMIN)
        source = self.session.scalar(
            select(IntegrationInboxProcessingRow)
            .where(IntegrationInboxProcessingRow.id == processing_id)
            .with_for_update()
        )
        if source is None:
            raise LookupError(processing_id)
        message = self.session.get(IntegrationInboxMessageRow, source.message_id)
        if message is None or message.organization_id != actor.organization_id:
            raise LookupError(processing_id)
        if source.status != "DEAD_LETTERED":
            raise ValueError("Only a dead-lettered inbox processing can be replayed")
        latest_generation = self.session.scalar(
            select(IntegrationInboxProcessingRow.generation)
            .where(IntegrationInboxProcessingRow.message_id == source.message_id)
            .order_by(IntegrationInboxProcessingRow.generation.desc())
            .limit(1)
        )
        if latest_generation != source.generation:
            raise ValueError("A newer inbox processing generation already exists")
        generation = source.generation + 1
        now = utc_now()
        row = IntegrationInboxProcessingRow(
            id=_processing_id(source.message_id, generation),
            message_id=source.message_id,
            generation=generation,
            status="PENDING",
            attempts=0,
            available_at=now,
            locked_by=None,
            lease_token=None,
            lease_expires_at=None,
            last_attempt_at=None,
            last_error=None,
            handler_qualification_id=None,
            result_reference=None,
            result_hash=None,
            consumed_at=None,
            dead_lettered_at=None,
            created_at=now,
        )
        self.session.add(row)
        self.session.flush()
        self._audit(
            actor=actor,
            aggregate_id=source.message_id,
            event_type="integration_inbox_message_replayed",
            request_id=request_id,
            reason=reason,
            payload={
                "source_processing_id": source.id,
                "replay_processing_id": row.id,
                "generation": generation,
            },
        )
        return self._processing_view(row)

    def replay_outbox_dead_letter(
        self,
        *,
        actor: Actor,
        outbox_event_id: str,
        request_id: str,
        reason: str,
    ) -> str:
        actor.require_any(ActorRole.ADMIN)
        source = self.session.scalar(
            select(OutboxEventRow).where(OutboxEventRow.id == outbox_event_id).with_for_update()
        )
        if source is None:
            raise LookupError(outbox_event_id)
        if source.dead_lettered_at is None or source.published_at is not None:
            raise ValueError("Only a dead-lettered outbox event can be replayed")
        if source.payload.get("organization_id") != actor.organization_id:
            raise LookupError(outbox_event_id)
        attempt = self.session.scalar(
            select(ConnectorDeliveryAttemptRow)
            .where(ConnectorDeliveryAttemptRow.outbox_event_id == source.id)
            .order_by(ConnectorDeliveryAttemptRow.attempt_number.desc())
            .limit(1)
        )
        if attempt is None:
            raise ValueError("Outbox event has no connector delivery evidence")
        existing = self.session.scalar(
            select(OutboxReplayRow).where(OutboxReplayRow.source_outbox_event_id == source.id)
        )
        if existing is not None:
            raise ValueError("A replay already exists for this terminal outbox event")
        now = utc_now()
        replay_event_id = f"outbox-{uuid4()}"
        replay_id = f"outbox-replay-{uuid4()}"
        self.session.add(
            OutboxEventRow(
                id=replay_event_id,
                deduplication_key=f"outbox-replay-event:{replay_id}",
                delivery_deduplication_key=source.delivery_deduplication_key,
                topic=source.topic,
                aggregate_id=source.aggregate_id,
                payload=source.payload,
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
        self.session.add(
            OutboxReplayRow(
                id=replay_id,
                organization_id=actor.organization_id,
                source_outbox_event_id=source.id,
                replay_outbox_event_id=replay_event_id,
                delivery_deduplication_key=source.delivery_deduplication_key,
                reason=_required_string(reason, "reason", 2000),
                replayed_by=actor.actor_id,
                replayed_at=now,
            )
        )
        self.session.flush()
        self._audit(
            actor=actor,
            aggregate_id=source.id,
            event_type="integration_outbox_dead_letter_replayed",
            request_id=request_id,
            reason=reason,
            payload={
                "source_outbox_event_id": source.id,
                "replay_outbox_event_id": replay_event_id,
                "delivery_deduplication_key": source.delivery_deduplication_key,
                "last_attempt_id": attempt.id,
            },
        )
        return replay_event_id

    def get(
        self,
        *,
        actor: Actor,
        message_id: str,
    ) -> IntegrationInboxMessageView:
        actor.require_any(ActorRole.ADMIN, ActorRole.AUDITOR)
        row = self.session.scalar(
            select(IntegrationInboxMessageRow).where(
                IntegrationInboxMessageRow.id == message_id,
                IntegrationInboxMessageRow.organization_id == actor.organization_id,
            )
        )
        if row is None:
            raise LookupError(message_id)
        processings = tuple(
            self.session.scalars(
                select(IntegrationInboxProcessingRow)
                .where(IntegrationInboxProcessingRow.message_id == row.id)
                .order_by(IntegrationInboxProcessingRow.generation)
            )
        )
        return IntegrationInboxMessageView(
            message_id=row.id,
            source_qualification_id=row.source_qualification_id,
            organization_id=row.organization_id,
            source_message_id=row.source_message_id,
            delivery_deduplication_key=row.delivery_deduplication_key,
            topic=row.topic,
            aggregate_id=row.aggregate_id,
            schema_version=row.schema_version,
            core_hash=row.core_hash,
            payload_hash=row.payload_hash,
            envelope=SignedIntegrationEnvelope.model_validate(row.envelope),
            receipt=SignedIntegrationReceipt.model_validate(row.receipt),
            qualification_snapshot=row.qualification_snapshot,
            received_at=row.received_at,
            processings=tuple(self._processing_view(item) for item in processings),
        )

    def _source_binding(
        self,
        *,
        actor: Actor,
        qualification_id: str,
    ) -> _InboundSourceBinding:
        row = _active_qualification(
            self.session,
            qualification_id=qualification_id,
            organization_id=actor.organization_id,
            required_method=INBOUND_SOURCE_METHOD,
            service_actor_id=actor.actor_id,
        )
        public_key = _payload_string(row, "inbound_signing_public_key_b64", 200)
        validate_integration_public_key(public_key)
        return _InboundSourceBinding(
            row=row,
            topics=_topics(row, "inbound_topics"),
            signing_key_id=_payload_string(row, "inbound_signing_key_id", 200),
            signing_public_key_b64=public_key,
        )

    def _receiver_id(self) -> str:
        if self.settings.integration_receiver_id is None:
            raise ValueError("Integration receipt receiver ID is not configured")
        return _required_string(
            self.settings.integration_receiver_id,
            "integration_receiver_id",
            200,
        )

    def _handler_binding(
        self,
        *,
        actor: Actor,
        qualification_id: str,
    ) -> _InboxHandlerBinding:
        row = _active_qualification(
            self.session,
            qualification_id=qualification_id,
            organization_id=actor.organization_id,
            required_method=INBOX_HANDLER_METHOD,
            service_actor_id=actor.actor_id,
        )
        return _InboxHandlerBinding(
            row=row,
            topics=_topics(row, "inbound_topics"),
        )

    def _existing_message(
        self,
        *,
        source_qualification_id: str,
        source_message_id: str,
        delivery_deduplication_key: str,
    ) -> IntegrationInboxMessageRow | None:
        return self.session.scalar(
            select(IntegrationInboxMessageRow)
            .where(
                IntegrationInboxMessageRow.source_qualification_id == source_qualification_id,
                or_(
                    IntegrationInboxMessageRow.source_message_id == source_message_id,
                    IntegrationInboxMessageRow.delivery_deduplication_key
                    == delivery_deduplication_key,
                ),
            )
            .with_for_update()
        )

    @staticmethod
    def _require_same_message(
        existing: IntegrationInboxMessageRow,
        core_hash: str,
    ) -> None:
        if existing.core_hash != core_hash:
            raise ValueError(
                "Integration message identity or deduplication key was reused for different content"
            )

    def _first_processing(self, message_id: str) -> IntegrationInboxProcessingRow:
        row = self.session.scalar(
            select(IntegrationInboxProcessingRow)
            .where(
                IntegrationInboxProcessingRow.message_id == message_id,
                IntegrationInboxProcessingRow.generation == 1,
            )
            .with_for_update()
        )
        if row is None:
            raise RuntimeError("Integration inbox message has no processing record")
        return row

    def _locked_processing(self, processing_id: str) -> IntegrationInboxProcessingRow:
        row = self.session.scalar(
            select(IntegrationInboxProcessingRow)
            .where(IntegrationInboxProcessingRow.id == processing_id)
            .with_for_update()
        )
        if row is None:
            raise LookupError(processing_id)
        if row.status != "PENDING":
            raise ValueError("Integration inbox processing is already terminal")
        return row

    def _require_processing_owner(
        self,
        row: IntegrationInboxProcessingRow,
        claim: IntegrationInboxClaim,
    ) -> None:
        lease_expires_at = ensure_utc(row.lease_expires_at)
        claim_lease_expires_at = ensure_utc(claim.lease_expires_at)
        last_attempt_at = ensure_utc(row.last_attempt_at)
        claim_deadline_at = ensure_utc(claim.processing_deadline_at)
        expected_deadline_at = (
            last_attempt_at + timedelta(seconds=self.settings.integration_job_timeout_seconds)
            if last_attempt_at is not None
            else None
        )
        now = utc_now()
        if (
            row.message_id != claim.message_id
            or row.generation != claim.generation
            or row.attempts != claim.delivery_attempt
            or row.handler_qualification_id != claim.handler_qualification_id
            or row.locked_by != claim.worker_id
            or row.lease_token != claim.lease_token
            or lease_expires_at is None
            or claim_lease_expires_at is None
            or lease_expires_at != claim_lease_expires_at
            or expected_deadline_at is None
            or claim_deadline_at is None
            or expected_deadline_at != claim_deadline_at
            or lease_expires_at <= now
            or expected_deadline_at <= now
        ):
            raise ValueError("Integration inbox processing lease is invalid or expired")

    @staticmethod
    def _clear_processing_lease(row: IntegrationInboxProcessingRow) -> None:
        row.locked_by = None
        row.lease_token = None
        row.lease_expires_at = None

    @staticmethod
    def _processing_view(
        row: IntegrationInboxProcessingRow,
    ) -> IntegrationInboxProcessingView:
        return IntegrationInboxProcessingView(
            processing_id=row.id,
            generation=row.generation,
            status=row.status,
            attempts=row.attempts,
            available_at=row.available_at,
            handler_qualification_id=row.handler_qualification_id,
            result_reference=row.result_reference,
            result_hash=row.result_hash,
            last_error=row.last_error,
            consumed_at=row.consumed_at,
            dead_lettered_at=row.dead_lettered_at,
            created_at=row.created_at,
        )

    def _audit(
        self,
        *,
        actor: Actor,
        aggregate_id: str,
        event_type: str,
        request_id: str,
        reason: str,
        payload: dict[str, Any],
    ) -> None:
        ProjectService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        ).record_event(
            aggregate_type="integration",
            aggregate_id=aggregate_id,
            event_type=event_type,
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload=payload,
        )


def _active_qualification(
    session: Session,
    *,
    qualification_id: str,
    organization_id: str,
    required_method: str,
    service_actor_id: str | None = None,
) -> AdapterQualificationRow:
    row = session.scalar(
        select(AdapterQualificationRow).where(
            AdapterQualificationRow.id == qualification_id,
            AdapterQualificationRow.status == "APPROVED",
            or_(
                AdapterQualificationRow.valid_until.is_(None),
                AdapterQualificationRow.valid_until >= utc_now().date(),
            ),
        )
    )
    if row is None:
        raise ValueError("Integration adapter qualification is missing, expired, or inactive")
    payload = row.payload
    methods = payload.get("supported_methods")
    if (
        payload.get("organization_id") != organization_id
        or not isinstance(methods, list)
        or required_method not in methods
    ):
        raise ValueError("Integration adapter qualification does not cover this operation")
    if service_actor_id is not None and payload.get("service_actor_id") != service_actor_id:
        raise ValueError("Integration service actor does not match its qualification")
    return row


def _topics(row: AdapterQualificationRow, field: str) -> frozenset[str]:
    raw = row.payload.get(field)
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"Integration qualification has no {field}")
    topics = frozenset(_required_topic(item) for item in raw)
    if len(topics) != len(raw):
        raise ValueError(f"Integration qualification {field} contains duplicates")
    return topics


def _required_topic(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Integration topic must be a string")
    normalized = _required_string(value, "topic", 200)
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", normalized):
        raise ValueError("Integration topic is invalid")
    return normalized


def _payload_string(
    row: AdapterQualificationRow,
    field: str,
    max_length: int,
) -> str:
    raw = row.payload.get(field)
    if not isinstance(raw, str):
        raise ValueError(f"Integration qualification lacks {field}")
    return _required_string(raw, field, max_length)


def _qualification_snapshot(row: AdapterQualificationRow) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        canonical_data(
            {
                "qualification_id": row.id,
                "adapter_name": row.adapter_name,
                "adapter_version": row.adapter_version,
                "test_evidence_hash": row.test_evidence_hash,
                "valid_until": row.valid_until,
                "approved_by": row.approved_by,
                "approved_at": ensure_utc(row.approved_at),
                "controlled_version_id": row.payload.get("controlled_version_id"),
                "controlled_version_hash": row.payload.get("controlled_version_hash"),
            }
        ),
    )


def _integration_signing_material(settings: Settings) -> IntegrationSigningMaterial:
    if not settings.integration_signing_configured:
        raise ValueError("Ed25519 integration signing key is not configured")
    assert settings.integration_signing_key_id is not None
    assert settings.integration_signing_private_key_b64 is not None
    return load_integration_signing_material(
        key_id=settings.integration_signing_key_id,
        private_key_b64=(settings.integration_signing_private_key_b64.get_secret_value()),
    )


def _require_system_actor(actor: Actor) -> None:
    if actor.roles != frozenset({ActorRole.SYSTEM}):
        raise ValueError("Integration worker requires a dedicated SYSTEM identity")


def _required_string(value: str, field: str, max_length: int) -> str:
    normalized = value.strip()
    if not normalized or normalized != value or len(value) > max_length:
        raise ValueError(f"{field} is invalid")
    return normalized


def _sha256(value: str, field: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{field} must be lowercase SHA-256")
    return value


def _error_code(value: str) -> str:
    normalized = value.strip().upper()
    if not _ERROR_CODE.fullmatch(normalized):
        raise ValueError("error_code must be an uppercase machine code")
    return normalized


def _processing_id(message_id: str, generation: int) -> str:
    return (
        "inbox-processing-"
        + content_hash({"message_id": message_id, "generation": generation})[:24]
    )
