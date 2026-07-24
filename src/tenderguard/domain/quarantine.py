from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field

from tenderguard.domain.intake import IntakeManifest
from tenderguard.domain.models import DomainModel


class QuarantineStatus(StrEnum):
    QUARANTINED = "QUARANTINED"
    CLEAN = "CLEAN"
    REJECTED = "REJECTED"
    SCAN_FAILED = "SCAN_FAILED"
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"
    PROCESSING_FAILED = "PROCESSING_FAILED"
    PROCESSING_DEAD_LETTERED = "PROCESSING_DEAD_LETTERED"


class MalwareVerdict(StrEnum):
    CLEAN = "CLEAN"
    INFECTED = "INFECTED"
    ERROR = "ERROR"


class MalwareScanResult(DomainModel):
    scanner_run_id: str = Field(min_length=1, max_length=200)
    adapter_qualification_id: str = Field(min_length=1, max_length=128)
    scanned_object_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    verdict: MalwareVerdict
    definitions_version: str = Field(min_length=1, max_length=200)
    detected_threats: tuple[str, ...] = ()
    report: dict[str, Any]
    report_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    completed_at: datetime


class QuarantinedUploadView(DomainModel):
    upload_id: str
    project_id: str
    status: QuarantineStatus
    object_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    original_filename: str
    uploaded_by: str
    latest_scan_verdict: MalwareVerdict | None = None
    latest_scan_report_hash: str | None = None
    processed_document_id: str | None = None
    processed_document_revision_id: str | None = None
    candidate_document_set_revision_id: str | None = None
    manifest: IntakeManifest | None = None
    failure_code: str | None = None
    processing_attempts: int = Field(ge=0)
    processing_lease_expires_at: datetime | None = None
    processing_dead_lettered_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
