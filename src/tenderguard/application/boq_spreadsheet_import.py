from __future__ import annotations

import hashlib
from datetime import datetime

from pydantic import Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from tenderguard.application.controlled_version_integrity import (
    require_bound_controlled_version,
)
from tenderguard.application.document_set_integrity import (
    require_confirmed_document_set_integrity,
    require_observation_in_document_set,
)
from tenderguard.application.evidence import EvidenceService, ObservationDraft
from tenderguard.application.projects import ProjectService, SystemProjectAccess
from tenderguard.config import Settings
from tenderguard.domain.boq_spreadsheet import (
    BOQ_ROW_VALUE_SCHEMA,
    BoqRowCandidate,
    BoqXlsxExtractionResult,
    BoqXlsxProfile,
    ImportedBoqRowValue,
)
from tenderguard.domain.common import content_hash, ensure_utc, utc_now
from tenderguard.domain.enums import (
    ActorRole,
    ApprovalState,
    EvidenceMethod,
    VerificationStatus,
)
from tenderguard.domain.intake import IntakeManifest
from tenderguard.domain.models import DomainModel, EvidenceLocation
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.boq_spreadsheet import extract_boq_xlsx_candidates
from tenderguard.infrastructure.object_store import ObjectStore
from tenderguard.infrastructure.orm import (
    AdapterQualificationRow,
    DocumentRevisionRow,
    DocumentRow,
    ExtractionRunRow,
    FileManifestRow,
    ObservationRow,
)

BOQ_XLSX_PROFILE_PURPOSE = "boq_xlsx_profile"
BOQ_XLSX_PROFILE_KIND = "boq_xlsx_profile"


class BoqXlsxImportPolicy(DomainModel):
    profile: BoqXlsxProfile
    source_priority: int = Field(ge=0)
    allowed_project_states: tuple[ApprovalState, ...] = Field(min_length=1)
    required_adapter_name: str = Field(min_length=1, max_length=200)
    required_adapter_version: str = Field(min_length=1, max_length=200)

    @field_validator("allowed_project_states")
    @classmethod
    def states_are_unique(
        cls,
        values: tuple[ApprovalState, ...],
    ) -> tuple[ApprovalState, ...]:
        if len(values) != len(set(values)):
            raise ValueError("BoQ XLSX import project states must be unique")
        return values

    @model_validator(mode="after")
    def released_states_are_never_importable(self) -> BoqXlsxImportPolicy:
        forbidden = {
            ApprovalState.APPROVED_FOR_INTERNAL_USE,
            ApprovalState.APPROVED_FOR_BID,
            ApprovalState.SUPERSEDED,
            ApprovalState.ARCHIVED,
        }
        if forbidden.intersection(self.allowed_project_states):
            raise ValueError("Released or historical projects cannot accept BoQ imports")
        return self


class BoqXlsxImportResult(DomainModel):
    extraction_run_id: str = Field(min_length=1, max_length=64)
    project_id: str = Field(min_length=1, max_length=64)
    document_revision_id: str = Field(min_length=1, max_length=64)
    document_set_revision_id: str = Field(min_length=1, max_length=64)
    profile_version_id: str = Field(min_length=1, max_length=64)
    adapter_qualification_id: str = Field(min_length=1, max_length=128)
    workbook_object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observation_ids: tuple[str, ...] = Field(min_length=1)
    status: VerificationStatus = VerificationStatus.UNVERIFIED
    reused_existing_run: bool = False

    @model_validator(mode="after")
    def import_never_verifies_rows(self) -> BoqXlsxImportResult:
        if self.status is not VerificationStatus.UNVERIFIED:
            raise ValueError("BoQ spreadsheet import cannot verify observations")
        if len(self.observation_ids) != len(set(self.observation_ids)):
            raise ValueError("BoQ spreadsheet import observation IDs must be unique")
        return self


