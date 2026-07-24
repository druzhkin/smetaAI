from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import BinaryIO
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tenderguard.application.projects import ProjectService
from tenderguard.application.quarantine import QuarantineService
from tenderguard.config import Settings
from tenderguard.domain.common import ensure_utc, utc_now
from tenderguard.domain.enums import ActorRole
from tenderguard.domain.intake import IntakeManifest
from tenderguard.domain.quarantine import (
    MalwareVerdict,
    QuarantinedUploadView,
    QuarantineStatus,
)
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.intake import inspect_intake_stream
from tenderguard.infrastructure.object_store import ObjectStore, StoredObject
from tenderguard.infrastructure.orm import (
    AdapterQualificationRow,
    MalwareScanResultRow,
    QuarantinedUploadRow,
    VerificationFindingRow,
)


class DocumentProcessingTimeoutError(RuntimeError):
    pass


class DocumentProcessingLeaseLostError(RuntimeError):
    pass


@dataclass(frozen=True)
class _ProcessingClaim:
    upload_id: str
    project_id: str
    organization_id: str
    logical_key: str
    title: str
    document_type: str
    critical: bool
    revision_label: str
    original_filename: str
    declared_media_type: str
    make_candidate_current: bool
    object_hash: str
    uploaded_by: str
    invalidated_document_set_revision_id: str | None
    malware_scan_result_id: str
    processor_qualification_id: str
    worker_id: str
    lease_token: str
    deadline_at: datetime
    attempt: int


@dataclass(frozen=True)
class _PreparedDocument:
    stored: StoredObject
    manifest: IntakeManifest
    member_objects: dict[str, str]


