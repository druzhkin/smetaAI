from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from tenderguard.application.projects import ProjectService
from tenderguard.config import Settings
from tenderguard.domain.access import project_role_mask
from tenderguard.domain.common import content_hash, ensure_utc, utc_now
from tenderguard.domain.enums import (
    ActorRole,
    ProjectAccessLevel,
    ProjectMembershipStatus,
    VersionStatus,
)
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.object_store import ObjectStore
from tenderguard.infrastructure.orm import (
    ApprovalTaskRow,
    ControlledVersionRow,
    DocumentSetRevisionRow,
    ProjectControlledVersionRow,
    ProjectMembershipRow,
)


def approval_task_updated_at(session: Session, task_id: str) -> datetime:
    task = session.get(ApprovalTaskRow, task_id)
    assert task is not None
    updated_at = ensure_utc(task.updated_at)
    assert updated_at is not None
    return updated_at


def project_memberships(
    project_id: str,
    actors: tuple[Actor, ...],
    *,
    owner_id: str,
    now: datetime,
) -> tuple[ProjectMembershipRow, ...]:
    roles_by_principal: dict[str, set[ActorRole]] = {}
    for actor in actors:
        roles_by_principal.setdefault(actor.actor_id, set()).update(
            role for role in actor.roles if role is not ActorRole.SYSTEM
        )
    return tuple(
        ProjectMembershipRow(
            id=f"membership-test-{content_hash((project_id, principal_id))[:24]}",
            project_id=project_id,
            principal_id=principal_id,
            roles=sorted(role.value for role in roles),
            role_mask=project_role_mask(roles),
            access_level=(
                ProjectAccessLevel.OWNER.value
                if principal_id == owner_id
                else ProjectAccessLevel.MEMBER.value
            ),
            status=ProjectMembershipStatus.ACTIVE.value,
            version=1,
            supersedes_membership_id=None,
            changed_by="test-fixture",
            reason="Explicit project access for integration fixture",
            created_at=now,
        )
        for principal_id, roles in sorted(roles_by_principal.items())
    )


def add_governed_controlled_version(
    *,
    session: Session,
    settings: Settings,
    object_store: ObjectStore,
    row: ControlledVersionRow,
    organization_id: str,
    creator: Actor,
    approver: Actor,
) -> None:
    assert creator.organization_id == organization_id
    assert approver.organization_id == organization_id
    created_at = utc_now()
    row.payload = {
        **row.payload,
        "_governance": {
            "organization_id": organization_id,
            "created_by": creator.actor_id,
            "created_at": created_at.isoformat(),
        },
    }
    row.content_hash = content_hash(
        {
            "kind": row.kind,
            "version_label": row.version_label,
            "payload": row.payload,
        }
    )
    row.status = VersionStatus.DRAFT.value
    row.approved_by = None
    row.approved_at = None
    session.add(row)
    session.flush()
    projects = ProjectService(
        session=session,
        settings=settings,
        object_store=object_store,
    )
    projects.record_event(
        aggregate_type="controlled_version",
        aggregate_id=row.id,
        event_type="controlled_version_created",
        actor=creator,
        request_id=f"fixture-create-{row.id}",
        reason="Create governed integration-test fixture",
        payload={
            "kind": row.kind,
            "version_label": row.version_label,
            "content_hash": row.content_hash,
        },
    )
    row.status = VersionStatus.APPROVED.value
    row.approved_by = approver.actor_id
    row.approved_at = utc_now()
    projects.record_event(
        aggregate_type="controlled_version",
        aggregate_id=row.id,
        event_type="controlled_version_approved",
        actor=approver,
        request_id=f"fixture-approve-{row.id}",
        reason="Independently approve governed integration-test fixture",
        payload={"content_hash": row.content_hash, "kind": row.kind},
    )


def add_project_controlled_version_binding(
    *,
    session: Session,
    settings: Settings,
    object_store: ObjectStore,
    project_id: str,
    version: ControlledVersionRow,
    purpose: str,
    actor: Actor,
) -> ProjectControlledVersionRow:
    bound_at = utc_now()
    binding = ProjectControlledVersionRow(
        project_id=project_id,
        controlled_version_id=version.id,
        purpose=purpose,
        bound_by=actor.actor_id,
        bound_at=bound_at,
    )
    session.add(binding)
    session.flush()
    ProjectService(
        session=session,
        settings=settings,
        object_store=object_store,
    ).record_event(
        aggregate_type="project",
        aggregate_id=project_id,
        event_type="controlled_version_bound",
        actor=actor,
        request_id=f"fixture-bind-{project_id}-{purpose}",
        reason="Bind governed integration-test fixture to the project",
        payload={
            "version_id": version.id,
            "kind": version.kind,
            "purpose": purpose,
            "content_hash": version.content_hash,
        },
    )
    return binding


def add_document_set_confirmation_audit(
    *,
    session: Session,
    settings: Settings,
    object_store: ObjectStore,
    row: DocumentSetRevisionRow,
    actor: Actor,
) -> None:
    assert row.confirmed_by == actor.actor_id
    assert row.confirmed_at is not None
    session.flush()
    ProjectService(
        session=session,
        settings=settings,
        object_store=object_store,
    ).record_event(
        aggregate_type="project",
        aggregate_id=row.project_id,
        event_type="document_set_confirmed",
        actor=actor,
        request_id=f"fixture-confirm-{row.id}",
        reason="Independently confirm integration-test document set",
        payload={
            "document_set_revision_id": row.id,
            "manifest_hash": row.manifest_hash,
            "revision_ids": row.revision_ids,
        },
    )
