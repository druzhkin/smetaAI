from __future__ import annotations

import json
import re
from datetime import datetime
from io import BytesIO
from typing import Any, cast
from uuid import uuid4

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from tenderguard.application.lineage import LineageService
from tenderguard.application.projects import ProjectService
from tenderguard.application.snapshot_integrity import read_verified_snapshot
from tenderguard.config import Settings
from tenderguard.domain.audit import AuditEvent, verify_chain
from tenderguard.domain.common import (
    canonical_data,
    canonical_json,
    content_hash,
    ensure_utc,
    utc_now,
)
from tenderguard.domain.enums import ActorRole, ApprovalState, VersionStatus
from tenderguard.domain.exports import (
    EXPORT_FORMAT,
    EXPORT_MEDIA_TYPE,
    EXPORT_SCHEMA_VERSION,
    ExportManifest,
    ExportSigningMaterial,
    SignedExportPackage,
    build_content_entries,
    build_signed_export_package,
    load_signing_material,
    verify_signed_export_package,
)
from tenderguard.domain.models import DomainModel
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.object_store import ObjectStore
from tenderguard.infrastructure.orm import (
    ApprovalRecordRow,
    ApprovalTaskRow,
    AuditEventRow,
    CalculationSnapshotRow,
    ControlledVersionRow,
    ExportArtifactRow,
    OutboxEventRow,
    ProjectControlledVersionRow,
    ReleaseDecisionRow,
    WorkflowTransitionRow,
)


class ExportIntegrityError(RuntimeError):
    pass


class ExportArtifactView(DomainModel):
    artifact_id: str
    project_id: str
    snapshot_id: str
    release_decision_id: str
    template_version_id: str
    format: str
    media_type: str
    filename: str
    object_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    signature_algorithm: str
    signing_key_id: str
    public_key_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_by: str
    created_at: datetime


class ExportVerificationResult(DomainModel):
    artifact: ExportArtifactView
    valid: bool
    manifest: ExportManifest


