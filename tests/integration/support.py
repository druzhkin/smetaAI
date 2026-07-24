from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from tenderguard.domain.access import project_role_mask
from tenderguard.domain.common import content_hash, ensure_utc
from tenderguard.domain.enums import ActorRole, ProjectAccessLevel, ProjectMembershipStatus
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.orm import ApprovalTaskRow, ProjectMembershipRow


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
