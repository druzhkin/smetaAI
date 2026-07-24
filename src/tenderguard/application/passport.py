from __future__ import annotations

from typing import Any
from uuid import uuid4

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from tenderguard.application.evidence_independence import (
    require_distinct_qualified_independence,
)
from tenderguard.application.projects import ProjectService
from tenderguard.config import Settings
from tenderguard.domain.common import content_hash, utc_now
from tenderguard.domain.enums import (
    ActorRole,
    ApprovalState,
    Severity,
    VerificationStatus,
    VersionStatus,
)
from tenderguard.domain.models import DomainModel, ValidationFinding
from tenderguard.domain.passport import PassportFact, ProjectPassport, validate_passport
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.object_store import ObjectStore
from tenderguard.infrastructure.orm import (
    ControlledVersionRow,
    ObservationRow,
    ProjectControlledVersionRow,
    ProjectPassportFactRow,
    VerificationFindingRow,
)


class PassportFactDraft(DomainModel):
    field_name: str = Field(min_length=1, max_length=200)
    value: Any
    unit: str | None = None
    observation_ids: tuple[str, ...] = Field(min_length=1)


class PassportFactView(DomainModel):
    fact_id: str
    field_name: str
    value: Any
    unit: str | None
    observation_ids: tuple[str, ...]
    status: VerificationStatus
    supersedes_fact_id: str | None
    is_current: bool
    created_by: str
    verified_by: str | None = None


class PassportValidationResult(DomainModel):
    passport: ProjectPassport
    findings: tuple[ValidationFinding, ...]
    requirements_version_id: str


