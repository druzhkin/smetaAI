from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from tenderguard.application.projects import ProjectService
from tenderguard.config import Settings
from tenderguard.domain.common import content_hash, utc_now
from tenderguard.domain.enums import (
    ActorRole,
    EvidenceMethod,
    VerificationStatus,
    VersionStatus,
)
from tenderguard.domain.models import (
    Conflict,
    DomainModel,
    EvidenceLocation,
    Observation,
)
from tenderguard.domain.reconciliation import reconcile_observations
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.object_store import ObjectStore
from tenderguard.infrastructure.orm import (
    AdapterQualificationRow,
    ApprovalRecordRow,
    ApprovalTaskRow,
    ConflictRow,
    ControlledVersionRow,
    DocumentRevisionRow,
    DocumentRow,
    ObservationRow,
    ProjectControlledVersionRow,
)


class ObservationDraft(DomainModel):
    field_name: str
    value: Any
    unit: str | None = None
    method: EvidenceMethod
    method_version: str
    source_priority: int = Field(ge=0)
    location: EvidenceLocation
    observed_at: datetime
    confidence: Decimal | None = Field(default=None, ge=0, le=1)
    adapter_qualification_id: str | None = None
    basis_metadata: dict[str, str] = Field(default_factory=dict)


class ReconciliationOutcome(DomainModel):
    agreed_value: Any | None = None
    verified_observation_id: str | None = None
    conflict: Conflict | None = None


class ConflictResolutionCommand(DomainModel):
    selected_observation_id: str = Field(min_length=1)
    resolution_reason: str = Field(min_length=1, max_length=2000)


class ConflictResolutionResult(DomainModel):
    conflict: Conflict
    verified_observation: Observation


