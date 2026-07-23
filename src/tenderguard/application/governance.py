from __future__ import annotations

from datetime import date
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from tenderguard.application.projects import ProjectService
from tenderguard.config import Settings
from tenderguard.domain.common import content_hash, ensure_utc, utc_now
from tenderguard.domain.enums import ActorRole, VersionStatus
from tenderguard.domain.models import ControlledVersion
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.object_store import ObjectStore
from tenderguard.infrastructure.orm import (
    AdapterQualificationRow,
    ControlledVersionRow,
    ProjectControlledVersionRow,
)


class GovernanceService:
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

    def create_version(
        self,
        *,
        actor: Actor,
        kind: str,
        version_label: str,
        payload: dict[str, Any],
        request_id: str,
        reason: str,
    ) -> ControlledVersion:
        self._require_owner(actor, kind)
        now = utc_now()
        version_id = f"version-{uuid4()}"
        governed_payload = {
            **payload,
            "_governance": {
                "organization_id": actor.organization_id,
                "created_by": actor.actor_id,
                "created_at": now.isoformat(),
            },
        }
        digest = content_hash(
            {
                "kind": kind,
                "version_label": version_label,
                "payload": governed_payload,
            }
        )
        row = ControlledVersionRow(
            id=version_id,
            kind=kind,
            version_label=version_label,
            content_hash=digest,
            status=VersionStatus.DRAFT.value,
            payload=governed_payload,
        )
        self.session.add(row)
        self.session.flush()
        ProjectService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        ).record_event(
            aggregate_type="controlled_version",
            aggregate_id=version_id,
            event_type="controlled_version_created",
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload={
                "kind": kind,
                "version_label": version_label,
                "content_hash": digest,
            },
        )
        return self._domain(row)

    def approve_version(
        self,
        *,
        actor: Actor,
        version_id: str,
        request_id: str,
        reason: str,
    ) -> ControlledVersion:
        row = self.session.scalar(
            select(ControlledVersionRow)
            .where(ControlledVersionRow.id == version_id)
            .with_for_update()
        )
        if row is None:
            raise LookupError(version_id)
        self._require_owner(actor, row.kind)
        governance = row.payload.get("_governance", {})
        if governance.get("organization_id") != actor.organization_id:
            raise LookupError(version_id)
        if governance.get("created_by") == actor.actor_id:
            raise ValueError("Controlled version requires four-eyes approval")
        if row.status != VersionStatus.DRAFT.value:
            raise ValueError("Only DRAFT controlled versions can be approved")
        row.status = VersionStatus.APPROVED.value
        row.approved_by = actor.actor_id
        row.approved_at = utc_now()
        ProjectService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        ).record_event(
            aggregate_type="controlled_version",
            aggregate_id=row.id,
            event_type="controlled_version_approved",
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload={"content_hash": row.content_hash, "kind": row.kind},
        )
        return self._domain(row)

    def bind_to_project(
        self,
        *,
        actor: Actor,
        project_id: str,
        version_id: str,
        purpose: str,
        request_id: str,
        reason: str,
    ) -> None:
        version = self.session.scalar(
            select(ControlledVersionRow).where(ControlledVersionRow.id == version_id)
        )
        if version is None:
            raise LookupError(version_id)
        self._require_owner(actor, version.kind)
        governance = version.payload.get("_governance", {})
        if governance.get("organization_id") != actor.organization_id:
            raise LookupError(version_id)
        if version.status != VersionStatus.APPROVED.value:
            raise ValueError("Only APPROVED controlled versions can be bound")
        project_service = ProjectService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        )
        project = project_service.get_project(actor=actor, project_id=project_id, lock=True)
        existing = self.session.scalar(
            select(ProjectControlledVersionRow).where(
                ProjectControlledVersionRow.project_id == project.id,
                ProjectControlledVersionRow.purpose == purpose,
            )
        )
        if existing:
            self.session.delete(existing)
            self.session.flush()
        self.session.add(
            ProjectControlledVersionRow(
                project_id=project.id,
                controlled_version_id=version.id,
                purpose=purpose,
                bound_by=actor.actor_id,
                bound_at=utc_now(),
            )
        )
        project.row_version += 1
        project.updated_at = utc_now()
        project_service.record_event(
            aggregate_type="project",
            aggregate_id=project.id,
            event_type="controlled_version_bound",
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload={
                "version_id": version.id,
                "kind": version.kind,
                "purpose": purpose,
                "content_hash": version.content_hash,
            },
        )

    def activate_adapter_qualification(
        self,
        *,
        actor: Actor,
        version_id: str,
        request_id: str,
        reason: str,
    ) -> AdapterQualificationRow:
        actor.require_any(ActorRole.METHODOLOGY_OWNER)
        version = self.session.scalar(
            select(ControlledVersionRow)
            .where(ControlledVersionRow.id == version_id)
            .with_for_update()
        )
        if version is None:
            raise LookupError(version_id)
        governance = version.payload.get("_governance", {})
        if governance.get("organization_id") != actor.organization_id:
            raise LookupError(version_id)
        if version.kind != "adapter_qualification":
            raise ValueError("Controlled version is not an adapter qualification")
        if version.status != VersionStatus.APPROVED.value or not version.approved_by:
            raise ValueError("Adapter qualification controlled version is not approved")
        payload = version.payload
        required = (
            "adapter_name",
            "adapter_version",
            "test_evidence_hash",
            "independence_domain",
        )
        if not all(isinstance(payload.get(field), str) and payload[field] for field in required):
            raise ValueError("Adapter qualification payload is incomplete")
        evidence_hash = str(payload["test_evidence_hash"])
        if len(evidence_hash) != 64 or any(
            character not in "0123456789abcdef" for character in evidence_hash
        ):
            raise ValueError("Adapter qualification evidence hash must be lowercase SHA-256")
        supported_methods = payload.get("supported_methods")
        if not isinstance(supported_methods, list) or not supported_methods:
            raise ValueError("Adapter qualification must declare supported methods")
        valid_until_raw = payload.get("valid_until")
        valid_until = date.fromisoformat(valid_until_raw) if valid_until_raw else None
        if valid_until is not None and valid_until < utc_now().date():
            raise ValueError("Expired adapter qualification cannot be activated")
        existing = self.session.get(AdapterQualificationRow, version.id)
        if existing is not None:
            return existing
        qualification = AdapterQualificationRow(
            id=version.id,
            adapter_name=str(payload["adapter_name"]),
            adapter_version=str(payload["adapter_version"]),
            status="APPROVED",
            valid_until=valid_until,
            test_evidence_hash=evidence_hash,
            payload={
                "controlled_version_id": version.id,
                "controlled_version_hash": version.content_hash,
                "supported_methods": supported_methods,
                "supported_price_evidence_classes": payload.get(
                    "supported_price_evidence_classes",
                    [],
                ),
                "independence_domain": payload["independence_domain"],
                "organization_id": actor.organization_id,
            },
            approved_by=version.approved_by,
            approved_at=version.approved_at or utc_now(),
        )
        self.session.add(qualification)
        ProjectService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        ).record_event(
            aggregate_type="adapter_qualification",
            aggregate_id=qualification.id,
            event_type="adapter_qualification_activated",
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload={
                "adapter_name": qualification.adapter_name,
                "adapter_version": qualification.adapter_version,
                "test_evidence_hash": qualification.test_evidence_hash,
                "valid_until": qualification.valid_until,
            },
        )
        return qualification

    @staticmethod
    def _require_owner(actor: Actor, kind: str) -> None:
        if kind in {
            "catalog",
            "nomenclature_catalog",
            "nomenclature_equivalence_rules",
            "equivalence_rules",
        }:
            actor.require_any(ActorRole.CATALOG_OWNER)
        else:
            actor.require_any(ActorRole.METHODOLOGY_OWNER)

    @staticmethod
    def _domain(row: ControlledVersionRow) -> ControlledVersion:
        return ControlledVersion(
            kind=row.kind,
            version_id=row.id,
            content_hash=row.content_hash,
            status=VersionStatus(row.status),
            approved_by=row.approved_by,
            approved_at=ensure_utc(row.approved_at),
        )
