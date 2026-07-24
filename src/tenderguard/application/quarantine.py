from __future__ import annotations

from datetime import timedelta
from typing import BinaryIO
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from tenderguard.application.projects import ProjectService
from tenderguard.config import Settings
from tenderguard.domain.common import canonical_json, content_hash, ensure_utc, utc_now
from tenderguard.domain.enums import ActorRole, Severity
from tenderguard.domain.intake import normalize_upload_filename
from tenderguard.domain.quarantine import (
    MalwareScanResult,
    MalwareVerdict,
    QuarantinedUploadView,
    QuarantineStatus,
)
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.object_store import ObjectStore
from tenderguard.infrastructure.orm import (
    AdapterQualificationRow,
    DocumentRevisionRow,
    DocumentRow,
    MalwareScanResultRow,
    OutboxEventRow,
    QuarantinedUploadRow,
    VerificationFindingRow,
)


class QuarantineService:
    def __init__(
        self,
        *,
        session: Session,
        settings: Settings,
        evidence_store: ObjectStore,
        quarantine_store: ObjectStore,
    ) -> None:
        self.session = session
        self.settings = settings
        self.evidence_store = evidence_store
        self.quarantine_store = quarantine_store

    def receive(
        self,
        *,
        actor: Actor,
        project_id: str,
        logical_key: str,
        title: str,
        document_type: str,
        critical: bool,
        revision_label: str,
        filename: str,
        media_type: str,
        stream: BinaryIO,
        request_id: str,
        reason: str,
        make_candidate_current: bool,
    ) -> QuarantinedUploadView:
        actor.require_any(
            ActorRole.ESTIMATOR,
            ActorRole.TECHNICAL_EXPERT,
            ActorRole.ADMIN,
        )
        logical_key = self._required_text(logical_key, "logical_key", 300)
        title = self._required_text(title, "title", 1000)
        document_type = self._required_text(document_type, "document_type", 100)
        revision_label = self._required_text(revision_label, "revision_label", 100)
        self._required_text(reason, "reason", 2000)
        safe_filename = normalize_upload_filename(filename)
        project_service = self._project_service()
        project_service.get_project(actor=actor, project_id=project_id, lock=True)
        active_statuses = {
            QuarantineStatus.QUARANTINED.value,
            QuarantineStatus.CLEAN.value,
            QuarantineStatus.SCAN_FAILED.value,
            QuarantineStatus.PROCESSING.value,
            QuarantineStatus.PROCESSING_FAILED.value,
            QuarantineStatus.PROCESSING_DEAD_LETTERED.value,
        }
        active_upload = self.session.scalar(
            select(QuarantinedUploadRow).where(
                QuarantinedUploadRow.project_id == project_id,
                QuarantinedUploadRow.logical_key == logical_key,
                QuarantinedUploadRow.status.in_(active_statuses),
            )
        )
        if active_upload is not None:
            raise ValueError("An unresolved quarantined upload already exists for this document")
        rejected_predecessor = self.session.scalar(
            select(QuarantinedUploadRow)
            .where(
                QuarantinedUploadRow.project_id == project_id,
                QuarantinedUploadRow.logical_key == logical_key,
                QuarantinedUploadRow.status == QuarantineStatus.REJECTED.value,
            )
            .order_by(QuarantinedUploadRow.created_at.desc())
            .limit(1)
        )
        existing_revision = self.session.scalar(
            select(DocumentRevisionRow.id)
            .join(DocumentRow, DocumentRow.id == DocumentRevisionRow.document_id)
            .where(
                DocumentRow.project_id == project_id,
                DocumentRow.logical_key == logical_key,
                DocumentRevisionRow.revision_label == revision_label,
            )
        )
        if existing_revision is not None:
            raise ValueError("Document revision label already exists for this document")
        stored = self.quarantine_store.put(
            stream,
            max_bytes=self.settings.max_upload_bytes,
        )
        now = utc_now()
        upload_id = f"quarantine-upload-{uuid4()}"
        invalidated_document_set_id = project_service.register_quarantined_upload(
            actor=actor,
            project_id=project_id,
            upload_id=upload_id,
            object_hash=stored.object_hash,
            make_candidate_current=make_candidate_current,
            inherited_invalidated_document_set_id=(
                rejected_predecessor.invalidated_document_set_revision_id
                if rejected_predecessor
                else None
            ),
            request_id=request_id,
            reason=reason,
        )
        upload = QuarantinedUploadRow(
            id=upload_id,
            project_id=project_id,
            organization_id=actor.organization_id,
            logical_key=logical_key,
            title=title,
            document_type=document_type,
            critical=critical,
            revision_label=revision_label,
            original_filename=safe_filename,
            declared_media_type=media_type[:200],
            make_candidate_current=make_candidate_current,
            object_hash=stored.object_hash,
            object_key=stored.object_key,
            size_bytes=stored.size_bytes,
            status=QuarantineStatus.QUARANTINED.value,
            uploaded_by=actor.actor_id,
            invalidated_document_set_revision_id=invalidated_document_set_id,
            processed_document_id=None,
            processed_document_revision_id=None,
            candidate_document_set_revision_id=None,
            result_payload=None,
            failure_code=None,
            failure_detail=None,
            processing_attempts=0,
            processing_worker_id=None,
            processing_lease_token=None,
            processing_lease_expires_at=None,
            processing_deadline_at=None,
            processing_started_at=None,
            processing_dead_lettered_at=None,
            created_at=now,
            updated_at=now,
        )
        self.session.add(upload)
        self.session.flush()
        return self._view(upload)

    def get(
        self,
        *,
        actor: Actor,
        project_id: str,
        upload_id: str,
    ) -> QuarantinedUploadView:
        self._project_service().get_project(actor=actor, project_id=project_id)
        upload = self.session.scalar(
            select(QuarantinedUploadRow).where(
                QuarantinedUploadRow.id == upload_id,
                QuarantinedUploadRow.project_id == project_id,
                QuarantinedUploadRow.organization_id == actor.organization_id,
            )
        )
        if upload is None:
            raise LookupError(upload_id)
        return self._view(upload)

    def record_scan_result(
        self,
        *,
        actor: Actor,
        project_id: str,
        upload_id: str,
        result: MalwareScanResult,
        request_id: str,
        reason: str,
    ) -> QuarantinedUploadView:
        actor.require_any(ActorRole.SYSTEM)
        self._project_service().get_project(actor=actor, project_id=project_id)
        upload = self.session.scalar(
            select(QuarantinedUploadRow)
            .where(
                QuarantinedUploadRow.id == upload_id,
                QuarantinedUploadRow.project_id == project_id,
                QuarantinedUploadRow.organization_id == actor.organization_id,
            )
            .with_for_update()
        )
        if upload is None:
            raise LookupError(upload_id)
        self._validate_scan_result(actor=actor, upload=upload, result=result)
        existing = self.session.scalar(
            select(MalwareScanResultRow).where(
                MalwareScanResultRow.adapter_qualification_id == result.adapter_qualification_id,
                MalwareScanResultRow.scanner_run_id == result.scanner_run_id,
            )
        )
        result_payload = result.model_dump(mode="json")
        if existing is not None:
            if existing.quarantined_upload_id != upload.id or existing.payload != result_payload:
                raise ValueError("Scanner run ID was already used for a different result")
            return self._view(upload)
        if upload.status not in {
            QuarantineStatus.QUARANTINED.value,
            QuarantineStatus.SCAN_FAILED.value,
        }:
            raise ValueError(f"Upload in {upload.status} cannot accept another scan result")
        previous_status = upload.status
        now = utc_now()
        scan = MalwareScanResultRow(
            id=f"malware-scan-{uuid4()}",
            quarantined_upload_id=upload.id,
            adapter_qualification_id=result.adapter_qualification_id,
            scanner_run_id=result.scanner_run_id,
            scanned_object_hash=result.scanned_object_hash,
            verdict=result.verdict.value,
            definitions_version=result.definitions_version,
            report_hash=result.report_hash,
            payload=result_payload,
            completed_at=ensure_utc(result.completed_at),
            recorded_at=now,
        )
        self.session.add(scan)
        upload.updated_at = now
        upload.failure_detail = None
        if result.verdict is MalwareVerdict.CLEAN:
            upload.status = QuarantineStatus.CLEAN.value
            upload.failure_code = None
            topic = "document.upload.scan-clean"
            failed_findings = list(
                self.session.scalars(
                    select(VerificationFindingRow).where(
                        VerificationFindingRow.project_id == upload.project_id,
                        VerificationFindingRow.code == "MALWARE_SCAN_FAILED",
                        VerificationFindingRow.resolved.is_(False),
                    )
                )
            )
            for finding in failed_findings:
                if finding.payload.get("upload_id") == upload.id:
                    finding.resolved = True
                    finding.payload = {
                        **finding.payload,
                        "superseded_by_scan_result_id": scan.id,
                    }
                    finding.updated_at = now
        elif result.verdict is MalwareVerdict.INFECTED:
            upload.status = QuarantineStatus.REJECTED.value
            upload.failure_code = "MALWARE_DETECTED"
            topic = "document.upload.rejected"
            self._add_blocking_finding(
                upload=upload,
                code="MALWARE_DETECTED",
                payload={
                    "scan_result_id": scan.id,
                    "report_hash": result.report_hash,
                    "detected_threats": list(result.detected_threats),
                },
            )
        else:
            upload.status = QuarantineStatus.SCAN_FAILED.value
            upload.failure_code = "MALWARE_SCAN_FAILED"
            topic = "document.upload.scan-failed"
            self._add_blocking_finding(
                upload=upload,
                code="MALWARE_SCAN_FAILED",
                payload={
                    "scan_result_id": scan.id,
                    "report_hash": result.report_hash,
                },
            )
        project_service = self._project_service()
        event_payload = {
            "upload_id": upload.id,
            "scan_result_id": scan.id,
            "adapter_qualification_id": result.adapter_qualification_id,
            "scanner_run_id": result.scanner_run_id,
            "verdict": result.verdict,
            "report_hash": result.report_hash,
            "object_hash": result.scanned_object_hash,
            "from_status": previous_status,
            "to_status": upload.status,
        }
        project_service.record_event(
            aggregate_type="quarantined_upload",
            aggregate_id=upload.id,
            event_type="malware_scan_result_recorded",
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload=event_payload,
        )
        project_service.record_event(
            aggregate_type="project",
            aggregate_id=project_id,
            event_type="document_upload_scan_state_changed",
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload=event_payload,
        )
        project_service.enqueue_event(
            topic=topic,
            aggregate_id=upload.id,
            payload={
                "project_id": project_id,
                "upload_id": upload.id,
                "object_hash": upload.object_hash,
            },
        )
        self.session.flush()
        return self._view(upload)

    def requeue_processing(
        self,
        *,
        actor: Actor,
        project_id: str,
        upload_id: str,
        request_id: str,
        reason: str,
    ) -> QuarantinedUploadView:
        actor.require_any(ActorRole.ADMIN)
        self._required_text(reason, "reason", 2000)
        project_service = self._project_service()
        project_service.get_project(actor=actor, project_id=project_id, lock=True)
        upload = self.session.scalar(
            select(QuarantinedUploadRow)
            .where(
                QuarantinedUploadRow.id == upload_id,
                QuarantinedUploadRow.project_id == project_id,
                QuarantinedUploadRow.organization_id == actor.organization_id,
            )
            .with_for_update()
        )
        if upload is None:
            raise LookupError(upload_id)
        if upload.status != QuarantineStatus.PROCESSING_DEAD_LETTERED.value:
            raise ValueError("Only a dead-lettered document upload can be requeued")
        clean_scan = self.session.scalar(
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
            raise ValueError("Dead-lettered upload has no exact CLEAN scan evidence")
        prior_dead_letter = self.session.scalar(
            select(OutboxEventRow)
            .where(
                OutboxEventRow.topic == "document.upload.scan-clean",
                OutboxEventRow.aggregate_id == upload.id,
                OutboxEventRow.dead_lettered_at.is_not(None),
            )
            .order_by(OutboxEventRow.created_at.desc(), OutboxEventRow.id.desc())
            .limit(1)
        )
        if prior_dead_letter is None:
            raise RuntimeError("Dead-lettered upload has no terminal outbox evidence")
        now = utc_now()
        prior_attempts = upload.processing_attempts
        prior_failure_code = upload.failure_code
        upload.status = QuarantineStatus.CLEAN.value
        upload.processing_attempts = 0
        upload.processing_dead_lettered_at = None
        upload.failure_code = None
        upload.failure_detail = None
        upload.updated_at = now
        event_payload = {
            "upload_id": upload.id,
            "object_hash": upload.object_hash,
            "clean_scan_result_id": clean_scan.id,
            "prior_outbox_event_id": prior_dead_letter.id,
            "prior_processing_attempts": prior_attempts,
            "prior_failure_code": prior_failure_code,
            "from_status": QuarantineStatus.PROCESSING_DEAD_LETTERED.value,
            "to_status": QuarantineStatus.CLEAN.value,
        }
        project_service.record_event(
            aggregate_type="quarantined_upload",
            aggregate_id=upload.id,
            event_type="document_processing_requeued",
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload=event_payload,
        )
        project_service.record_event(
            aggregate_type="project",
            aggregate_id=upload.project_id,
            event_type="document_processing_requeued",
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload=event_payload,
        )
        project_service.enqueue_event(
            topic="document.upload.scan-clean",
            aggregate_id=upload.id,
            payload={
                "project_id": upload.project_id,
                "upload_id": upload.id,
                "object_hash": upload.object_hash,
                "requeued_from_outbox_event_id": prior_dead_letter.id,
            },
        )
        self.session.flush()
        return self._view(upload)

    def _validate_scan_result(
        self,
        *,
        actor: Actor,
        upload: QuarantinedUploadRow,
        result: MalwareScanResult,
    ) -> None:
        if result.scanned_object_hash != upload.object_hash:
            raise ValueError("Scanner result does not bind the quarantined object hash")
        try:
            report_bytes = canonical_json(result.report)
        except TypeError as error:
            raise ValueError("Scanner report is not canonical auditable data") from error
        if len(report_bytes) > self.settings.max_scan_report_bytes:
            raise ValueError("Scanner report exceeds the configured size limit")
        if content_hash(result.report) != result.report_hash:
            raise ValueError("Scanner report hash does not reproduce the report")
        completed_at = ensure_utc(result.completed_at)
        assert completed_at is not None
        if completed_at > utc_now() + timedelta(minutes=5):
            raise ValueError("Scanner completion time is unreasonably in the future")
        uploaded_at = ensure_utc(upload.created_at)
        assert uploaded_at is not None
        if completed_at < uploaded_at - timedelta(minutes=5):
            raise ValueError("Scanner result predates the quarantined upload")
        if result.verdict is MalwareVerdict.CLEAN and result.detected_threats:
            raise ValueError("CLEAN scanner result cannot contain detected threats")
        if result.verdict is MalwareVerdict.INFECTED and not result.detected_threats:
            raise ValueError("INFECTED scanner result must identify at least one threat")
        if result.adapter_qualification_id != self.settings.malware_scanner_qualification_id:
            raise ValueError("Scanner result is outside the configured qualification")
        qualification = self.session.scalar(
            select(AdapterQualificationRow).where(
                AdapterQualificationRow.id == result.adapter_qualification_id,
                AdapterQualificationRow.adapter_name == self.settings.malware_scanner_adapter,
                AdapterQualificationRow.status == "APPROVED",
            )
        )
        if qualification is None:
            raise ValueError("Malware scanner is not actively qualified")
        if qualification.valid_until and qualification.valid_until < utc_now().date():
            raise ValueError("Malware scanner qualification has expired")
        if qualification.payload.get("organization_id") != actor.organization_id:
            raise ValueError("Malware scanner qualification belongs to another organisation")
        if "MALWARE_SCAN" not in qualification.payload.get("supported_methods", []):
            raise ValueError("Qualification does not authorize malware scanning")

    def _add_blocking_finding(
        self,
        *,
        upload: QuarantinedUploadRow,
        code: str,
        payload: dict[str, object],
    ) -> None:
        now = utc_now()
        finding_id = (
            f"finding-{content_hash({'upload': upload.id, 'code': code, 'payload': payload})[:24]}"
        )
        self.session.add(
            VerificationFindingRow(
                id=finding_id,
                project_id=upload.project_id,
                contour="INPUT_INTEGRITY",
                code=code,
                severity=Severity.BLOCKER.value,
                resolved=False,
                payload={"upload_id": upload.id, **payload},
                created_at=now,
                updated_at=now,
            )
        )

    def _view(self, upload: QuarantinedUploadRow) -> QuarantinedUploadView:
        latest_scan = self.session.scalar(
            select(MalwareScanResultRow)
            .where(MalwareScanResultRow.quarantined_upload_id == upload.id)
            .order_by(MalwareScanResultRow.recorded_at.desc())
            .limit(1)
        )
        result_payload = upload.result_payload or {}
        return QuarantinedUploadView(
            upload_id=upload.id,
            project_id=upload.project_id,
            status=QuarantineStatus(upload.status),
            object_hash=upload.object_hash,
            size_bytes=upload.size_bytes,
            original_filename=upload.original_filename,
            uploaded_by=upload.uploaded_by,
            latest_scan_verdict=(MalwareVerdict(latest_scan.verdict) if latest_scan else None),
            latest_scan_report_hash=latest_scan.report_hash if latest_scan else None,
            processed_document_id=upload.processed_document_id,
            processed_document_revision_id=upload.processed_document_revision_id,
            candidate_document_set_revision_id=(upload.candidate_document_set_revision_id),
            manifest=result_payload.get("manifest"),
            failure_code=upload.failure_code,
            processing_attempts=upload.processing_attempts,
            processing_lease_expires_at=upload.processing_lease_expires_at,
            processing_dead_lettered_at=upload.processing_dead_lettered_at,
            created_at=upload.created_at,
            updated_at=upload.updated_at,
        )

    def _project_service(self) -> ProjectService:
        return ProjectService(
            session=self.session,
            settings=self.settings,
            object_store=self.evidence_store,
        )

    @staticmethod
    def _required_text(value: str, field: str, max_length: int) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{field} must not be blank")
        if len(normalized) > max_length:
            raise ValueError(f"{field} exceeds {max_length} characters")
        return normalized