class EvidenceService:
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

    def record_observation(
        self,
        *,
        actor: Actor,
        project_id: str,
        draft: ObservationDraft,
        request_id: str,
        reason: str,
    ) -> Observation:
        actor.require_any(
            ActorRole.TECHNICAL_EXPERT,
            ActorRole.REVIEWER,
            ActorRole.SYSTEM,
            ActorRole.ADMIN,
        )
        project_service = ProjectService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        )
        project_service.get_project(actor=actor, project_id=project_id, lock=True)
        revision = self.session.scalar(
            select(DocumentRevisionRow)
            .join(DocumentRow, DocumentRow.id == DocumentRevisionRow.document_id)
            .where(
                DocumentRevisionRow.id == draft.location.document_revision_id,
                DocumentRow.id == draft.location.document_id,
                DocumentRow.project_id == project_id,
            )
        )
        if revision is None:
            raise ValueError("Evidence location is not part of this project")
        if draft.location.original_object_hash != revision.object_hash:
            raise ValueError("Evidence location object hash does not match document revision")
        self._validate_adapter(actor, draft)
        identity = {
            "project_id": project_id,
            "field_name": draft.field_name,
            "value": draft.value,
            "unit": draft.unit,
            "method": draft.method,
            "method_version": draft.method_version,
            "location": draft.location,
            "observed_at": draft.observed_at,
            "actor_id": actor.actor_id,
        }
        observation = Observation(
            observation_id=f"observation-{content_hash(identity)[:24]}",
            field_name=draft.field_name,
            value=draft.value,
            unit=draft.unit,
            method=draft.method,
            method_version=draft.method_version,
            source_priority=draft.source_priority,
            location=draft.location,
            observed_at=draft.observed_at,
            actor_id=actor.actor_id,
            confidence=draft.confidence,
            status=VerificationStatus.UNVERIFIED,
        )
        payload: dict[str, Any] = {
            "observation": observation.model_dump(mode="json"),
            "adapter_qualification_id": draft.adapter_qualification_id,
            **draft.basis_metadata,
        }
        existing = self.session.get(ObservationRow, observation.observation_id)
        if existing is not None:
            return Observation.model_validate(existing.payload["observation"])
        self.session.add(
            ObservationRow(
                id=observation.observation_id,
                project_id=project_id,
                document_revision_id=revision.id,
                field_name=observation.field_name,
                method=observation.method.value,
                method_version=observation.method_version,
                status=observation.status.value,
                payload=payload,
                created_at=utc_now(),
            )
        )
        project_service.record_event(
            aggregate_type="project",
            aggregate_id=project_id,
            event_type="evidence_observation_recorded",
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload={
                "observation_id": observation.observation_id,
                "document_revision_id": revision.id,
                "field_name": observation.field_name,
                "method": observation.method,
                "method_version": observation.method_version,
                "adapter_qualification_id": draft.adapter_qualification_id,
            },
        )
        return observation

    def reconcile(
        self,
        *,
        actor: Actor,
        project_id: str,
        observation_ids: tuple[str, ...],
        reconciliation_version_id: str,
        request_id: str,
        reason: str,
    ) -> ReconciliationOutcome:
        actor.require_any(ActorRole.REVIEWER, ActorRole.TECHNICAL_EXPERT)
        project_service = ProjectService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        )
        project_service.get_project(actor=actor, project_id=project_id, lock=True)
        rule_version = self.session.scalar(
            select(ControlledVersionRow)
            .join(
                ProjectControlledVersionRow,
                ProjectControlledVersionRow.controlled_version_id == ControlledVersionRow.id,
            )
            .where(
                ControlledVersionRow.id == reconciliation_version_id,
                ControlledVersionRow.kind == "reconciliation_rules",
                ControlledVersionRow.status == VersionStatus.APPROVED.value,
                ProjectControlledVersionRow.project_id == project_id,
            )
        )
        if rule_version is None:
            raise ValueError("A bound approved reconciliation_rules version is required")
        rows = list(
            self.session.scalars(
                select(ObservationRow).where(
                    ObservationRow.project_id == project_id,
                    ObservationRow.id.in_(observation_ids),
                )
            )
        )
        if len(rows) != len(set(observation_ids)):
            raise ValueError("One or more evidence observations do not exist")
        rows.sort(key=lambda row: row.id)
        observations = tuple(Observation.model_validate(row.payload["observation"]) for row in rows)
        self._validate_independence_domains(rows)
        value, conflict = reconcile_observations(
            conflict_namespace=f"{project_id}:{reconciliation_version_id}",
            observations=observations,
        )
        if value is None and conflict is None:
            raise ValueError("At least two independent observations are required")
        if conflict is not None:
            if self.session.get(ConflictRow, conflict.conflict_id) is None:
                now = utc_now()
                self.session.add(
                    ConflictRow(
                        id=conflict.conflict_id,
                        project_id=project_id,
                        field_name=conflict.field_name,
                        status=conflict.status.value,
                        payload=conflict.model_dump(mode="json"),
                        created_at=now,
                        updated_at=now,
                    )
                )
                task_id = self._conflict_task_id(conflict.conflict_id)
                self.session.add(
                    ApprovalTaskRow(
                        id=task_id,
                        project_id=project_id,
                        task_type="CONFLICT_RESOLUTION",
                        entity_type="evidence_conflict",
                        entity_id=conflict.conflict_id,
                        assigned_role=ActorRole.REVIEWER.value,
                        status="PENDING",
                        required=True,
                        payload={
                            "created_by": actor.actor_id,
                            "observation_ids": list(conflict.observation_ids),
                        },
                        created_at=now,
                        updated_at=now,
                    )
                )
                project_service.record_event(
                    aggregate_type="project",
                    aggregate_id=project_id,
                    event_type="evidence_conflict_created",
                    actor=actor,
                    request_id=request_id,
                    reason=reason,
                    payload=conflict.model_dump(mode="json"),
                )
            return ReconciliationOutcome(conflict=conflict)

        basis_metadata = self._common_basis_metadata(rows, value)
        source = observations[0]
        derived_identity = {
            "project_id": project_id,
            "source_observation_ids": sorted(observation_ids),
            "reconciliation_version_id": reconciliation_version_id,
            "value": value,
        }
        verified = Observation(
            observation_id=f"observation-{content_hash(derived_identity)[:24]}",
            field_name=source.field_name,
            value=value,
            unit=source.unit,
            method=EvidenceMethod.RULE_ENGINE,
            method_version=reconciliation_version_id,
            source_priority=min(item.source_priority for item in observations),
            location=source.location,
            observed_at=utc_now(),
            actor_id=actor.actor_id,
            confidence=None,
            status=VerificationStatus.VERIFIED,
        )
        if self.session.get(ObservationRow, verified.observation_id) is not None:
            return ReconciliationOutcome(
                agreed_value=value,
                verified_observation_id=verified.observation_id,
            )
        self.session.add(
            ObservationRow(
                id=verified.observation_id,
                project_id=project_id,
                document_revision_id=source.location.document_revision_id,
                field_name=verified.field_name,
                method=verified.method.value,
                method_version=verified.method_version,
                status=verified.status.value,
                payload={
                    "observation": verified.model_dump(mode="json"),
                    "source_observation_ids": sorted(observation_ids),
                    "reconciliation_version_id": reconciliation_version_id,
                    **basis_metadata,
                },
                created_at=utc_now(),
            )
        )
        project_service.record_event(
            aggregate_type="project",
            aggregate_id=project_id,
            event_type="evidence_reconciled",
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload={
                "verified_observation_id": verified.observation_id,
                "source_observation_ids": sorted(observation_ids),
                "reconciliation_version_id": reconciliation_version_id,
            },
        )
        return ReconciliationOutcome(
            agreed_value=value,
            verified_observation_id=verified.observation_id,
        )

    def resolve_conflict(
        self,
        *,
        actor: Actor,
        project_id: str,
        conflict_id: str,
        command: ConflictResolutionCommand,
        request_id: str,
    ) -> ConflictResolutionResult:
        actor.require_any(ActorRole.REVIEWER, ActorRole.TECHNICAL_EXPERT)
        project_service = ProjectService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        )
        project_service.get_project(actor=actor, project_id=project_id, lock=True)
        row = self.session.scalar(
            select(ConflictRow)
            .where(
                ConflictRow.id == conflict_id,
                ConflictRow.project_id == project_id,
            )
            .with_for_update()
        )
        if row is None:
            raise LookupError(conflict_id)
        conflict = Conflict.model_validate(row.payload)
        if conflict.status is not VerificationStatus.CONFLICT:
            raise ValueError("Only an unresolved conflict can be resolved")
        if command.selected_observation_id not in conflict.observation_ids:
            raise ValueError("Selected observation is not part of the conflict")
        selected = self.session.scalar(
            select(ObservationRow).where(
                ObservationRow.id == command.selected_observation_id,
                ObservationRow.project_id == project_id,
            )
        )
        if selected is None:
            raise LookupError(command.selected_observation_id)
        selected_observation = Observation.model_validate(selected.payload["observation"])
        if selected_observation.actor_id == actor.actor_id:
            raise ValueError("Conflict resolution requires an actor different from the source")

        now = utc_now()
        resolved_conflict = conflict.model_copy(
            update={
                "status": VerificationStatus.VERIFIED,
                "resolved_value": selected_observation.value,
                "resolved_by": actor.actor_id,
                "resolved_at": now,
                "resolution_reason": command.resolution_reason,
            }
        )
        row.status = VerificationStatus.VERIFIED.value
        row.payload = resolved_conflict.model_dump(mode="json")
        row.updated_at = now

        derived_id = (
            "observation-"
            + content_hash(
                {
                    "conflict_id": conflict_id,
                    "selected_observation_id": selected.id,
                    "resolved_value": selected_observation.value,
                }
            )[:24]
        )
        verified = selected_observation.model_copy(
            update={
                "observation_id": derived_id,
                "method": EvidenceMethod.RULE_ENGINE,
                "method_version": "conflict-resolution-v1",
                "observed_at": now,
                "actor_id": actor.actor_id,
                "confidence": None,
                "status": VerificationStatus.VERIFIED,
            }
        )
        self.session.add(
            ObservationRow(
                id=verified.observation_id,
                project_id=project_id,
                document_revision_id=selected.document_revision_id,
                field_name=verified.field_name,
                method=verified.method.value,
                method_version=verified.method_version,
                status=verified.status.value,
                payload={
                    "observation": verified.model_dump(mode="json"),
                    "source_observation_ids": [selected.id],
                    "conflict_id": conflict_id,
                    "basis_type": "CONFLICT_RESOLUTION",
                },
                created_at=now,
            )
        )
        task_id = self._conflict_task_id(conflict_id)
        task = self.session.get(ApprovalTaskRow, task_id)
        if task is None:
            raise RuntimeError("Conflict has no mandatory review task")
        if task.status != "PENDING":
            raise ValueError("Conflict review task is not pending")
        task.status = "APPROVED"
        task.updated_at = now
        approval_id = f"approval-{uuid4()}"
        self.session.add(
            ApprovalRecordRow(
                id=approval_id,
                task_id=task.id,
                decision="APPROVED",
                decided_by=actor.actor_id,
                decided_at=now,
                reason=command.resolution_reason,
                payload={
                    "project_id": project_id,
                    "evidence_ids": [selected.id, verified.observation_id],
                    "conflict_id": conflict_id,
                },
            )
        )
        project_service.record_event(
            aggregate_type="project",
            aggregate_id=project_id,
            event_type="evidence_conflict_resolved",
            actor=actor,
            request_id=request_id,
            reason=command.resolution_reason,
            payload={
                "conflict_id": conflict_id,
                "selected_observation_id": selected.id,
                "verified_observation_id": verified.observation_id,
                "approval_task_id": task.id,
                "approval_record_id": approval_id,
            },
        )
        return ConflictResolutionResult(
            conflict=resolved_conflict,
            verified_observation=verified,
        )

    @staticmethod
    def _conflict_task_id(conflict_id: str) -> str:
        return f"approval-task-conflict-{content_hash(conflict_id)[:24]}"

    def _validate_adapter(self, actor: Actor, draft: ObservationDraft) -> None:
        if draft.method is EvidenceMethod.MANUAL:
            if draft.adapter_qualification_id is not None:
                raise ValueError("Manual evidence cannot claim an adapter qualification")
            if ActorRole.SYSTEM in actor.roles:
                raise ValueError("SYSTEM actor cannot create manual evidence")
            return
        actor.require_any(ActorRole.SYSTEM)
        if not draft.adapter_qualification_id:
            raise ValueError("Automated evidence requires an adapter qualification")
        qualification = self.session.scalar(
            select(AdapterQualificationRow).where(
                AdapterQualificationRow.id == draft.adapter_qualification_id,
                AdapterQualificationRow.status == "APPROVED",
                AdapterQualificationRow.adapter_version == draft.method_version,
            )
        )
        if qualification is None:
            raise ValueError("Evidence adapter is not qualified for this version")
        if qualification.valid_until and qualification.valid_until < utc_now().date():
            raise ValueError("Evidence adapter qualification has expired")
        if qualification.payload.get("organization_id") != actor.organization_id:
            raise ValueError("Evidence adapter qualification belongs to another organisation")
        if draft.method.value not in qualification.payload.get("supported_methods", []):
            raise ValueError("Evidence method is outside the adapter qualification")

    def _validate_independence_domains(self, rows: list[ObservationRow]) -> None:
        qualification_ids = {
            row.payload.get("adapter_qualification_id")
            for row in rows
            if row.payload.get("adapter_qualification_id")
        }
        if len(qualification_ids) != len(rows):
            raise ValueError(
                "Independent automatic reconciliation requires a qualified adapter "
                "for every observation"
            )
        qualifications = list(
            self.session.scalars(
                select(AdapterQualificationRow).where(
                    AdapterQualificationRow.id.in_(qualification_ids),
                    AdapterQualificationRow.status == "APPROVED",
                )
            )
        )
        domains = {
            qualification.payload.get("independence_domain") for qualification in qualifications
        }
        if len(qualifications) != len(rows) or None in domains or len(domains) != len(rows):
            raise ValueError(
                "Observations do not come from distinct qualified independence domains"
            )

    @staticmethod
    def _common_basis_metadata(rows: list[ObservationRow], agreed_value: Any) -> dict[str, str]:
        keys = ("basis_type", "currency", "unit")
        metadata = [{key: row.payload.get(key) for key in keys} for row in rows]
        first = metadata[0]
        if any(item != first for item in metadata[1:]):
            raise ValueError("Independent observations disagree in commercial basis")
        if first.get("basis_type") == "NORMALIZED_PRICE":
            if not first.get("currency") or not first.get("unit"):
                raise ValueError("Normalized price evidence lacks currency or unit")
            return {
                "basis_type": "NORMALIZED_PRICE",
                "unit_rate": str(agreed_value),
                "currency": str(first["currency"]),
                "unit": str(first["unit"]),
            }
        return {}