class BoqSpreadsheetImportService:
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

    def import_current_workbook(
        self,
        *,
        actor: Actor,
        project_id: str,
        document_revision_id: str,
        adapter_qualification_id: str,
        request_id: str,
        reason: str,
    ) -> BoqXlsxImportResult:
        reason = reason.strip()
        if not reason or len(reason) > 2000:
            raise ValueError("BoQ XLSX import reason must contain 1 to 2000 characters")
        if actor.roles != frozenset({ActorRole.SYSTEM}):
            raise ValueError("BoQ XLSX import requires the isolated SYSTEM worker")
        if not self.settings.boq_xlsx_adapter_configured:
            raise ValueError("BoQ XLSX worker binding is not configured")
        if (
            adapter_qualification_id != self.settings.boq_xlsx_adapter_qualification_id
            or actor.actor_id != self.settings.boq_xlsx_worker_actor_id
        ):
            raise ValueError("BoQ XLSX request differs from the configured worker binding")
        project = ProjectService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        ).get_project(
            actor=actor,
            project_id=project_id,
            lock=True,
            system_access=SystemProjectAccess(
                qualification_id=adapter_qualification_id,
                capability=EvidenceMethod.TABLE_PARSER.value,
            ),
        )
        document_set = require_confirmed_document_set_integrity(
            session=self.session,
            settings=self.settings,
            project_id=project.id,
            document_set_revision_id=project.current_document_set_revision_id,
        )
        require_observation_in_document_set(
            document_revision_ids=document_set.revision_ids,
            document_revision_id=document_revision_id,
        )
        profile_row = require_bound_controlled_version(
            session=self.session,
            settings=self.settings,
            project_id=project.id,
            organization_id=project.organization_id,
            purpose=BOQ_XLSX_PROFILE_PURPOSE,
            kind=BOQ_XLSX_PROFILE_KIND,
        )
        policy = self._policy(profile_row.payload)
        if policy.profile.profile_version_id != profile_row.id:
            raise ValueError("BoQ XLSX profile identity does not match its controlled version")
        if policy.profile.expected_workbook_sha256 is None:
            raise ValueError("Governed BoQ XLSX profile must pin the workbook SHA-256")
        if ApprovalState(project.state) not in policy.allowed_project_states:
            raise ValueError("BoQ XLSX import is not allowed in the current project state")
        qualification = self._qualification(
            actor=actor,
            qualification_id=adapter_qualification_id,
            policy=policy,
        )
        document, revision = self._document_revision(
            project_id=project.id,
            document_revision_id=document_revision_id,
        )
        if revision.corrupt or revision.protected:
            raise ValueError("BoQ source document revision is corrupt or protected")
        manifest = IntakeManifest.model_validate(revision.inspection_payload)
        workbook_entry = self._workbook_entry(
            revision_id=revision.id,
            workbook_sha256=policy.profile.expected_workbook_sha256,
        )
        workbook_content = self._read_workbook(workbook_entry)
        revision_created_at = ensure_utc(revision.created_at)
        if revision_created_at is None:
            raise RuntimeError("BoQ source document revision timestamp is missing")
        extraction = extract_boq_xlsx_candidates(
            workbook_content=workbook_content,
            workbook_archive_path=workbook_entry.archive_path,
            manifest=manifest,
            profile=policy.profile,
            extracted_at=revision_created_at,
        )
        self._require_importable_extraction(extraction)
        drafts = tuple(
            self._observation_draft(
                candidate=candidate,
                extraction=extraction,
                policy=policy,
                qualification=qualification,
                document=document,
                revision=revision,
                observed_at=revision_created_at,
            )
            for candidate in extraction.candidates
        )
        run_identity = {
            "project_id": project.id,
            "document_revision_id": revision.id,
            "document_set_revision_id": document_set.id,
            "workbook_object_sha256": extraction.workbook_object_sha256,
            "profile_version_id": profile_row.id,
            "profile_content_hash": profile_row.content_hash,
            "adapter_qualification_id": qualification.id,
            "adapter_version": qualification.adapter_version,
        }
        run_id = f"extraction-run-{content_hash(run_identity)[:24]}"
        observations = tuple(
            EvidenceService(
                session=self.session,
                settings=self.settings,
                object_store=self.object_store,
            ).record_observation(
                actor=actor,
                project_id=project.id,
                draft=draft,
                request_id=request_id,
                reason=reason,
            )
            for draft in drafts
        )
        observation_ids = tuple(item.observation_id for item in observations)
        expected_payload = {
            **run_identity,
            "extraction_result_hash": content_hash(extraction),
            "observation_ids": list(observation_ids),
            "row_count": len(observation_ids),
        }
        existing = self.session.get(ExtractionRunRow, run_id)
        if existing is not None:
            self._require_existing_run(
                existing=existing,
                project_id=project.id,
                revision_id=revision.id,
                qualification_id=qualification.id,
                expected_payload=expected_payload,
                observation_ids=observation_ids,
            )
            return self._result(
                run_id=run_id,
                project_id=project.id,
                revision_id=revision.id,
                document_set_id=document_set.id,
                profile_version_id=profile_row.id,
                qualification_id=qualification.id,
                workbook_sha256=extraction.workbook_object_sha256,
                observation_ids=observation_ids,
                reused=True,
            )

        now = utc_now()
        self.session.add(
            ExtractionRunRow(
                id=run_id,
                project_id=project.id,
                document_revision_id=revision.id,
                adapter_qualification_id=qualification.id,
                method=EvidenceMethod.TABLE_PARSER.value,
                status="COMPLETED",
                payload=expected_payload,
                created_at=now,
                completed_at=now,
            )
        )
        ProjectService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        ).record_event(
            aggregate_type="project",
            aggregate_id=project.id,
            event_type="boq_xlsx_rows_imported",
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload={
                "extraction_run_id": run_id,
                "document_revision_id": revision.id,
                "document_set_revision_id": document_set.id,
                "workbook_object_sha256": extraction.workbook_object_sha256,
                "profile_version_id": profile_row.id,
                "profile_content_hash": profile_row.content_hash,
                "adapter_qualification_id": qualification.id,
                "observation_ids": list(observation_ids),
                "status": VerificationStatus.UNVERIFIED.value,
            },
        )
        return self._result(
            run_id=run_id,
            project_id=project.id,
            revision_id=revision.id,
            document_set_id=document_set.id,
            profile_version_id=profile_row.id,
            qualification_id=qualification.id,
            workbook_sha256=extraction.workbook_object_sha256,
            observation_ids=observation_ids,
            reused=False,
        )

    @staticmethod
    def _policy(payload: dict[str, object]) -> BoqXlsxImportPolicy:
        return BoqXlsxImportPolicy.model_validate(
            {
                "profile": payload.get("profile"),
                "source_priority": payload.get("source_priority"),
                "allowed_project_states": payload.get("allowed_project_states"),
                "required_adapter_name": payload.get("required_adapter_name"),
                "required_adapter_version": payload.get("required_adapter_version"),
            }
        )

    def _qualification(
        self,
        *,
        actor: Actor,
        qualification_id: str,
        policy: BoqXlsxImportPolicy,
    ) -> AdapterQualificationRow:
        qualification = self.session.scalar(
            select(AdapterQualificationRow).where(
                AdapterQualificationRow.id == qualification_id,
                AdapterQualificationRow.status == "APPROVED",
                AdapterQualificationRow.adapter_name == policy.required_adapter_name,
                AdapterQualificationRow.adapter_version == policy.required_adapter_version,
            )
        )
        if qualification is None:
            raise ValueError("BoQ XLSX extractor qualification does not match the profile")
        if (
            qualification.id != self.settings.boq_xlsx_adapter_qualification_id
            or qualification.adapter_name != self.settings.boq_xlsx_adapter
            or actor.actor_id != self.settings.boq_xlsx_worker_actor_id
        ):
            raise ValueError("BoQ XLSX extractor differs from the configured worker binding")
        if qualification.valid_until and qualification.valid_until < utc_now().date():
            raise ValueError("BoQ XLSX extractor qualification has expired")
        if (
            qualification.payload.get("organization_id") != actor.organization_id
            or qualification.payload.get("service_actor_id") != actor.actor_id
            or EvidenceMethod.TABLE_PARSER.value
            not in qualification.payload.get("supported_methods", [])
        ):
            raise ValueError("BoQ XLSX extractor service identity is not qualified")
        return qualification

    def _document_revision(
        self,
        *,
        project_id: str,
        document_revision_id: str,
    ) -> tuple[DocumentRow, DocumentRevisionRow]:
        row = self.session.execute(
            select(DocumentRow, DocumentRevisionRow)
            .join(
                DocumentRevisionRow,
                DocumentRevisionRow.document_id == DocumentRow.id,
            )
            .where(
                DocumentRow.project_id == project_id,
                DocumentRevisionRow.id == document_revision_id,
            )
        ).one_or_none()
        if row is None:
            raise ValueError("BoQ source revision is not part of the project")
        return row[0], row[1]

    def _workbook_entry(
        self,
        *,
        revision_id: str,
        workbook_sha256: str,
    ) -> FileManifestRow:
        rows = tuple(
            self.session.scalars(
                select(FileManifestRow).where(
                    FileManifestRow.document_revision_id == revision_id,
                    FileManifestRow.object_hash == workbook_sha256,
                )
            )
        )
        if len(rows) != 1:
            raise ValueError("BoQ workbook manifest entry is missing or ambiguous")
        row = rows[0]
        if (
            row.corrupt
            or row.protected
            or row.media_type != "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ):
            raise ValueError("BoQ workbook manifest entry is not a processable XLSX")
        return row

    def _read_workbook(self, entry: FileManifestRow) -> bytes:
        if entry.size_bytes <= 0 or entry.size_bytes > self.settings.max_upload_bytes:
            raise ValueError("BoQ workbook size is outside the configured limit")
        with self.object_store.open(entry.object_hash) as stream:
            content = stream.read(entry.size_bytes + 1)
        if len(content) != entry.size_bytes:
            raise RuntimeError("BoQ workbook object size does not match its manifest")
        if content_hash_bytes(content) != entry.object_hash:
            raise RuntimeError("BoQ workbook object hash does not match its manifest")
        return content

    @staticmethod
    def _require_importable_extraction(
        extraction: BoqXlsxExtractionResult,
    ) -> None:
        if (
            extraction.status != "UNVERIFIED"
            or extraction.global_blockers
            or not extraction.candidates
            or any(candidate.blockers for candidate in extraction.candidates)
        ):
            raise ValueError("BoQ XLSX extraction is BLOCKED and cannot create observations")

    @staticmethod
    def _observation_draft(
        *,
        candidate: BoqRowCandidate,
        extraction: BoqXlsxExtractionResult,
        policy: BoqXlsxImportPolicy,
        qualification: AdapterQualificationRow,
        document: DocumentRow,
        revision: DocumentRevisionRow,
        observed_at: datetime,
    ) -> ObservationDraft:
        if (
            candidate.source_position_id is None
            or candidate.description is None
            or candidate.unit is None
            or candidate.quantity is None
        ):
            raise RuntimeError("Importable BoQ candidate lacks a required value")
        source_item_identity = {
            "document_revision_id": revision.id,
            "workbook_object_sha256": extraction.workbook_object_sha256,
            "worksheet_name": candidate.worksheet_name,
            "source_position_id": candidate.source_position_id,
            "profile_version_id": extraction.profile_version_id,
        }
        source_item_id = f"boq-source-{content_hash(source_item_identity)[:24]}"
        value = ImportedBoqRowValue(
            schema_version=BOQ_ROW_VALUE_SCHEMA,
            source_item_id=source_item_id,
            source_position_id=candidate.source_position_id,
            description=candidate.description,
            specification=candidate.specification,
            source_reference=candidate.source_reference,
            unit=candidate.unit,
            quantity=candidate.quantity,
            cells=candidate.cells,
            worksheet_name=candidate.worksheet_name,
            row_number=candidate.row_number,
            archive_path=extraction.archive_path,
            workbook_object_sha256=extraction.workbook_object_sha256,
            workbook_profile_version_id=extraction.profile_version_id,
            workbook_profile_content_hash=extraction.profile_content_hash,
        )
        cell_coordinates = ",".join(sorted(cell.coordinate for cell in candidate.cells.values()))
        return ObservationDraft(
            field_name=f"boq_row_candidate:{source_item_id}",
            value=value.model_dump(mode="json"),
            unit=candidate.unit,
            method=EvidenceMethod.TABLE_PARSER,
            method_version=qualification.adapter_version,
            source_priority=policy.source_priority,
            location=EvidenceLocation(
                document_id=document.id,
                document_revision_id=revision.id,
                original_object_hash=revision.object_hash,
                locator_kind="XLSX_ROW",
                locator=(
                    f"{extraction.archive_path}::{candidate.worksheet_name}::{cell_coordinates}"
                ),
                sheet=candidate.worksheet_name,
                cell_or_range=cell_coordinates,
            ),
            observed_at=observed_at,
            confidence=None,
            adapter_qualification_id=qualification.id,
        )

    def _require_existing_run(
        self,
        *,
        existing: ExtractionRunRow,
        project_id: str,
        revision_id: str,
        qualification_id: str,
        expected_payload: dict[str, object],
        observation_ids: tuple[str, ...],
    ) -> None:
        if (
            existing.project_id != project_id
            or existing.document_revision_id != revision_id
            or existing.adapter_qualification_id != qualification_id
            or existing.method != EvidenceMethod.TABLE_PARSER.value
            or existing.status != "COMPLETED"
            or existing.payload != expected_payload
            or ensure_utc(existing.completed_at) is None
        ):
            raise RuntimeError("Existing BoQ XLSX extraction run fails integrity replay")
        rows = tuple(
            self.session.scalars(
                select(ObservationRow).where(ObservationRow.id.in_(observation_ids))
            )
        )
        if (
            len(rows) != len(observation_ids)
            or {row.id for row in rows} != set(observation_ids)
            or any(
                row.project_id != project_id
                or row.document_revision_id != revision_id
                or row.method != EvidenceMethod.TABLE_PARSER.value
                or row.status != VerificationStatus.UNVERIFIED.value
                for row in rows
            )
        ):
            raise RuntimeError("Existing BoQ XLSX observations fail integrity replay")

    @staticmethod
    def _result(
        *,
        run_id: str,
        project_id: str,
        revision_id: str,
        document_set_id: str,
        profile_version_id: str,
        qualification_id: str,
        workbook_sha256: str,
        observation_ids: tuple[str, ...],
        reused: bool,
    ) -> BoqXlsxImportResult:
        return BoqXlsxImportResult(
            extraction_run_id=run_id,
            project_id=project_id,
            document_revision_id=revision_id,
            document_set_revision_id=document_set_id,
            profile_version_id=profile_version_id,
            adapter_qualification_id=qualification_id,
            workbook_object_sha256=workbook_sha256,
            observation_ids=observation_ids,
            reused_existing_run=reused,
        )


def content_hash_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
