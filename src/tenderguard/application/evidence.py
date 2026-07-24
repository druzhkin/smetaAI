from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from pydantic import Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from tenderguard.application.projects import (
    OptimisticLockError,
    ProjectService,
    SystemProjectAccess,
)
from tenderguard.config import Settings
from tenderguard.domain.common import content_hash, ensure_utc, utc_now
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

    @field_validator("basis_metadata")
    @classmethod
    def basis_metadata_is_closed_and_normalized(
        cls,
        value: dict[str, str],
    ) -> dict[str, str]:
        if not value:
            return {}
        allowed = {"basis_type", "currency", "unit"}
        unexpected = set(value) - allowed
        if unexpected:
            raise ValueError(
                "Unsupported evidence basis metadata: " + ", ".join(sorted(unexpected))
            )
        normalized = {key: item.strip() for key, item in value.items()}
        if any(not item for item in normalized.values()):
            raise ValueError("Evidence basis metadata values cannot be blank")
        if set(normalized) != allowed:
            raise ValueError("Normalized price evidence requires basis_type, currency, and unit")
        if normalized["basis_type"] != "NORMALIZED_PRICE":
            raise ValueError("Only NORMALIZED_PRICE commercial evidence is supported")
        currency = normalized["currency"]
        if len(currency) != 3 or not currency.isascii() or not currency.isalpha():
            raise ValueError("Evidence currency must be a three-letter ISO code")
        normalized["currency"] = currency.upper()
        return normalized

    @model_validator(mode="after")
    def normalized_price_matches_observation(self) -> ObservationDraft:
        if not self.basis_metadata:
            return self
        if self.unit != self.basis_metadata["unit"]:
            raise ValueError("Normalized price evidence unit must match the observation unit")
        try:
            rate = Decimal(str(self.value))
        except (InvalidOperation, TypeError, ValueError) as error:
            raise ValueError("Normalized price evidence value must be a decimal rate") from error
        if not rate.is_finite() or rate <= 0:
            raise ValueError("Normalized price evidence rate must be finite and positive")
        return self


class ReconciliationOutcome(DomainModel):
    agreed_value: Any | None = None
    verified_observation_id: str | None = None
    conflict: Conflict | None = None


class ConflictResolutionCommand(DomainModel):
    selected_observation_id: str = Field(min_length=1, max_length=64)
    resolution_reason: str = Field(min_length=1, max_length=2000)
    expected_conflict_updated_at: datetime
    expected_task_updated_at: datetime

    @field_validator("expected_conflict_updated_at", "expected_task_updated_at")
    @classmethod
    def timestamps_are_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Expected timestamps must include a timezone")
        return value


class ConflictResolutionResult(DomainModel):
    conflict: Conflict
    verified_observation: Observation


class ConflictObservationView(Observation):
    adapter_qualification_id: str | None = None
    adapter_qualification_status: str | None = None
    adapter_qualification_valid_until: date | None = None
    independence_domain: str | None = None
    basis_metadata: dict[str, str] = Field(default_factory=dict)


