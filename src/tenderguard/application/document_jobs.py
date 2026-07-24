from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tenderguard.application.document_processing import DocumentProcessingService
from tenderguard.application.outbox import OutboxDeliveryService
from tenderguard.application.quarantine import QuarantineService
from tenderguard.config import Settings
from tenderguard.domain.enums import ActorRole
from tenderguard.domain.jobs import (
    DispatchDisposition,
    DocumentDispatchResult,
    OutboxClaim,
    OutboxSettlement,
)
from tenderguard.domain.quarantine import QuarantineStatus
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.object_store import ObjectStore
from tenderguard.infrastructure.orm import QuarantinedUploadRow

DOCUMENT_INTAKE_TOPIC = "document.upload.scan-clean"


class DocumentIntakeDispatcher:
    """Idempotently delivers clean-upload events to the isolated parser service."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        settings: Settings,
        evidence_store: ObjectStore,
        quarantine_store: ObjectStore,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.evidence_store = evidence_store
        self.quarantine_store = quarantine_store
        self.processor = DocumentProcessingService(
            session_factory=session_factory,
            settings=settings,
            evidence_store=evidence_store,
            quarantine_store=quarantine_store,
        )

    def dispatch_next(
        self,
        *,
        worker_id: str,
        upload_id: str | None = None,
    ) -> DocumentDispatchResult:
        claim = self._claim(worker_id=worker_id, upload_id=upload_id)
        if claim is None:
            return self._idle_result(upload_id)
        upload = self._upload_for_claim(claim)
        if upload is None:
            self._reject(
                claim,
                error_code="DOCUMENT_JOB_PAYLOAD_INVALID",
                force_dead_letter=True,
            )
            return DocumentDispatchResult(
                disposition=DispatchDisposition.DEAD_LETTERED,
                outbox_event_id=claim.event_id,
                error_code="DOCUMENT_JOB_PAYLOAD_INVALID",
            )
        actor = self._worker_actor(upload.organization_id)
        request_id = f"document-dispatch-{uuid4()}"
        try:
            result = self.processor.process(
                actor=actor,
                upload_id=upload.id,
                request_id=request_id,
                reason=("Durable outbox delivery to qualified isolated document worker"),
                worker_id=worker_id,
            )
        except Exception as error:
            error_code = self._dispatch_error_code(error)
            return self._settle_processing_error(
                claim=claim,
                actor=actor,
                upload_id=upload.id,
                request_id=request_id,
                error_code=error_code,
            )

        if result.status is QuarantineStatus.PROCESSED:
            self._acknowledge(claim)
            return DocumentDispatchResult(
                disposition=DispatchDisposition.PROCESSED,
                outbox_event_id=claim.event_id,
                upload=result,
            )
        if result.status is QuarantineStatus.PROCESSING_DEAD_LETTERED:
            self._reject(
                claim,
                error_code=result.failure_code or "DOCUMENT_PROCESSING_DEAD_LETTERED",
                force_dead_letter=True,
            )
            return DocumentDispatchResult(
                disposition=DispatchDisposition.DEAD_LETTERED,
                outbox_event_id=claim.event_id,
                upload=result,
                error_code=result.failure_code,
            )
        return self._settle_processing_error(
            claim=claim,
            actor=actor,
            upload_id=upload.id,
            request_id=request_id,
            error_code=result.failure_code or "DOCUMENT_PROCESSING_INCOMPLETE",
            current_result=result,
        )

    def _settle_processing_error(
        self,
        *,
        claim: OutboxClaim,
        actor: Actor,
        upload_id: str,
        request_id: str,
        error_code: str,
        current_result: object | None = None,
    ) -> DocumentDispatchResult:
        terminal = claim.delivery_attempt >= self.settings.document_job_max_attempts
        result = current_result
        if terminal:
            try:
                result = self.processor.dead_letter(
                    actor=actor,
                    upload_id=upload_id,
                    request_id=request_id,
                    reason="Document intake delivery attempts were exhausted",
                    error_code=error_code,
                )
            except ValueError:
                self._reject(
                    claim,
                    error_code="DOCUMENT_DEAD_LETTER_STATE_INVALID",
                    force_dead_letter=True,
                )
                return DocumentDispatchResult(
                    disposition=DispatchDisposition.DEAD_LETTERED,
                    outbox_event_id=claim.event_id,
                    error_code="DOCUMENT_DEAD_LETTER_STATE_INVALID",
                )
            except Exception:
                self._reject(
                    claim,
                    error_code="DOCUMENT_DEAD_LETTER_PERSISTENCE_FAILED",
                    allow_dead_letter=False,
                )
                return DocumentDispatchResult(
                    disposition=DispatchDisposition.RETRY_SCHEDULED,
                    outbox_event_id=claim.event_id,
                    error_code="DOCUMENT_DEAD_LETTER_PERSISTENCE_FAILED",
                )
            self._reject(
                claim,
                error_code=error_code,
                force_dead_letter=True,
            )
            return DocumentDispatchResult(
                disposition=DispatchDisposition.DEAD_LETTERED,
                outbox_event_id=claim.event_id,
                upload=result,
                error_code=error_code,
            )
        settlement = self._reject(claim, error_code=error_code)
        return DocumentDispatchResult(
            disposition=(
                DispatchDisposition.DEAD_LETTERED
                if settlement.dead_lettered
                else DispatchDisposition.RETRY_SCHEDULED
            ),
            outbox_event_id=claim.event_id,
            upload=result,
            error_code=error_code,
        )

    def _claim(self, *, worker_id: str, upload_id: str | None) -> OutboxClaim | None:
        with self.session_factory.begin() as session:
            return OutboxDeliveryService(
                session=session,
                settings=self.settings,
            ).claim_next(
                topics={DOCUMENT_INTAKE_TOPIC},
                worker_id=worker_id,
                aggregate_id=upload_id,
            )

    def _acknowledge(self, claim: OutboxClaim) -> OutboxSettlement:
        with self.session_factory.begin() as session:
            return OutboxDeliveryService(
                session=session,
                settings=self.settings,
            ).acknowledge(claim)

    def _reject(
        self,
        claim: OutboxClaim,
        *,
        error_code: str,
        force_dead_letter: bool = False,
        allow_dead_letter: bool = True,
    ) -> OutboxSettlement:
        with self.session_factory.begin() as session:
            return OutboxDeliveryService(
                session=session,
                settings=self.settings,
            ).reject(
                claim,
                error_code=error_code,
                force_dead_letter=force_dead_letter,
                allow_dead_letter=allow_dead_letter,
            )

    def _upload_for_claim(self, claim: OutboxClaim) -> QuarantinedUploadRow | None:
        upload_id = claim.payload.get("upload_id")
        project_id = claim.payload.get("project_id")
        if (
            not isinstance(upload_id, str)
            or not isinstance(project_id, str)
            or upload_id != claim.aggregate_id
        ):
            return None
        with self.session_factory() as session:
            return session.scalar(
                select(QuarantinedUploadRow).where(
                    QuarantinedUploadRow.id == upload_id,
                    QuarantinedUploadRow.project_id == project_id,
                )
            )

    def _idle_result(self, upload_id: str | None) -> DocumentDispatchResult:
        if upload_id is None:
            return DocumentDispatchResult(disposition=DispatchDisposition.IDLE)
        with self.session_factory() as session:
            upload = session.get(QuarantinedUploadRow, upload_id)
            if upload is None:
                return DocumentDispatchResult(
                    disposition=DispatchDisposition.IDLE,
                    error_code="DOCUMENT_UPLOAD_NOT_FOUND",
                )
            actor = self._worker_actor(upload.organization_id)
            view = QuarantineService(
                session=session,
                settings=self.settings,
                evidence_store=self.evidence_store,
                quarantine_store=self.quarantine_store,
            ).get(actor=actor, project_id=upload.project_id, upload_id=upload.id)
            if view.status is QuarantineStatus.PROCESSED:
                disposition = DispatchDisposition.PROCESSED
            elif view.status is QuarantineStatus.PROCESSING_DEAD_LETTERED:
                disposition = DispatchDisposition.DEAD_LETTERED
            else:
                disposition = DispatchDisposition.IDLE
            return DocumentDispatchResult(
                disposition=disposition,
                upload=view,
                error_code=view.failure_code,
            )

    @staticmethod
    def _dispatch_error_code(error: Exception) -> str:
        if isinstance(error, ValueError):
            return "DOCUMENT_JOB_VALIDATION_FAILED"
        if isinstance(error, LookupError):
            return "DOCUMENT_JOB_TARGET_NOT_FOUND"
        return "DOCUMENT_JOB_HANDLER_FAILED"

    def _worker_actor(self, organization_id: str) -> Actor:
        if not self.settings.document_worker_actor_id:
            raise ValueError("Document worker actor is not configured")
        return Actor(
            actor_id=self.settings.document_worker_actor_id,
            organization_id=organization_id,
            roles=frozenset({ActorRole.SYSTEM}),
        )