class DocumentProcessingService:
    """Short DB transactions around parsing in a separately isolated worker."""

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

    def process(
        self,
        *,
        actor: Actor,
        upload_id: str,
        request_id: str,
        reason: str,
        worker_id: str | None = None,
    ) -> QuarantinedUploadView:
        actor.require_any(ActorRole.SYSTEM)
        resolved_worker_id = self._required(worker_id or actor.actor_id, "worker_id", 128)
        claimed = self._claim(
            actor=actor,
            upload_id=upload_id,
            request_id=request_id,
            reason=reason,
            worker_id=resolved_worker_id,
        )
        if isinstance(claimed, QuarantinedUploadView):
            return claimed
        try:
            prepared = self._prepare(claimed)
            return self._complete(
                actor=actor,
                claim=claimed,
                prepared=prepared,
                request_id=request_id,
                reason=reason,
            )
        except Exception as error:
            return self._record_failure(
                actor=actor,
                claim=claimed,
                request_id=request_id,
                reason=reason,
                error=error,
            )

    def dead_letter(
        self,
        *,
        actor: Actor,
        upload_id: str,
        request_id: str,
        reason: str,
        error_code: str,
    ) -> QuarantinedUploadView:
        actor.require_any(ActorRole.SYSTEM)
        with self.session_factory.begin() as session:
            upload = self._locked_upload(session, actor, upload_id)
            if upload.status == QuarantineStatus.PROCESSED.value:
                return self._view(session, actor, upload)
            if upload.status == QuarantineStatus.PROCESSING_DEAD_LETTERED.value:
                return self._view(session, actor, upload)
            if upload.status not in {
                QuarantineStatus.CLEAN.value,
                QuarantineStatus.PROCESSING.value,
                QuarantineStatus.PROCESSING_FAILED.value,
            }:
                raise ValueError("Only a clean or processing document upload can be dead-lettered")
            self._dead_letter_row(
                session=session,
                actor=actor,
                upload=upload,
                request_id=request_id,
                reason=reason,
                error_code=error_code,
            )
            return self._view(session, actor, upload)

    def _claim(
        self,
        *,
        actor: Actor,
        upload_id: str,
        request_id: str,
        reason: str,
        worker_id: str,
    ) -> _ProcessingClaim | QuarantinedUploadView:
        with self.session_factory.begin() as session:
            upload = self._locked_upload(session, actor, upload_id)
            if upload.status in {
                QuarantineStatus.PROCESSED.value,
                QuarantineStatus.PROCESSING_DEAD_LETTERED.value,
            }:
                return self._view(session, actor, upload)
            now = utc_now()
            stale_lease = False
            if upload.status == QuarantineStatus.PROCESSING.value:
                lease_expires_at = ensure_utc(upload.processing_lease_expires_at)
                if lease_expires_at is None or lease_expires_at > now:
                    raise ValueError("Document upload is already leased by an active worker")
                stale_lease = True
            elif upload.status not in {
                QuarantineStatus.CLEAN.value,
                QuarantineStatus.PROCESSING_FAILED.value,
            }:
                raise ValueError("Only a qualified CLEAN upload can be processed")
            if upload.processing_attempts >= self.settings.document_job_max_attempts:
                self._dead_letter_row(
                    session=session,
                    actor=actor,
                    upload=upload,
                    request_id=request_id,
                    reason=reason,
                    error_code="DOCUMENT_PROCESSING_ATTEMPTS_EXHAUSTED",
                )
                return self._view(session, actor, upload)

            qualification = self._qualified_processor(session, actor)
            clean_scan = session.scalar(
                select(MalwareScanResultRow)
                .where(
                    MalwareScanResultRow.quarantined_upload_id == upload.id,
                    MalwareScanResultRow.verdict == MalwareVerdict.CLEAN.value,
                    MalwareScanResultRow.scanned_object_hash == upload.object_hash,
                )
                .order_by(MalwareScanResultRow.recorded_at.desc())
                .limit(1)
            )
            if clean_scan is None:
                raise ValueError("CLEAN state has no exact qualified scan evidence")
            self._validate_clean_scan_qualification(session, actor, clean_scan)

            previous_status = upload.status
            lease_token = f"document-lease-{uuid4()}"
            lease_expires_at = now + timedelta(seconds=self.settings.document_job_lease_seconds)
            deadline_at = now + timedelta(seconds=self.settings.document_job_timeout_seconds)
            upload.status = QuarantineStatus.PROCESSING.value
            upload.processing_attempts += 1
            upload.processing_worker_id = worker_id
            upload.processing_lease_token = lease_token
            upload.processing_lease_expires_at = lease_expires_at
            upload.processing_deadline_at = deadline_at
            upload.processing_started_at = now
            upload.failure_code = None
            upload.failure_detail = None
            upload.updated_at = now
            project_service = self._project_service(session)
            project_service.record_event(
                aggregate_type="quarantined_upload",
                aggregate_id=upload.id,
                event_type="document_processing_started",
                actor=actor,
                request_id=request_id,
                reason=reason,
                payload={
                    "upload_id": upload.id,
                    "object_hash": upload.object_hash,
                    "malware_scan_result_id": clean_scan.id,
                    "processor_qualification_id": qualification.id,
                    "worker_id": worker_id,
                    "attempt": upload.processing_attempts,
                    "stale_lease_reclaimed": stale_lease,
                    "lease_expires_at": lease_expires_at.isoformat(),
                    "deadline_at": deadline_at.isoformat(),
                    "from_status": previous_status,
                    "to_status": QuarantineStatus.PROCESSING.value,
                },
            )
            session.flush()
            return _ProcessingClaim(
                upload_id=upload.id,
                project_id=upload.project_id,
                organization_id=upload.organization_id,
                logical_key=upload.logical_key,
                title=upload.title,
                document_type=upload.document_type,
                critical=upload.critical,
                revision_label=upload.revision_label,
                original_filename=upload.original_filename,
                declared_media_type=upload.declared_media_type,
                make_candidate_current=upload.make_candidate_current,
                object_hash=upload.object_hash,
                uploaded_by=upload.uploaded_by,
                invalidated_document_set_revision_id=(upload.invalidated_document_set_revision_id),
                malware_scan_result_id=clean_scan.id,
                processor_qualification_id=qualification.id,
                worker_id=worker_id,
                lease_token=lease_token,
                deadline_at=deadline_at,
                attempt=upload.processing_attempts,
            )

    def _prepare(self, claim: _ProcessingClaim) -> _PreparedDocument:
        self._require_before_deadline(claim)
        with self.quarantine_store.open(claim.object_hash) as source:
            stored = self.evidence_store.put(
                source,
                max_bytes=self.settings.max_upload_bytes,
            )
        if stored.object_hash != claim.object_hash:
            raise RuntimeError("Evidence promotion changed the object hash")
        self._require_before_deadline(claim)
        member_objects: dict[str, str] = {}

        def store_member(path: str, stream: BinaryIO) -> None:
            self._require_before_deadline(claim)
            member = self.evidence_store.put(
                stream,
                max_bytes=self.settings.max_archive_unpacked_bytes,
            )
            member_objects[path] = member.object_hash
            self._require_before_deadline(claim)

        with self.evidence_store.open(stored.object_hash) as source:
            manifest = inspect_intake_stream(
                claim.original_filename,
                source,
                self.settings,
                on_member=store_member,
            )
        self._require_before_deadline(claim)
        return _PreparedDocument(
            stored=stored,
            manifest=manifest,
            member_objects=member_objects,
        )

    def _complete(
        self,
        *,
        actor: Actor,
        claim: _ProcessingClaim,
        prepared: _PreparedDocument,
        request_id: str,
        reason: str,
    ) -> QuarantinedUploadView:
        self._require_before_deadline(claim)
        with self.session_factory.begin() as session:
            upload = self._locked_upload(session, actor, claim.upload_id)
            self._require_processing_owner(upload, claim)
            self._require_before_deadline(claim)
            qualification = self._qualified_processor(session, actor)
            if qualification.id != claim.processor_qualification_id:
                raise ValueError("Document processor qualification changed during processing")
            clean_scan = session.get(MalwareScanResultRow, claim.malware_scan_result_id)
            if (
                clean_scan is None
                or clean_scan.quarantined_upload_id != upload.id
                or clean_scan.scanned_object_hash != upload.object_hash
                or clean_scan.verdict != MalwareVerdict.CLEAN.value
            ):
                raise ValueError("Qualified CLEAN scan changed during processing")
            self._validate_clean_scan_qualification(session, actor, clean_scan)

            project_service = self._project_service(session)
            result = project_service.register_scanned_document_revision(
                actor=actor,
                project_id=claim.project_id,
                logical_key=claim.logical_key,
                title=claim.title,
                document_type=claim.document_type,
                critical=claim.critical,
                revision_label=claim.revision_label,
                filename=claim.original_filename,
                media_type=claim.declared_media_type,
                stored=prepared.stored,
                manifest=prepared.manifest,
                member_objects=prepared.member_objects,
                quarantine_upload_id=upload.id,
                submitted_by=claim.uploaded_by,
                request_id=request_id,
                reason=reason,
                make_candidate_current=claim.make_candidate_current,
                invalidated_document_set_revision_id=(claim.invalidated_document_set_revision_id),
            )
            self._require_before_deadline(claim)
            now = utc_now()
            upload.status = QuarantineStatus.PROCESSED.value
            upload.processed_document_id = result.document_id
            upload.processed_document_revision_id = result.document_revision_id
            upload.candidate_document_set_revision_id = result.candidate_document_set_revision_id
            upload.result_payload = result.model_dump(mode="json")
            upload.failure_code = None
            upload.failure_detail = None
            self._clear_processing_lease(upload)
            upload.updated_at = now
            pending_finding = session.get(
                VerificationFindingRow,
                ProjectService.quarantine_finding_id(upload.id),
            )
            if pending_finding is None:
                raise RuntimeError("Quarantine blocker finding is missing")
            pending_finding.resolved = True
            pending_finding.payload = {
                **pending_finding.payload,
                "resolved_by_document_revision_id": result.document_revision_id,
                "malware_scan_result_id": clean_scan.id,
                "processor_qualification_id": qualification.id,
                "processing_attempt": claim.attempt,
            }
            pending_finding.updated_at = now
            self._resolve_rejected_predecessors(
                session=session,
                upload=upload,
                document_revision_id=result.document_revision_id,
                now=now,
            )
            event_payload = {
                "upload_id": upload.id,
                "object_hash": upload.object_hash,
                "document_id": upload.processed_document_id,
                "document_revision_id": upload.processed_document_revision_id,
                "candidate_document_set_revision_id": (upload.candidate_document_set_revision_id),
                "malware_scan_result_id": clean_scan.id,
                "processor_qualification_id": qualification.id,
                "worker_id": claim.worker_id,
                "attempt": claim.attempt,
                "from_status": QuarantineStatus.PROCESSING.value,
                "to_status": QuarantineStatus.PROCESSED.value,
            }
            project_service.record_event(
                aggregate_type="quarantined_upload",
                aggregate_id=upload.id,
                event_type="document_processing_completed",
                actor=actor,
                request_id=request_id,
                reason=reason,
                payload=event_payload,
            )
            project_service.record_event(
                aggregate_type="project",
                aggregate_id=upload.project_id,
                event_type="quarantined_document_promoted",
                actor=actor,
                request_id=request_id,
                reason=reason,
                payload=event_payload,
            )
            project_service.enqueue_event(
                topic="document.upload.processed",
                aggregate_id=upload.id,
                payload={
                    "project_id": upload.project_id,
                    "upload_id": upload.id,
                    "document_revision_id": upload.processed_document_revision_id,
                },
            )
            self._require_before_deadline(claim)
            session.flush()
            return self._view(session, actor, upload)

    def _record_failure(
        self,
        *,
        actor: Actor,
        claim: _ProcessingClaim,
        request_id: str,
        reason: str,
        error: Exception,
    ) -> QuarantinedUploadView:
        with self.session_factory.begin() as session:
            upload = self._locked_upload(session, actor, claim.upload_id)
            if (
                upload.status != QuarantineStatus.PROCESSING.value
                or upload.processing_lease_token != claim.lease_token
                or upload.processing_worker_id != claim.worker_id
            ):
                self._project_service(session).record_event(
                    aggregate_type="quarantined_upload",
                    aggregate_id=upload.id,
                    event_type="document_processing_stale_completion_rejected",
                    actor=actor,
                    request_id=request_id,
                    reason=reason,
                    payload={
                        "upload_id": upload.id,
                        "object_hash": upload.object_hash,
                        "stale_worker_id": claim.worker_id,
                        "stale_attempt": claim.attempt,
                        "current_status": upload.status,
                        "current_worker_id": upload.processing_worker_id,
                        "error_type": type(error).__name__,
                    },
                )
                return self._view(session, actor, upload)
            if isinstance(error, DocumentProcessingTimeoutError):
                error_code = "DOCUMENT_PROCESSING_TIMEOUT"
            elif isinstance(error, DocumentProcessingLeaseLostError):
                error_code = "DOCUMENT_PROCESSING_LEASE_LOST"
            else:
                error_code = "DOCUMENT_PROCESSING_FAILED"
            if upload.processing_attempts >= self.settings.document_job_max_attempts:
                self._dead_letter_row(
                    session=session,
                    actor=actor,
                    upload=upload,
                    request_id=request_id,
                    reason=reason,
                    error_code=error_code,
                    error_type=type(error).__name__,
                    processor_qualification_id=claim.processor_qualification_id,
                )
                return self._view(session, actor, upload)

            now = utc_now()
            upload.status = QuarantineStatus.PROCESSING_FAILED.value
            upload.failure_code = error_code
            upload.failure_detail = type(error).__name__
            self._clear_processing_lease(upload)
            upload.updated_at = now
            event_payload = {
                "upload_id": upload.id,
                "object_hash": upload.object_hash,
                "error_code": error_code,
                "error_type": type(error).__name__,
                "processor_qualification_id": claim.processor_qualification_id,
                "worker_id": claim.worker_id,
                "attempt": claim.attempt,
                "from_status": QuarantineStatus.PROCESSING.value,
                "to_status": QuarantineStatus.PROCESSING_FAILED.value,
            }
            project_service = self._project_service(session)
            project_service.record_event(
                aggregate_type="quarantined_upload",
                aggregate_id=upload.id,
                event_type="document_processing_failed",
                actor=actor,
                request_id=request_id,
                reason=reason,
                payload=event_payload,
            )
            project_service.record_event(
                aggregate_type="project",
                aggregate_id=upload.project_id,
                event_type="document_processing_failed",
                actor=actor,
                request_id=request_id,
                reason=reason,
                payload=event_payload,
            )
            project_service.enqueue_event(
                topic="document.upload.processing-failed",
                aggregate_id=upload.id,
                payload={
                    "project_id": upload.project_id,
                    "upload_id": upload.id,
                    "error_code": error_code,
                },
            )
            session.flush()
            return self._view(session, actor, upload)

    def _dead_letter_row(
        self,
        *,
        session: Session,
        actor: Actor,
        upload: QuarantinedUploadRow,
        request_id: str,
        reason: str,
        error_code: str,
        error_type: str | None = None,
        processor_qualification_id: str | None = None,
    ) -> None:
        if upload.status == QuarantineStatus.PROCESSING_DEAD_LETTERED.value:
            return
        previous_status = upload.status
        now = utc_now()
        worker_id = upload.processing_worker_id
        upload.status = QuarantineStatus.PROCESSING_DEAD_LETTERED.value
        upload.failure_code = error_code
        upload.failure_detail = error_type
        upload.processing_dead_lettered_at = now
        self._clear_processing_lease(upload)
        upload.updated_at = now
        payload = {
            "upload_id": upload.id,
            "object_hash": upload.object_hash,
            "error_code": error_code,
            "error_type": error_type,
            "processor_qualification_id": processor_qualification_id,
            "worker_id": worker_id,
            "attempts": upload.processing_attempts,
            "from_status": previous_status,
            "to_status": QuarantineStatus.PROCESSING_DEAD_LETTERED.value,
        }
        project_service = self._project_service(session)
        project_service.record_event(
            aggregate_type="quarantined_upload",
            aggregate_id=upload.id,
            event_type="document_processing_dead_lettered",
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload=payload,
        )
        project_service.record_event(
            aggregate_type="project",
            aggregate_id=upload.project_id,
            event_type="document_processing_dead_lettered",
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload=payload,
        )
        project_service.enqueue_event(
            topic="document.upload.processing-dead-lettered",
            aggregate_id=upload.id,
            payload={
                "project_id": upload.project_id,
                "upload_id": upload.id,
                "error_code": error_code,
            },
        )
        session.flush()

    def _resolve_rejected_predecessors(
        self,
        *,
        session: Session,
        upload: QuarantinedUploadRow,
        document_revision_id: str,
        now: datetime,
    ) -> None:
        rejected_predecessors = list(
            session.scalars(
                select(QuarantinedUploadRow).where(
                    QuarantinedUploadRow.project_id == upload.project_id,
                    QuarantinedUploadRow.logical_key == upload.logical_key,
                    QuarantinedUploadRow.status == QuarantineStatus.REJECTED.value,
                    QuarantinedUploadRow.created_at < upload.created_at,
                )
            )
        )
        predecessor_ids = {item.id for item in rejected_predecessors}
        if not predecessor_ids:
            return
        unresolved_findings = list(
            session.scalars(
                select(VerificationFindingRow).where(
                    VerificationFindingRow.project_id == upload.project_id,
                    VerificationFindingRow.resolved.is_(False),
                    VerificationFindingRow.code.in_(
                        {
                            "QUARANTINE_SCAN_PENDING",
                            "MALWARE_DETECTED",
                        }
                    ),
                )
            )
        )
        for finding in unresolved_findings:
            if finding.payload.get("upload_id") in predecessor_ids:
                finding.resolved = True
                finding.payload = {
                    **finding.payload,
                    "resolved_by_clean_replacement_upload_id": upload.id,
                    "resolved_by_document_revision_id": document_revision_id,
                }
                finding.updated_at = now

    def _qualified_processor(
        self,
        session: Session,
        actor: Actor,
    ) -> AdapterQualificationRow:
        qualification = session.scalar(
            select(AdapterQualificationRow).where(
                AdapterQualificationRow.id == self.settings.document_processor_qualification_id,
                AdapterQualificationRow.adapter_name == self.settings.document_processor_adapter,
                AdapterQualificationRow.status == "APPROVED",
            )
        )
        if qualification is None:
            raise ValueError("Document processor is not actively qualified")
        if qualification.valid_until and qualification.valid_until < utc_now().date():
            raise ValueError("Document processor qualification has expired")
        if qualification.payload.get("organization_id") != actor.organization_id:
            raise ValueError("Document processor qualification belongs to another organisation")
        if "DOCUMENT_INTAKE" not in qualification.payload.get("supported_methods", []):
            raise ValueError("Qualification does not authorize document intake")
        return qualification

    def _validate_clean_scan_qualification(
        self,
        session: Session,
        actor: Actor,
        scan: MalwareScanResultRow,
    ) -> None:
        if scan.adapter_qualification_id != self.settings.malware_scanner_qualification_id:
            raise ValueError("CLEAN scan is outside the configured scanner qualification")
        qualification = session.scalar(
            select(AdapterQualificationRow).where(
                AdapterQualificationRow.id == scan.adapter_qualification_id,
                AdapterQualificationRow.adapter_name == self.settings.malware_scanner_adapter,
                AdapterQualificationRow.status == "APPROVED",
            )
        )
        if qualification is None:
            raise ValueError("CLEAN scan qualification is no longer active")
        if qualification.valid_until and qualification.valid_until < utc_now().date():
            raise ValueError("CLEAN scan qualification has expired")
        if qualification.payload.get("organization_id") != actor.organization_id:
            raise ValueError("CLEAN scan qualification belongs to another organisation")
        if "MALWARE_SCAN" not in qualification.payload.get("supported_methods", []):
            raise ValueError("CLEAN scan qualification does not authorize malware scanning")

    def _locked_upload(
        self,
        session: Session,
        actor: Actor,
        upload_id: str,
    ) -> QuarantinedUploadRow:
        upload = session.scalar(
            select(QuarantinedUploadRow)
            .where(
                QuarantinedUploadRow.id == upload_id,
                QuarantinedUploadRow.organization_id == actor.organization_id,
            )
            .with_for_update()
        )
        if upload is None:
            raise LookupError(upload_id)
        return upload

    @staticmethod
    def _require_processing_owner(
        upload: QuarantinedUploadRow,
        claim: _ProcessingClaim,
    ) -> None:
        if (
            upload.status != QuarantineStatus.PROCESSING.value
            or upload.processing_worker_id != claim.worker_id
            or upload.processing_lease_token != claim.lease_token
        ):
            raise DocumentProcessingLeaseLostError(
                "Document processing lease is owned by another worker"
            )

    @staticmethod
    def _clear_processing_lease(upload: QuarantinedUploadRow) -> None:
        upload.processing_worker_id = None
        upload.processing_lease_token = None
        upload.processing_lease_expires_at = None
        upload.processing_deadline_at = None

    @staticmethod
    def _require_before_deadline(claim: _ProcessingClaim) -> None:
        if utc_now() > claim.deadline_at:
            raise DocumentProcessingTimeoutError(
                "Document processing exceeded its persisted deadline"
            )

    def _view(
        self,
        session: Session,
        actor: Actor,
        upload: QuarantinedUploadRow,
    ) -> QuarantinedUploadView:
        return self._quarantine_service(session).get(
            actor=actor,
            project_id=upload.project_id,
            upload_id=upload.id,
        )

    def _quarantine_service(self, session: Session) -> QuarantineService:
        return QuarantineService(
            session=session,
            settings=self.settings,
            evidence_store=self.evidence_store,
            quarantine_store=self.quarantine_store,
        )

    def _project_service(self, session: Session) -> ProjectService:
        return ProjectService(
            session=session,
            settings=self.settings,
            object_store=self.evidence_store,
        )

    @staticmethod
    def _required(value: str, field: str, max_length: int) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{field} is required")
        if len(normalized) > max_length:
            raise ValueError(f"{field} exceeds {max_length} characters")
        return normalized
