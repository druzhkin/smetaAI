from __future__ import annotations

from typing import BinaryIO

from sqlalchemy import select
from sqlalchemy.orm import Session

from tenderguard.application.projects import ProjectService
from tenderguard.application.quarantine import QuarantineService
from tenderguard.config import Settings
from tenderguard.domain.common import utc_now
from tenderguard.domain.enums import ActorRole
from tenderguard.domain.quarantine import (
    MalwareVerdict,
    QuarantinedUploadView,
    QuarantineStatus,
)
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.intake import inspect_intake_stream
from tenderguard.infrastructure.object_store import ObjectStore
from tenderguard.infrastructure.orm import (
    AdapterQualificationRow,
    MalwareScanResultRow,
    QuarantinedUploadRow,
    VerificationFindingRow,
)


class DocumentProcessingService:
    """Runs only in the separately deployed, resource-restricted intake worker."""

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

    def process(
        self,
        *,
        actor: Actor,
        upload_id: str,
        request_id: str,
        reason: str,
    ) -> QuarantinedUploadView:
        actor.require_any(ActorRole.SYSTEM)
        upload = self.session.scalar(
            select(QuarantinedUploadRow)
            .where(
                QuarantinedUploadRow.id == upload_id,
                QuarantinedUploadRow.organization_id == actor.organization_id,
            )
            .with_for_update()
        )
        if upload is None:
            raise LookupError(upload_id)
        if upload.status == QuarantineStatus.PROCESSED.value:
            return self._quarantine_service().get(
                actor=actor,
                project_id=upload.project_id,
                upload_id=upload.id,
            )
        if upload.status not in {
            QuarantineStatus.CLEAN.value,
            QuarantineStatus.PROCESSING_FAILED.value,
        }:
            raise ValueError("Only a qualified CLEAN upload can be processed")
        qualification = self._qualified_processor(actor)
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
            raise ValueError("CLEAN state has no exact qualified scan evidence")
        self._validate_clean_scan_qualification(actor, clean_scan)

        now = utc_now()
        previous_status = upload.status
        upload.status = QuarantineStatus.PROCESSING.value
        upload.updated_at = now
        self.session.flush()
        project_service = self._project_service()
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
                "from_status": previous_status,
                "to_status": QuarantineStatus.PROCESSING.value,
            },
        )
        try:
            with self.session.begin_nested():
                with self.quarantine_store.open(upload.object_hash) as source:
                    stored = self.evidence_store.put(
                        source,
                        max_bytes=self.settings.max_upload_bytes,
                    )
                if stored.object_hash != upload.object_hash:
                    raise RuntimeError("Evidence promotion changed the object hash")
                member_objects: dict[str, str] = {}

                def store_member(path: str, stream: BinaryIO) -> None:
                    member = self.evidence_store.put(
                        stream,
                        max_bytes=self.settings.max_archive_unpacked_bytes,
                    )
                    member_objects[path] = member.object_hash

                with self.evidence_store.open(stored.object_hash) as source:
                    manifest = inspect_intake_stream(
                        upload.original_filename,
                        source,
                        self.settings,
                        on_member=store_member,
                    )
                result = project_service.register_scanned_document_revision(
                    actor=actor,
                    project_id=upload.project_id,
                    logical_key=upload.logical_key,
                    title=upload.title,
                    document_type=upload.document_type,
                    critical=upload.critical,
                    revision_label=upload.revision_label,
                    filename=upload.original_filename,
                    media_type=upload.declared_media_type,
                    stored=stored,
                    manifest=manifest,
                    member_objects=member_objects,
                    quarantine_upload_id=upload.id,
                    submitted_by=upload.uploaded_by,
                    request_id=request_id,
                    reason=reason,
                    make_candidate_current=upload.make_candidate_current,
                    invalidated_document_set_revision_id=(
                        upload.invalidated_document_set_revision_id
                    ),
                )
                upload.status = QuarantineStatus.PROCESSED.value
                upload.processed_document_id = result.document_id
                upload.processed_document_revision_id = result.document_revision_id
                upload.candidate_document_set_revision_id = (
                    result.candidate_document_set_revision_id
                )
                upload.result_payload = result.model_dump(mode="json")
                upload.failure_code = None
                upload.failure_detail = None
                upload.updated_at = utc_now()
                pending_finding = self.session.get(
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
                }
                pending_finding.updated_at = upload.updated_at
                rejected_predecessors = list(
                    self.session.scalars(
                        select(QuarantinedUploadRow).where(
                            QuarantinedUploadRow.project_id == upload.project_id,
                            QuarantinedUploadRow.logical_key == upload.logical_key,
                            QuarantinedUploadRow.status == QuarantineStatus.REJECTED.value,
                            QuarantinedUploadRow.created_at < upload.created_at,
                        )
                    )
                )
                predecessor_ids = {item.id for item in rejected_predecessors}
                if predecessor_ids:
                    unresolved_findings = list(
                        self.session.scalars(
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
                                "resolved_by_document_revision_id": result.document_revision_id,
                            }
                            finding.updated_at = upload.updated_at
        except Exception as error:
            upload.status = QuarantineStatus.PROCESSING_FAILED.value
            upload.failure_code = "DOCUMENT_PROCESSING_FAILED"
            upload.failure_detail = type(error).__name__
            upload.updated_at = utc_now()
            event_payload = {
                "upload_id": upload.id,
                "object_hash": upload.object_hash,
                "error_type": type(error).__name__,
                "processor_qualification_id": qualification.id,
                "from_status": QuarantineStatus.PROCESSING.value,
                "to_status": QuarantineStatus.PROCESSING_FAILED.value,
            }
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
                    "error_type": type(error).__name__,
                },
            )
            return self._quarantine_service().get(
                actor=actor,
                project_id=upload.project_id,
                upload_id=upload.id,
            )

        event_payload = {
            "upload_id": upload.id,
            "object_hash": upload.object_hash,
            "document_id": upload.processed_document_id,
            "document_revision_id": upload.processed_document_revision_id,
            "candidate_document_set_revision_id": (upload.candidate_document_set_revision_id),
            "malware_scan_result_id": clean_scan.id,
            "processor_qualification_id": qualification.id,
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
        self.session.flush()
        return self._quarantine_service().get(
            actor=actor,
            project_id=upload.project_id,
            upload_id=upload.id,
        )

    def _qualified_processor(self, actor: Actor) -> AdapterQualificationRow:
        qualification = self.session.scalar(
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
        actor: Actor,
        scan: MalwareScanResultRow,
    ) -> None:
        if scan.adapter_qualification_id != self.settings.malware_scanner_qualification_id:
            raise ValueError("CLEAN scan is outside the configured scanner qualification")
        qualification = self.session.scalar(
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

    def _quarantine_service(self) -> QuarantineService:
        return QuarantineService(
            session=self.session,
            settings=self.settings,
            evidence_store=self.evidence_store,
            quarantine_store=self.quarantine_store,
        )

    def _project_service(self) -> ProjectService:
        return ProjectService(
            session=self.session,
            settings=self.settings,
            object_store=self.evidence_store,
        )
