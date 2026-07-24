from __future__ import annotations

from collections.abc import Iterable
from types import MappingProxyType

from tenderguard.domain.enums import ActorRole

PROJECT_ROLE_BITS = MappingProxyType(
    {
        ActorRole.ESTIMATOR: 1 << 0,
        ActorRole.PROCUREMENT: 1 << 1,
        ActorRole.TECHNICAL_EXPERT: 1 << 2,
        ActorRole.REVIEWER: 1 << 3,
        ActorRole.APPROVER: 1 << 4,
        ActorRole.METHODOLOGY_OWNER: 1 << 5,
        ActorRole.CATALOG_OWNER: 1 << 6,
        ActorRole.AUDITOR: 1 << 7,
        ActorRole.ADMIN: 1 << 8,
    }
)
PROJECT_ROLE_MASK_MAX = sum(PROJECT_ROLE_BITS.values())


def project_role_bit(role: ActorRole) -> int:
    try:
        return PROJECT_ROLE_BITS[role]
    except KeyError as error:
        raise ValueError(f"{role.value} is not a project membership role") from error


def project_role_mask(roles: Iterable[ActorRole]) -> int:
    mask = 0
    for role in roles:
        mask |= project_role_bit(role)
    if mask == 0:
        raise ValueError("At least one project membership role is required")
    return mask


def validate_project_role_evidence(
    raw_roles: object,
    stored_mask: object,
) -> frozenset[ActorRole]:
    if (
        not isinstance(raw_roles, list)
        or not raw_roles
        or any(not isinstance(value, str) for value in raw_roles)
        or len(set(raw_roles)) != len(raw_roles)
    ):
        raise ValueError("Project membership role evidence is malformed")
    try:
        roles = frozenset(ActorRole(value) for value in raw_roles)
    except ValueError as error:
        raise ValueError("Project membership contains an unknown role") from error
    if ActorRole.SYSTEM in roles:
        raise ValueError("Project membership cannot contain the SYSTEM role")
    if isinstance(stored_mask, bool) or not isinstance(stored_mask, int):
        raise ValueError("Project membership role mask is malformed")
    if project_role_mask(roles) != stored_mask:
        raise ValueError("Project membership role mask does not match its role set")
    return roles