class ExportPackageService:
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

    def generate(
        self,
        *,
        actor: Actor,
        project_id: str,
        snapshot_id: str,
        request_id: str,
        reason: str,
    ) -> ExportArtifactView:
        project_service = ProjectService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        )
        project = project_service.get_project(
            actor=actor,
            project_id=project_id,
            lock=True,
            required_roles=(ActorRole.APPROVER,),
        )
        if ApprovalState(project.state) not in {
            ApprovalState.APPROVED_FOR_INTERNAL_USE,
            ApprovalState.APPROVED_FOR_BID,
        }:
            raise ValueError("A signed export requires an approved release state")
        snapshot = self.session.scalar(
            select(CalculationSnapshotRow).where(
                CalculationSnapshotRow.id == snapshot_id,
                CalculationSnapshotRow.project_id == project.id,
            )
        )
        if snapshot is None:
            raise LookupError(snapshot_id)
        if not snapshot.fixed:
            raise ValueError("A signed export requires a fixed calculation snapshot")
        if snapshot.document_set_revision_id != project.current_document_set_revision_id:
            raise ValueError("The calculation snapshot is stale for the current document set")
        try:
            snapshot_payload = read_verified_snapshot(
                object_store=self.object_store,
                snapshot=snapshot,
            )
        except RuntimeError as error:
            raise ExportIntegrityError(
                "Calculation snapshot integrity verification failed"
            ) from error
        template = self._export_template(project.id)
        self._validate_template(template)
        controlled_versions = self._controlled_version_contents(
            project.id,
            snapshot_payload,
        )
        release = self.session.scalar(
            select(ReleaseDecisionRow)
            .where(
                ReleaseDecisionRow.project_id == project.id,
                ReleaseDecisionRow.snapshot_id == snapshot.id,
                ReleaseDecisionRow.allowed.is_(True),
                ReleaseDecisionRow.resulting_state == project.state,
            )
            .order_by(ReleaseDecisionRow.decided_at.desc(), ReleaseDecisionRow.id.desc())
            .limit(1)
        )
        if release is None:
            raise ValueError("No allowed release decision exists for this snapshot and state")
        existing = self.session.scalar(
            select(ExportArtifactRow).where(
                ExportArtifactRow.project_id == project.id,
                ExportArtifactRow.snapshot_id == snapshot.id,
                ExportArtifactRow.release_decision_id == release.id,
                ExportArtifactRow.template_version_id == template.id,
                ExportArtifactRow.format == EXPORT_FORMAT,
            )
        )
        if existing is not None:
            self.verify(
                actor=actor,
                project_id=project.id,
                artifact_id=existing.id,
            )
            return self._view(existing)

        signing_material = self._signing_material()
        audit_events, cutoff = self._audit_chain(project.id, release.id)
        try:
            lineage = LineageService(
                session=self.session,
                settings=self.settings,
                object_store=self.object_store,
            ).snapshot_lineage(
                actor=actor,
                project_id=project.id,
                snapshot_id=snapshot.id,
            )
        except RuntimeError as error:
            raise ExportIntegrityError(
                "Calculation snapshot lineage verification failed"
            ) from error
        approvals = self._approval_contents(project.id, cutoff)
        workflow = self._workflow_contents(project.id, cutoff)
        contents: dict[str, Any] = {
            "approvals.json": approvals,
            "audit_chain.json": [event.model_dump(mode="json") for event in audit_events],
            "controlled_versions.json": controlled_versions,
            "lineage.json": lineage.model_dump(mode="json"),
            "project.json": canonical_data(
                {
                    "project_id": project.id,
                    "organization_id": project.organization_id,
                    "code": project.code,
                    "name": project.name,
                    "state": project.state,
                    "current_document_set_revision_id": project.current_document_set_revision_id,
                }
            ),
            "release_decision.json": canonical_data(
                {
                    "id": release.id,
                    "snapshot_id": release.snapshot_id,
                    "requested_state": release.requested_state,
                    "resulting_state": release.resulting_state,
                    "allowed": release.allowed,
                    "payload": release.payload,
                    "decided_by": release.decided_by,
                    "decided_at": self._required_utc(
                        release.decided_at,
                        "Release decision",
                    ),
                }
            ),
            "snapshot.json": snapshot_payload,
            "workflow.json": workflow,
        }
        manifest = ExportManifest(
            schema_version=EXPORT_SCHEMA_VERSION,
            format=EXPORT_FORMAT,
            project_id=project.id,
            organization_id=project.organization_id,
            project_code=project.code,
            snapshot_id=snapshot.id,
            snapshot_hash=snapshot.snapshot_hash,
            document_set_revision_id=snapshot.document_set_revision_id,
            release_decision_id=release.id,
            release_state=project.state,
            template_version_id=template.id,
            audit_cutoff_event_hash=audit_events[-1].event_hash,
            content_entries=build_content_entries(contents),
        )
        package = build_signed_export_package(
            manifest=manifest,
            contents=contents,
            signing_material=signing_material,
        )
        package_bytes = canonical_json(package)
        stored = self.object_store.put(BytesIO(package_bytes))
        filename = self._filename(project.code, snapshot.id)
        now = utc_now()
        artifact = ExportArtifactRow(
            id=f"export-{stored.object_hash[:24]}",
            project_id=project.id,
            snapshot_id=snapshot.id,
            adapter_qualification_id=None,
            release_decision_id=release.id,
            template_version_id=template.id,
            package_schema_version=EXPORT_SCHEMA_VERSION,
            format=EXPORT_FORMAT,
            media_type=EXPORT_MEDIA_TYPE,
            filename=filename,
            object_hash=stored.object_hash,
            object_key=stored.object_key,
            size_bytes=stored.size_bytes,
            manifest_hash=package.signature.manifest_hash,
            signature_algorithm=package.signature.algorithm,
            signature=package.signature.value_b64,
            signing_key_id=package.signature.key_id,
            signing_public_key_b64=package.signature.public_key_b64,
            public_key_fingerprint=package.signature.public_key_fingerprint,
            payload={
                "audit_cutoff_event_hash": package.manifest.audit_cutoff_event_hash,
                "content_entries": [
                    entry.model_dump(mode="json") for entry in package.manifest.content_entries
                ],
            },
            created_by=actor.actor_id,
            created_at=now,
        )
        self.session.add(artifact)
        self.session.flush()
        project_service.record_event(
            aggregate_type="project",
            aggregate_id=project.id,
            event_type="signed_export_package_generated",
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload={
                "artifact_id": artifact.id,
                "snapshot_id": snapshot.id,
                "release_decision_id": release.id,
                "template_version_id": template.id,
                "object_hash": stored.object_hash,
                "manifest_hash": package.signature.manifest_hash,
                "signing_key_id": package.signature.key_id,
                "public_key_fingerprint": package.signature.public_key_fingerprint,
            },
        )
        self.session.add(
            OutboxEventRow(
                id=f"outbox-{uuid4()}",
                deduplication_key=f"export-artifact:{artifact.id}",
                topic="export.package.generated",
                aggregate_id=artifact.id,
                payload={
                    "project_id": project.id,
                    "artifact_id": artifact.id,
                    "snapshot_id": snapshot.id,
                    "format": EXPORT_FORMAT,
                    "object_hash": stored.object_hash,
                },
                attempts=0,
                available_at=now,
                created_at=now,
            )
        )
        return self._view(artifact)

    def get(
        self,
        *,
        actor: Actor,
        project_id: str,
        artifact_id: str,
    ) -> ExportArtifactView:
        row = self._artifact(actor=actor, project_id=project_id, artifact_id=artifact_id)
        return self._view(row)

    def verify(
        self,
        *,
        actor: Actor,
        project_id: str,
        artifact_id: str,
    ) -> ExportVerificationResult:
        row = self._artifact(actor=actor, project_id=project_id, artifact_id=artifact_id)
        try:
            with self.object_store.open(row.object_hash) as stream:
                package_bytes = stream.read()
        except RuntimeError as error:
            raise ExportIntegrityError(
                "Signed export content-addressed object failed verification"
            ) from error
        if len(package_bytes) != row.size_bytes:
            raise ExportIntegrityError(
                "Signed export package size does not match its database record"
            )
        try:
            raw_package = json.loads(package_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ExportIntegrityError("Signed export package is not valid UTF-8 JSON") from error
        try:
            package = SignedExportPackage.model_validate(raw_package)
            verify_signed_export_package(
                package,
                trusted_public_key_b64=row.signing_public_key_b64,
                trusted_key_id=row.signing_key_id,
            )
        except ValueError as error:
            raise ExportIntegrityError("Signed export cryptographic verification failed") from error
        if (
            package.manifest.project_id != row.project_id
            or package.manifest.snapshot_id != row.snapshot_id
            or package.manifest.release_decision_id != row.release_decision_id
            or package.manifest.template_version_id != row.template_version_id
            or package.manifest.format != row.format
            or package.manifest.schema_version != row.package_schema_version
            or package.signature.manifest_hash != row.manifest_hash
            or package.signature.algorithm != row.signature_algorithm
            or package.signature.value_b64 != row.signature
            or package.signature.public_key_fingerprint != row.public_key_fingerprint
        ):
            raise ExportIntegrityError(
                "Signed export package metadata does not match its database record"
            )
        snapshot = self.session.scalar(
            select(CalculationSnapshotRow).where(
                CalculationSnapshotRow.id == row.snapshot_id,
                CalculationSnapshotRow.project_id == row.project_id,
            )
        )
        if snapshot is None:
            raise ExportIntegrityError("Signed export package snapshot record is missing")
        try:
            verified_snapshot = read_verified_snapshot(
                object_store=self.object_store,
                snapshot=snapshot,
            )
        except RuntimeError as error:
            raise ExportIntegrityError(
                "Signed export source snapshot failed integrity verification"
            ) from error
        if content_hash(verified_snapshot) != content_hash(package.contents["snapshot.json"]):
            raise ExportIntegrityError("Signed export embeds a different calculation snapshot")
        release = self.session.scalar(
            select(ReleaseDecisionRow).where(
                ReleaseDecisionRow.id == row.release_decision_id,
                ReleaseDecisionRow.project_id == row.project_id,
                ReleaseDecisionRow.snapshot_id == row.snapshot_id,
                ReleaseDecisionRow.allowed.is_(True),
            )
        )
        if release is None:
            raise ExportIntegrityError(
                "Signed export release decision is missing or was not allowed"
            )
        expected_release_content = canonical_data(
            {
                "id": release.id,
                "snapshot_id": release.snapshot_id,
                "requested_state": release.requested_state,
                "resulting_state": release.resulting_state,
                "allowed": release.allowed,
                "payload": release.payload,
                "decided_by": release.decided_by,
                "decided_at": self._required_utc(
                    release.decided_at,
                    "Release decision",
                ),
            }
        )
        if content_hash(expected_release_content) != content_hash(
            package.contents["release_decision.json"]
        ):
            raise ExportIntegrityError(
                "Signed export release decision differs from its source record"
            )
        self._verify_packaged_controlled_versions(package.contents["controlled_versions.json"])
        audit_events = [
            AuditEvent.model_validate(item) for item in package.contents["audit_chain.json"]
        ]
        if not audit_events or not verify_chain(
            audit_events,
            self.settings.audit_verification_keyring,
        ):
            raise ExportIntegrityError("Signed export audit chain verification failed")
        if audit_events[-1].event_hash != package.manifest.audit_cutoff_event_hash:
            raise ExportIntegrityError("Signed export audit cutoff does not match its audit chain")
        return ExportVerificationResult(
            artifact=self._view(row),
            valid=True,
            manifest=package.manifest,
        )

    def content(
        self,
        *,
        actor: Actor,
        project_id: str,
        artifact_id: str,
    ) -> tuple[ExportArtifactView, bytes]:
        verification = self.verify(
            actor=actor,
            project_id=project_id,
            artifact_id=artifact_id,
        )
        try:
            with self.object_store.open(verification.artifact.object_hash) as stream:
                return verification.artifact, stream.read()
        except RuntimeError as error:
            raise ExportIntegrityError("Signed export changed after verification") from error

    def _artifact(
        self,
        *,
        actor: Actor,
        project_id: str,
        artifact_id: str,
    ) -> ExportArtifactRow:
        required_roles = (
            ActorRole.ESTIMATOR,
            ActorRole.PROCUREMENT,
            ActorRole.TECHNICAL_EXPERT,
            ActorRole.REVIEWER,
            ActorRole.APPROVER,
            ActorRole.METHODOLOGY_OWNER,
            ActorRole.AUDITOR,
        )
        ProjectService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        ).get_project(
            actor=actor,
            project_id=project_id,
            required_roles=required_roles,
        )
        row = self.session.scalar(
            select(ExportArtifactRow).where(
                ExportArtifactRow.id == artifact_id,
                ExportArtifactRow.project_id == project_id,
                ExportArtifactRow.signature_algorithm == "Ed25519",
            )
        )
        if row is None:
            raise LookupError(artifact_id)
        return row

    def _export_template(self, project_id: str) -> ControlledVersionRow:
        row = self.session.scalar(
            select(ControlledVersionRow)
            .join(
                ProjectControlledVersionRow,
                ProjectControlledVersionRow.controlled_version_id == ControlledVersionRow.id,
            )
            .where(
                ProjectControlledVersionRow.project_id == project_id,
                ProjectControlledVersionRow.purpose == "export_template",
                ControlledVersionRow.kind == "export_template",
                ControlledVersionRow.status == VersionStatus.APPROVED.value,
            )
        )
        if row is None:
            raise ValueError("An approved export_template version must be bound")
        return row

    @staticmethod
    def _validate_template(template: ControlledVersionRow) -> None:
        expected_hash = content_hash(
            {
                "kind": template.kind,
                "version_label": template.version_label,
                "payload": template.payload,
            }
        )
        if template.content_hash != expected_hash:
            raise ExportIntegrityError("Export template controlled-version hash does not match")
        if (
            template.payload.get("schema_version") != EXPORT_SCHEMA_VERSION
            or template.payload.get("format") != EXPORT_FORMAT
        ):
            raise ValueError("Export template does not declare the mandatory signed format")

    def _controlled_version_contents(
        self,
        project_id: str,
        snapshot_payload: dict[str, Any],
    ) -> list[dict[str, Any]]:
        raw_versions = snapshot_payload.get("controlled_versions")
        if not isinstance(raw_versions, list) or not raw_versions:
            raise ExportIntegrityError("Calculation snapshot has no controlled-version set")
        snapshot_versions: dict[str, tuple[str, str]] = {}
        for item in raw_versions:
            if not isinstance(item, dict):
                raise ExportIntegrityError(
                    "Calculation snapshot controlled-version entry is invalid"
                )
            version_id = item.get("version_id")
            kind = item.get("kind")
            digest = item.get("content_hash")
            if (
                not isinstance(version_id, str)
                or not version_id
                or not isinstance(kind, str)
                or not kind
                or not isinstance(digest, str)
                or not digest
            ):
                raise ExportIntegrityError(
                    "Calculation snapshot controlled-version entry is incomplete"
                )
            if version_id in snapshot_versions:
                raise ExportIntegrityError("Calculation snapshot repeats a controlled version")
            snapshot_versions[version_id] = (kind, digest)
        bound_rows = list(
            self.session.scalars(
                select(ControlledVersionRow)
                .join(
                    ProjectControlledVersionRow,
                    ProjectControlledVersionRow.controlled_version_id == ControlledVersionRow.id,
                )
                .where(ProjectControlledVersionRow.project_id == project_id)
                .order_by(ControlledVersionRow.kind, ControlledVersionRow.id)
            )
        )
        bound_set = {row.id: (row.kind, row.content_hash) for row in bound_rows}
        if bound_set != snapshot_versions:
            raise ValueError("Calculation snapshot controlled versions are stale")
        result: list[dict[str, Any]] = []
        for row in bound_rows:
            expected_hash = content_hash(
                {
                    "kind": row.kind,
                    "version_label": row.version_label,
                    "payload": row.payload,
                }
            )
            if row.content_hash != expected_hash or row.status != VersionStatus.APPROVED.value:
                raise ExportIntegrityError(
                    f"Controlled version {row.id} failed integrity or approval"
                )
            result.append(
                canonical_data(
                    {
                        "id": row.id,
                        "kind": row.kind,
                        "version_label": row.version_label,
                        "content_hash": row.content_hash,
                        "status": row.status,
                        "payload": row.payload,
                        "approved_by": row.approved_by,
                        "approved_at": (
                            self._required_utc(
                                row.approved_at,
                                f"Controlled version {row.id}",
                            )
                            if row.approved_at is not None
                            else None
                        ),
                    }
                )
            )
        return result

    def _audit_chain(
        self,
        project_id: str,
        release_decision_id: str,
    ) -> tuple[list[AuditEvent], AuditEvent]:
        rows = list(
            self.session.scalars(
                select(AuditEventRow)
                .where(
                    AuditEventRow.aggregate_type == "project",
                    AuditEventRow.aggregate_id == project_id,
                )
                .order_by(AuditEventRow.sequence)
            )
        )
        events = [self._audit_event(row) for row in rows]
        cutoff_index = next(
            (
                index
                for index, event in enumerate(events)
                if event.payload.get("decision_id") == release_decision_id
                and event.event_type in {"bid_release_decided", "internal_release_decided"}
            ),
            None,
        )
        if cutoff_index is None:
            raise ExportIntegrityError("Release decision has no matching audit event")
        chain = events[: cutoff_index + 1]
        if not verify_chain(
            chain,
            self.settings.audit_verification_keyring,
        ):
            raise ExportIntegrityError("Project audit chain failed verification")
        return chain, chain[-1]

    def _verify_packaged_controlled_versions(self, packaged: Any) -> None:
        if not isinstance(packaged, list) or not packaged:
            raise ExportIntegrityError("Signed export controlled-version content is invalid")
        version_ids: list[str] = []
        for item in packaged:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise ExportIntegrityError("Signed export controlled-version entry is invalid")
            version_ids.append(item["id"])
        if len(version_ids) != len(set(version_ids)):
            raise ExportIntegrityError("Signed export repeats a controlled-version entry")
        rows = list(
            self.session.scalars(
                select(ControlledVersionRow)
                .where(ControlledVersionRow.id.in_(version_ids))
                .order_by(ControlledVersionRow.kind, ControlledVersionRow.id)
            )
        )
        if len(rows) != len(version_ids):
            raise ExportIntegrityError("Signed export controlled-version source record is missing")
        expected: list[dict[str, Any]] = []
        for row in rows:
            expected_hash = content_hash(
                {
                    "kind": row.kind,
                    "version_label": row.version_label,
                    "payload": row.payload,
                }
            )
            if row.content_hash != expected_hash or row.status != VersionStatus.APPROVED.value:
                raise ExportIntegrityError(
                    f"Signed export controlled version {row.id} failed source integrity"
                )
            expected.append(
                cast(
                    dict[str, Any],
                    canonical_data(
                        {
                            "id": row.id,
                            "kind": row.kind,
                            "version_label": row.version_label,
                            "content_hash": row.content_hash,
                            "status": row.status,
                            "payload": row.payload,
                            "approved_by": row.approved_by,
                            "approved_at": (
                                self._required_utc(
                                    row.approved_at,
                                    f"Controlled version {row.id}",
                                )
                                if row.approved_at is not None
                                else None
                            ),
                        }
                    ),
                )
            )
        if content_hash(expected) != content_hash(packaged):
            raise ExportIntegrityError(
                "Signed export controlled versions differ from their source records"
            )

    def _approval_contents(
        self,
        project_id: str,
        cutoff: AuditEvent,
    ) -> dict[str, Any]:
        tasks = list(
            self.session.scalars(
                select(ApprovalTaskRow)
                .where(
                    ApprovalTaskRow.project_id == project_id,
                    ApprovalTaskRow.created_at <= cutoff.occurred_at,
                )
                .order_by(ApprovalTaskRow.id)
            )
        )
        for task in tasks:
            updated_at = ensure_utc(task.updated_at)
            if updated_at is None:
                raise ExportIntegrityError("Approval task has no update timestamp")
            if updated_at > cutoff.occurred_at:
                raise ValueError("Approval tasks changed after release; a new release is required")
        task_ids = [task.id for task in tasks]
        records = (
            list(
                self.session.scalars(
                    select(ApprovalRecordRow)
                    .where(
                        ApprovalRecordRow.task_id.in_(task_ids),
                        ApprovalRecordRow.decided_at <= cutoff.occurred_at,
                    )
                    .order_by(ApprovalRecordRow.id)
                )
            )
            if task_ids
            else []
        )
        return cast(
            dict[str, Any],
            canonical_data(
                {
                    "tasks": [
                        {
                            "id": task.id,
                            "task_type": task.task_type,
                            "entity_type": task.entity_type,
                            "entity_id": task.entity_id,
                            "assigned_role": task.assigned_role,
                            "status": task.status,
                            "required": task.required,
                            "payload": task.payload,
                            "created_at": self._required_utc(
                                task.created_at,
                                f"Approval task {task.id}",
                            ),
                            "updated_at": self._required_utc(
                                task.updated_at,
                                f"Approval task {task.id}",
                            ),
                        }
                        for task in tasks
                    ],
                    "records": [
                        {
                            "id": record.id,
                            "task_id": record.task_id,
                            "decision": record.decision,
                            "decided_by": record.decided_by,
                            "reason": record.reason,
                            "payload": record.payload,
                            "decided_at": self._required_utc(
                                record.decided_at,
                                f"Approval record {record.id}",
                            ),
                        }
                        for record in records
                    ],
                }
            ),
        )

    def _workflow_contents(
        self,
        project_id: str,
        cutoff: AuditEvent,
    ) -> list[dict[str, Any]]:
        rows = list(
            self.session.scalars(
                select(WorkflowTransitionRow)
                .where(
                    WorkflowTransitionRow.project_id == project_id,
                    WorkflowTransitionRow.occurred_at <= cutoff.occurred_at,
                )
                .order_by(WorkflowTransitionRow.occurred_at, WorkflowTransitionRow.id)
            )
        )
        return cast(
            list[dict[str, Any]],
            canonical_data(
                [
                    {
                        "id": row.id,
                        "from_state": row.from_state,
                        "to_state": row.to_state,
                        "actor_id": row.actor_id,
                        "reason": row.reason,
                        "occurred_at": self._required_utc(
                            row.occurred_at,
                            f"Workflow transition {row.id}",
                        ),
                    }
                    for row in rows
                ]
            ),
        )

    def _signing_material(self) -> ExportSigningMaterial:
        if not self.settings.export_signing_configured:
            raise ValueError("Ed25519 export signing key is not configured")
        assert self.settings.export_signing_key_id is not None
        assert self.settings.export_signing_private_key_b64 is not None
        return load_signing_material(
            key_id=self.settings.export_signing_key_id,
            private_key_b64=(self.settings.export_signing_private_key_b64.get_secret_value()),
        )

    @staticmethod
    def _audit_event(row: AuditEventRow) -> AuditEvent:
        occurred_at = ensure_utc(row.occurred_at)
        if occurred_at is None:
            raise ExportIntegrityError("Audit event has no timestamp")
        return AuditEvent(
            sequence=row.sequence,
            event_id=row.id,
            aggregate_type=row.aggregate_type,
            aggregate_id=row.aggregate_id,
            event_type=row.event_type,
            actor_id=row.actor_id,
            actor_roles=tuple(row.actor_roles),
            request_id=row.request_id,
            reason=row.reason,
            occurred_at=occurred_at,
            payload=row.payload,
            previous_hash=row.previous_hash,
            signing_key_id=row.signing_key_id,
            signature_version=row.signature_version,
            event_hash=row.event_hash,
            signature=row.signature,
        )

    @staticmethod
    def _filename(project_code: str, snapshot_id: str) -> str:
        safe_code = re.sub(r"[^A-Za-z0-9._-]+", "-", project_code).strip("-") or "project"
        safe_snapshot = re.sub(r"[^A-Za-z0-9._-]+", "-", snapshot_id).strip("-")
        return f"{safe_code}-{safe_snapshot}-signed-estimate-audit.json"

    @staticmethod
    def _required_utc(value: datetime | None, label: str) -> datetime:
        normalized = ensure_utc(value)
        if normalized is None:
            raise ExportIntegrityError(f"{label} has no timestamp")
        return normalized

    @staticmethod
    def _view(row: ExportArtifactRow) -> ExportArtifactView:
        created_at = ensure_utc(row.created_at)
        if created_at is None:
            raise ExportIntegrityError("Export artifact has no creation timestamp")
        return ExportArtifactView(
            artifact_id=row.id,
            project_id=row.project_id,
            snapshot_id=row.snapshot_id,
            release_decision_id=row.release_decision_id,
            template_version_id=row.template_version_id,
            format=row.format,
            media_type=row.media_type,
            filename=row.filename,
            object_hash=row.object_hash,
            size_bytes=row.size_bytes,
            manifest_hash=row.manifest_hash,
            signature_algorithm=row.signature_algorithm,
            signing_key_id=row.signing_key_id,
            public_key_fingerprint=row.public_key_fingerprint,
            created_by=row.created_by,
            created_at=created_at,
        )