class ConflictReviewView(DomainModel):
    conflict: Conflict
    conflict_updated_at: datetime
    observations: tuple[ConflictObservationView, ...]
    missing_observation_ids: tuple[str, ...] = ()
    task_id: str
    task_status: str | None
    task_required: bool | None
    task_updated_at: datetime | None
    task_created_by: str | None
    resolution_allowed: bool
    resolution_blockers: tuple[str, ...] = ()


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
        project_service = ProjectService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        )
        if ActorRole.SYSTEM in actor.roles:
            project_service.get_project(
                actor=actor,
                project_id=project_id,
                lock=True,
                system_access=SystemProjectAccess(
                    qualification_id=draft.adapter_qualification_id or "",
                    capability=draft.method.value,
                ),
            )
        else:
            project_service.get_project(
                actor=actor,
                project_id=project_id,
                lock=True,
                required_roles=(ActorRole.TECHNICAL_EXPERT, ActorRole.REVIEWER),
            )
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
            "basis_metadata": draft.basis_metadata,
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
            **draft.basis_metadata,
            "observation": observation.model_dump(mode="json"),
            "adapter_qualification_id": draft.adapter_qualification_id,
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
        project_service = ProjectService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        )
        project_service.get_project(
            actor=actor,
            project_id=project_id,
            lock=True,
            required_roles=(ActorRole.REVIEWER, ActorRole.TECHNICAL_EXPERT),
        )
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
        self._validate_independence_domains(
            rows,
            organization_id=actor.organization_id,
        )
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
        resolution_reason = command.resolution_reason.strip()
        if not resolution_reason:
            raise ValueError("resolution_reason is required")
        project_service = ProjectService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        )
        project_service.get_project(
            actor=actor,
            project_id=project_id,
            lock=True,
            required_roles=(ActorRole.REVIEWER, ActorRole.TECHNICAL_EXPERT),
        )
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
        if row.field_name != conflict.field_name or row.status != conflict.status.value:
            raise RuntimeError("Conflict row and payload state differ")
        if conflict.status is not VerificationStatus.CONFLICT:
            raise ValueError("Only an unresolved conflict can be resolved")
        expected_conflict_updated_at = ensure_utc(command.expected_conflict_updated_at)
        assert expected_conflict_updated_at is not None
        if ensure_utc(row.updated_at) != expected_conflict_updated_at:
            raise OptimisticLockError(
                "Conflict changed after it was loaded; reload before resolving"
            )
        task_id = self._conflict_task_id(conflict_id)
        task = self.session.scalar(
            select(ApprovalTaskRow)
            .where(
                ApprovalTaskRow.id == task_id,
                ApprovalTaskRow.project_id == project_id,
                ApprovalTaskRow.entity_type == "evidence_conflict",
                ApprovalTaskRow.entity_id == conflict_id,
                ApprovalTaskRow.task_type == "CONFLICT_RESOLUTION",
            )
            .with_for_update()
        )
        if task is None:
            raise RuntimeError("Conflict has no mandatory review task")
        task_observation_ids = tuple(
            value for value in task.payload.get("observation_ids", []) if isinstance(value, str)
        )
        if (
            not task.required
            or task_observation_ids != conflict.observation_ids
            or not isinstance(task.payload.get("created_by"), str)
            or not str(task.payload["created_by"]).strip()
        ):
            raise ValueError("Conflict review task integrity check failed")
        expected_task_updated_at = ensure_utc(command.expected_task_updated_at)
        assert expected_task_updated_at is not None
        if ensure_utc(task.updated_at) != expected_task_updated_at:
            raise OptimisticLockError(
                "Conflict review task changed after it was loaded; reload before resolving"
            )
        if task.status != "PENDING":
            raise ValueError("Conflict review task is not pending")
        if task.payload.get("created_by") == actor.actor_id:
            raise ValueError("Four-eyes violation: conflict creator cannot resolve its review task")
        if command.selected_observation_id not in conflict.observation_ids:
            raise ValueError("Selected observation is not part of the conflict")
        source_rows_by_id = {
            source.id: source
            for source in self.session.scalars(
                select(ObservationRow).where(
                    ObservationRow.project_id == project_id,
                    ObservationRow.id.in_(conflict.observation_ids),
                )
            )
        }
        if len(source_rows_by_id) != len(conflict.observation_ids):
            raise ValueError("One or more conflict observations are missing")
        source_rows = [
            source_rows_by_id[observation_id] for observation_id in conflict.observation_ids
        ]
        if any(source.field_name != conflict.field_name for source in source_rows):
            raise ValueError("Conflict observation field does not match the conflict")
        self._validate_independence_domains(
            source_rows,
            organization_id=actor.organization_id,
        )
        source_observations_by_id = {
            source.id: Observation.model_validate(source.payload["observation"])
            for source in source_rows
        }
        for source in source_rows:
            self._validated_basis_metadata(
                source,
                source_observations_by_id[source.id],
            )
        selected = source_rows_by_id[command.selected_observation_id]
        selected_observation = source_observations_by_id[selected.id]
        if selected_observation.field_name != conflict.field_name:
            raise ValueError("Selected observation field does not match the conflict")
        if selected_observation.actor_id == actor.actor_id:
            raise ValueError("Conflict resolution requires an actor different from the source")
        basis_metadata = self._validated_basis_metadata(selected, selected_observation)

        now = utc_now()
        resolved_conflict = conflict.model_copy(
            update={
                "status": VerificationStatus.VERIFIED,
                "resolved_value": selected_observation.value,
                "resolved_by": actor.actor_id,
                "resolved_at": now,
                "resolution_reason": resolution_reason,
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
                    "derivation_type": "CONFLICT_RESOLUTION",
                    **basis_metadata,
                },
                created_at=now,
            )
        )
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
                reason=resolution_reason,
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
            reason=resolution_reason,
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

    def conflict_review(
        self,
        *,
        actor: Actor,
        project_id: str,
        conflict_id: str,
    ) -> ConflictReviewView:
        project_service = ProjectService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        )
        project_service.get_project(
            actor=actor,
            project_id=project_id,
            required_roles=(ActorRole.REVIEWER, ActorRole.TECHNICAL_EXPERT),
        )
        row = self.session.scalar(
            select(ConflictRow).where(
                ConflictRow.id == conflict_id,
                ConflictRow.project_id == project_id,
            )
        )
        if row is None:
            raise LookupError(conflict_id)
        conflict = Conflict.model_validate(row.payload)
        state_consistent = (
            row.field_name == conflict.field_name and row.status == conflict.status.value
        )
        observation_rows = {
            observation.id: observation
            for observation in self.session.scalars(
                select(ObservationRow).where(
                    ObservationRow.project_id == project_id,
                    ObservationRow.id.in_(conflict.observation_ids),
                )
            )
        }
        qualification_ids = {
            value
            for value in (
                observation.payload.get("adapter_qualification_id")
                for observation in observation_rows.values()
            )
            if isinstance(value, str) and value
        }
        qualifications = {
            qualification.id: qualification
            for qualification in self.session.scalars(
                select(AdapterQualificationRow).where(
                    AdapterQualificationRow.id.in_(qualification_ids)
                )
            )
        }
        observations = tuple(
            self._conflict_observation_view(
                observation_rows[observation_id],
                qualifications.get(
                    str(observation_rows[observation_id].payload.get("adapter_qualification_id"))
                ),
            )
            for observation_id in conflict.observation_ids
            if observation_id in observation_rows
        )
        missing_observation_ids = tuple(
            observation_id
            for observation_id in conflict.observation_ids
            if observation_id not in observation_rows
        )
        task_id = self._conflict_task_id(conflict_id)
        task = self.session.scalar(
            select(ApprovalTaskRow).where(
                ApprovalTaskRow.id == task_id,
                ApprovalTaskRow.project_id == project_id,
                ApprovalTaskRow.entity_type == "evidence_conflict",
                ApprovalTaskRow.entity_id == conflict_id,
                ApprovalTaskRow.task_type == "CONFLICT_RESOLUTION",
            )
        )
        blockers: list[str] = []
        if conflict.status is not VerificationStatus.CONFLICT:
            blockers.append("CONFLICT_NOT_OPEN")
        if not state_consistent:
            blockers.append("CONFLICT_STATE_MISMATCH")
        if missing_observation_ids:
            blockers.append("CONFLICT_OBSERVATION_MISSING")
        if any(observation.field_name != conflict.field_name for observation in observations):
            blockers.append("CONFLICT_OBSERVATION_FIELD_MISMATCH")
        try:
            self._validate_independence_domains(
                list(observation_rows.values()),
                organization_id=actor.organization_id,
            )
        except ValueError:
            blockers.append("CONFLICT_INDEPENDENCE_INVALID")
        try:
            for observation_row in observation_rows.values():
                self._validated_basis_metadata(
                    observation_row,
                    Observation.model_validate(observation_row.payload["observation"]),
                )
        except (KeyError, TypeError, ValueError):
            blockers.append("CONFLICT_COMMERCIAL_BASIS_INVALID")
        if task is None:
            blockers.append("CONFLICT_TASK_MISSING")
        else:
            task_observation_ids = tuple(
                value for value in task.payload.get("observation_ids", []) if isinstance(value, str)
            )
            task_created_by = task.payload.get("created_by")
            if not task.required:
                blockers.append("CONFLICT_TASK_NOT_REQUIRED")
            if task_observation_ids != conflict.observation_ids:
                blockers.append("CONFLICT_TASK_SCOPE_MISMATCH")
            if not isinstance(task_created_by, str) or not task_created_by.strip():
                blockers.append("CONFLICT_TASK_CREATOR_MISSING")
            if task.status != "PENDING":
                blockers.append("TASK_NOT_PENDING")
            if task_created_by == actor.actor_id:
                blockers.append("FOUR_EYES_TASK_CREATOR")
        if observations and all(
            observation.actor_id == actor.actor_id for observation in observations
        ):
            blockers.append("NO_INDEPENDENT_OBSERVATION")
        updated_at = ensure_utc(row.updated_at)
        if updated_at is None:
            raise RuntimeError("Conflict update timestamp is missing")
        return ConflictReviewView(
            conflict=conflict,
            conflict_updated_at=updated_at,
            observations=observations,
            missing_observation_ids=missing_observation_ids,
            task_id=task_id,
            task_status=task.status if task is not None else None,
            task_required=task.required if task is not None else None,
            task_updated_at=ensure_utc(task.updated_at) if task is not None else None,
            task_created_by=(
                str(task.payload.get("created_by"))
                if task is not None and task.payload.get("created_by")
                else None
            ),
            resolution_allowed=not blockers,
            resolution_blockers=tuple(blockers),
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
        if qualification.payload.get("service_actor_id") != actor.actor_id:
            raise ValueError("Evidence adapter qualification belongs to another service identity")
        if draft.method.value not in qualification.payload.get("supported_methods", []):
            raise ValueError("Evidence method is outside the adapter qualification")

    def _validate_independence_domains(
        self,
        rows: list[ObservationRow],
        *,
        organization_id: str,
    ) -> None:
        qualification_ids: list[str] = []
        for row in rows:
            qualification_id = row.payload.get("adapter_qualification_id")
            if not isinstance(qualification_id, str) or not qualification_id:
                raise ValueError(
                    "Independent automatic reconciliation requires a qualified adapter "
                    "for every observation"
                )
            qualification_ids.append(qualification_id)
        if len(set(qualification_ids)) != len(rows):
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
        domains = [
            qualification.payload.get("independence_domain") for qualification in qualifications
        ]
        if (
            len(qualifications) != len(rows)
            or any(not isinstance(domain, str) or not domain for domain in domains)
            or len(set(domains)) != len(rows)
        ):
            raise ValueError(
                "Observations do not come from distinct qualified independence domains"
            )
        qualifications_by_id = {qualification.id: qualification for qualification in qualifications}
        for row in rows:
            qualification_id = row.payload.get("adapter_qualification_id")
            qualification = qualifications_by_id.get(str(qualification_id))
            observation = Observation.model_validate(row.payload["observation"])
            if qualification is None:
                raise ValueError("An observation adapter qualification is unavailable")
            if (
                row.field_name != observation.field_name
                or row.method != observation.method.value
                or row.method_version != observation.method_version
                or row.status != observation.status.value
                or qualification.adapter_version != row.method_version
                or row.method not in qualification.payload.get("supported_methods", [])
                or qualification.payload.get("organization_id") != organization_id
                or qualification.payload.get("service_actor_id") != observation.actor_id
                or (
                    qualification.valid_until is not None
                    and qualification.valid_until < utc_now().date()
                )
            ):
                raise ValueError("An observation no longer matches its qualified adapter identity")

    @staticmethod
    def _conflict_observation_view(
        row: ObservationRow,
        qualification: AdapterQualificationRow | None,
    ) -> ConflictObservationView:
        observation = Observation.model_validate(row.payload["observation"])
        basis_metadata = {
            key: str(row.payload[key])
            for key in ("basis_type", "currency", "unit")
            if row.payload.get(key) is not None
        }
        qualification_id = row.payload.get("adapter_qualification_id")
        return ConflictObservationView.model_validate(
            {
                **observation.model_dump(mode="python"),
                "adapter_qualification_id": (
                    qualification_id
                    if isinstance(qualification_id, str) and qualification_id
                    else None
                ),
                "adapter_qualification_status": (
                    qualification.status if qualification is not None else None
                ),
                "adapter_qualification_valid_until": (
                    qualification.valid_until if qualification is not None else None
                ),
                "independence_domain": (
                    str(qualification.payload.get("independence_domain"))
                    if qualification is not None
                    and qualification.payload.get("independence_domain")
                    else None
                ),
                "basis_metadata": basis_metadata,
            }
        )

    @staticmethod
    def _common_basis_metadata(rows: list[ObservationRow], agreed_value: Any) -> dict[str, str]:
        metadata = [
            EvidenceService._validated_basis_metadata(
                row,
                Observation.model_validate(row.payload["observation"]),
            )
            for row in rows
        ]
        first = metadata[0]
        if any(item != first for item in metadata[1:]):
            raise ValueError("Independent observations disagree in commercial basis")
        if first and Decimal(first["unit_rate"]) != Decimal(str(agreed_value)):
            raise ValueError("Commercial basis rate differs from the reconciled value")
        return first

    @staticmethod
    def _validated_basis_metadata(
        row: ObservationRow,
        observation: Observation,
    ) -> dict[str, str]:
        basis_type = row.payload.get("basis_type")
        currency = row.payload.get("currency")
        unit = row.payload.get("unit")
        if basis_type is None and currency is None and unit is None:
            return {}
        if basis_type != "NORMALIZED_PRICE":
            raise ValueError("Evidence declares an unsupported commercial basis type")
        if (
            not isinstance(currency, str)
            or len(currency) != 3
            or not currency.isascii()
            or not currency.isalpha()
            or currency != currency.upper()
        ):
            raise ValueError("Normalized price evidence has an invalid currency")
        if not isinstance(unit, str) or not unit.strip() or unit != unit.strip():
            raise ValueError("Normalized price evidence has an invalid unit")
        if observation.unit != unit:
            raise ValueError("Normalized price evidence unit differs from its observation unit")
        try:
            rate = Decimal(str(observation.value))
        except (InvalidOperation, TypeError, ValueError) as error:
            raise ValueError("Normalized price evidence has no valid unit rate") from error
        if not rate.is_finite() or rate <= 0:
            raise ValueError("Normalized price evidence unit rate must be finite and positive")
        return {
            "basis_type": "NORMALIZED_PRICE",
            "unit_rate": str(rate),
            "currency": currency,
            "unit": unit,
        }