class PassportService:
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

    def submit_fact(
        self,
        *,
        actor: Actor,
        project_id: str,
        draft: PassportFactDraft,
        request_id: str,
        reason: str,
    ) -> PassportFactView:
        project_service = self._project_service()
        project = project_service.get_project(
            actor=actor,
            project_id=project_id,
            lock=True,
            required_roles=(ActorRole.TECHNICAL_EXPERT, ActorRole.REVIEWER),
        )
        if ApprovalState(project.state) not in {
            ApprovalState.EXTRACTION_IN_PROGRESS,
            ApprovalState.EXTRACTION_REVIEW,
            ApprovalState.BOQ_IN_PROGRESS,
            ApprovalState.BOQ_REVIEW,
        }:
            raise ValueError("Passport facts may only change before pricing")
        observations = self._observations(project_id, draft.observation_ids)
        self._validate_observation_values(draft, observations)
        requirements, requirements_version_id = self._requirements(project_id)
        independent_fields = requirements["independently_verified_fields"]
        if draft.field_name in independent_fields:
            require_distinct_qualified_independence(
                self.session,
                project_id=project_id,
                observations=observations,
            )

        previous = self.session.scalar(
            select(ProjectPassportFactRow)
            .where(
                ProjectPassportFactRow.project_id == project_id,
                ProjectPassportFactRow.field_name == draft.field_name,
                ProjectPassportFactRow.is_current.is_(True),
            )
            .with_for_update()
        )
        now = utc_now()
        if previous is not None:
            previous.is_current = False
            previous.updated_at = now
        row = ProjectPassportFactRow(
            id=f"passport-fact-{uuid4()}",
            project_id=project_id,
            field_name=draft.field_name,
            status=VerificationStatus.IN_REVIEW.value,
            supersedes_fact_id=previous.id if previous else None,
            is_current=True,
            payload={
                **draft.model_dump(mode="json"),
                "created_by": actor.actor_id,
                "requirements_version_id": requirements_version_id,
                "document_set_revision_id": project.current_document_set_revision_id,
            },
            created_at=now,
            updated_at=now,
        )
        self.session.add(row)
        project_service.record_event(
            aggregate_type="project",
            aggregate_id=project_id,
            event_type="passport_fact_submitted",
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload={
                "fact_id": row.id,
                "field_name": row.field_name,
                "value_hash": content_hash(draft.value),
                "observation_ids": list(draft.observation_ids),
                "supersedes_fact_id": row.supersedes_fact_id,
                "requirements_version_id": requirements_version_id,
            },
        )
        return self._view(row)

    def verify_fact(
        self,
        *,
        actor: Actor,
        project_id: str,
        fact_id: str,
        request_id: str,
        reason: str,
    ) -> tuple[PassportFactView, PassportValidationResult]:
        project_service = self._project_service()
        project = project_service.get_project(
            actor=actor,
            project_id=project_id,
            lock=True,
            required_roles=(ActorRole.TECHNICAL_EXPERT, ActorRole.REVIEWER),
        )
        row = self.session.scalar(
            select(ProjectPassportFactRow)
            .where(
                ProjectPassportFactRow.id == fact_id,
                ProjectPassportFactRow.project_id == project_id,
                ProjectPassportFactRow.is_current.is_(True),
            )
            .with_for_update()
        )
        if row is None:
            raise LookupError(fact_id)
        if row.status != VerificationStatus.IN_REVIEW.value:
            raise ValueError("Only a current IN_REVIEW passport fact can be verified")
        if row.payload.get("document_set_revision_id") != project.current_document_set_revision_id:
            raise ValueError("Passport fact belongs to a superseded document-set revision")
        created_by = row.payload.get("created_by")
        if created_by == actor.actor_id:
            raise ValueError("Passport fact verification requires a different actor")
        draft = PassportFactDraft.model_validate(
            {
                "field_name": row.field_name,
                "value": row.payload.get("value"),
                "unit": row.payload.get("unit"),
                "observation_ids": row.payload.get("observation_ids"),
            }
        )
        observations = self._observations(project_id, draft.observation_ids)
        self._validate_observation_values(draft, observations)
        requirements, requirements_version_id = self._requirements(project_id)
        if row.payload.get("requirements_version_id") != requirements_version_id:
            raise ValueError("Passport fact was prepared under a superseded requirements version")
        if draft.field_name in requirements["independently_verified_fields"]:
            require_distinct_qualified_independence(
                self.session,
                project_id=project_id,
                observations=observations,
            )
        now = utc_now()
        row.status = VerificationStatus.VERIFIED.value
        row.updated_at = now
        row.payload = {
            **row.payload,
            "verified_by": actor.actor_id,
            "verified_at": now.isoformat(),
        }
        validation = self._validate_current(project_id)
        project_service.record_event(
            aggregate_type="project",
            aggregate_id=project_id,
            event_type="passport_fact_verified",
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload={
                "fact_id": row.id,
                "field_name": row.field_name,
                "requirements_version_id": requirements_version_id,
                "remaining_finding_ids": [finding.entity_ids for finding in validation.findings],
            },
        )
        return self._view(row), validation

    def validate_current(
        self,
        *,
        actor: Actor,
        project_id: str,
        request_id: str,
        reason: str,
    ) -> PassportValidationResult:
        project_service = self._project_service()
        project_service.get_project(
            actor=actor,
            project_id=project_id,
            lock=True,
            required_roles=(
                ActorRole.ESTIMATOR,
                ActorRole.TECHNICAL_EXPERT,
                ActorRole.REVIEWER,
                ActorRole.AUDITOR,
            ),
        )
        result = self._validate_current(project_id)
        project_service.record_event(
            aggregate_type="project",
            aggregate_id=project_id,
            event_type="project_passport_validated",
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload={
                "passport_version": result.passport.passport_version,
                "requirements_version_id": result.requirements_version_id,
                "finding_codes": [finding.code for finding in result.findings],
            },
        )
        return result

    def _validate_current(self, project_id: str) -> PassportValidationResult:
        requirements, requirements_version_id = self._requirements(project_id)
        rows = list(
            self.session.scalars(
                select(ProjectPassportFactRow)
                .where(
                    ProjectPassportFactRow.project_id == project_id,
                    ProjectPassportFactRow.is_current.is_(True),
                )
                .order_by(ProjectPassportFactRow.field_name)
            )
        )
        facts = tuple(
            PassportFact(
                field_name=row.field_name,
                value=row.payload.get("value"),
                unit=row.payload.get("unit"),
                observation_ids=tuple(row.payload.get("observation_ids", [])),
                status=VerificationStatus(row.status),
            )
            for row in rows
        )
        passport = ProjectPassport(
            project_id=project_id,
            facts=facts,
            passport_version=content_hash(
                {
                    "fact_ids": [row.id for row in rows],
                    "requirements_version_id": requirements_version_id,
                }
            ),
        )
        findings = validate_passport(
            passport,
            required_fields=requirements["required_fields"],
            independently_verified_fields=requirements["independently_verified_fields"],
        )
        now = utc_now()
        prior = list(
            self.session.scalars(
                select(VerificationFindingRow).where(
                    VerificationFindingRow.project_id == project_id,
                    VerificationFindingRow.contour == "PASSPORT",
                    VerificationFindingRow.resolved.is_(False),
                )
            )
        )
        for old_finding in prior:
            old_finding.resolved = True
            old_finding.updated_at = now
            old_finding.payload = {
                **old_finding.payload,
                "resolved_by_passport_validation": passport.passport_version,
                "resolved_at": now.isoformat(),
            }
        for finding in findings:
            identity = {
                "project_id": project_id,
                "contour": "PASSPORT",
                "finding": finding,
            }
            finding_id = f"finding-{content_hash(identity)[:24]}"
            existing = self.session.get(VerificationFindingRow, finding_id)
            payload = {
                **finding.model_dump(mode="json"),
                "passport_version": passport.passport_version,
                "requirements_version_id": requirements_version_id,
            }
            if existing is None:
                self.session.add(
                    VerificationFindingRow(
                        id=finding_id,
                        project_id=project_id,
                        contour="PASSPORT",
                        code=finding.code.value,
                        severity=Severity.BLOCKER.value,
                        resolved=False,
                        payload=payload,
                        created_at=now,
                        updated_at=now,
                    )
                )
            else:
                existing.resolved = False
                existing.payload = payload
                existing.updated_at = now
        return PassportValidationResult(
            passport=passport,
            findings=findings,
            requirements_version_id=requirements_version_id,
        )

    def _requirements(
        self,
        project_id: str,
    ) -> tuple[dict[str, frozenset[str]], str]:
        row = self.session.scalar(
            select(ControlledVersionRow)
            .join(
                ProjectControlledVersionRow,
                ProjectControlledVersionRow.controlled_version_id == ControlledVersionRow.id,
            )
            .where(
                ProjectControlledVersionRow.project_id == project_id,
                ProjectControlledVersionRow.purpose == "document_requirements",
                ControlledVersionRow.kind == "document_requirements",
                ControlledVersionRow.status == VersionStatus.APPROVED.value,
            )
        )
        if row is None:
            raise ValueError("A bound approved document_requirements version is required")
        passport = row.payload.get("passport")
        if not isinstance(passport, dict):
            raise ValueError("Document requirements lack a passport section")
        required = passport.get("required_fields")
        independent = passport.get("independently_verified_fields")
        if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
            raise ValueError("Passport required_fields must be an approved string list")
        if not isinstance(independent, list) or not all(
            isinstance(item, str) for item in independent
        ):
            raise ValueError(
                "Passport independently_verified_fields must be an approved string list"
            )
        return {
            "required_fields": frozenset(required),
            "independently_verified_fields": frozenset(independent),
        }, row.id

    def _observations(
        self,
        project_id: str,
        observation_ids: tuple[str, ...],
    ) -> tuple[ObservationRow, ...]:
        rows = tuple(
            self.session.scalars(
                select(ObservationRow).where(
                    ObservationRow.project_id == project_id,
                    ObservationRow.id.in_(observation_ids),
                )
            )
        )
        if len(rows) != len(set(observation_ids)):
            raise ValueError("One or more passport evidence observations are missing")
        return rows

    @staticmethod
    def _validate_observation_values(
        draft: PassportFactDraft,
        observations: tuple[ObservationRow, ...],
    ) -> None:
        value_hash = content_hash(draft.value)
        for row in observations:
            observation = row.payload.get("observation")
            if not isinstance(observation, dict):
                raise ValueError("Passport evidence observation payload is invalid")
            if content_hash(observation.get("value")) != value_hash:
                raise ValueError("Passport evidence observations do not reproduce the fact")
            if observation.get("unit") != draft.unit:
                raise ValueError("Passport evidence unit differs from the submitted fact")

    def _project_service(self) -> ProjectService:
        return ProjectService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        )

    @staticmethod
    def _view(row: ProjectPassportFactRow) -> PassportFactView:
        return PassportFactView(
            fact_id=row.id,
            field_name=row.field_name,
            value=row.payload.get("value"),
            unit=row.payload.get("unit"),
            observation_ids=tuple(row.payload.get("observation_ids", [])),
            status=VerificationStatus(row.status),
            supersedes_fact_id=row.supersedes_fact_id,
            is_current=row.is_current,
            created_by=str(row.payload.get("created_by")),
            verified_by=row.payload.get("verified_by"),
        )
