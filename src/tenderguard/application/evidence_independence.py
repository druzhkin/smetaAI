from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from tenderguard.domain.common import utc_now
from tenderguard.domain.enums import EvidenceMethod, VerificationStatus
from tenderguard.domain.models import Observation
from tenderguard.infrastructure.orm import (
    AdapterQualificationRow,
    ObservationRow,
    ProjectRow,
)


def resolve_observation_leaves(
    session: Session,
    *,
    project_id: str,
    observations: Iterable[ObservationRow],
) -> tuple[ObservationRow, ...]:
    """Resolve an evidence graph to project-scoped immutable leaf rows."""

    project = session.get(ProjectRow, project_id)
    if project is None:
        raise ValueError("Evidence independence project does not exist")
    pending = [(row, False) for row in observations]
    leaves: dict[str, ObservationRow] = {}
    expanded: set[str] = set()
    active: set[str] = set()
    while pending:
        row, exiting = pending.pop()
        if exiting:
            active.remove(row.id)
            expanded.add(row.id)
            continue
        if row.id in active:
            raise ValueError("Derived evidence graph contains a cycle")
        if row.id in expanded:
            continue
        if row.project_id != project_id:
            raise ValueError("Evidence independence cannot cross project boundaries")
        raw_sources = row.payload.get("source_observation_ids")
        if raw_sources is not None:
            if (
                not isinstance(raw_sources, list)
                or not raw_sources
                or not all(isinstance(item, str) and item for item in raw_sources)
                or len(raw_sources) != len(set(raw_sources))
            ):
                raise ValueError("Derived evidence source identities are invalid")
            source_ids = set(raw_sources)
            source_rows = list(
                session.scalars(
                    select(ObservationRow).where(
                        ObservationRow.project_id == project_id,
                        ObservationRow.id.in_(source_ids),
                    )
                )
            )
            if len(source_rows) != len(source_ids):
                raise ValueError("Derived evidence has missing source observations")
            active.add(row.id)
            pending.append((row, True))
            pending.extend((source, False) for source in source_rows)
            continue
        leaves[row.id] = row
        expanded.add(row.id)
    if not leaves:
        raise ValueError("Derived evidence graph has no leaf observations")
    return tuple(leaves[item] for item in sorted(leaves))


def require_distinct_qualified_independence(
    session: Session,
    *,
    project_id: str,
    observations: Iterable[ObservationRow],
    minimum_sources: int = 2,
) -> tuple[str, ...]:
    """Return qualified leaf evidence IDs or fail closed on claimed independence."""

    project = session.get(ProjectRow, project_id)
    if project is None:
        raise ValueError("Evidence independence project does not exist")
    leaf_rows = resolve_observation_leaves(
        session,
        project_id=project_id,
        observations=observations,
    )
    leaves = {row.id: row for row in leaf_rows}
    if len(leaves) < minimum_sources:
        raise ValueError(
            f"Critical evidence requires at least {minimum_sources} independent sources"
        )
    qualification_ids: list[str] = []
    for row in leaves.values():
        qualification_id = row.payload.get("adapter_qualification_id")
        if not isinstance(qualification_id, str) or not qualification_id:
            raise ValueError("Critical evidence source lacks a qualified independence domain")
        qualification_ids.append(qualification_id)
    if len(set(qualification_ids)) != len(qualification_ids):
        raise ValueError("Critical evidence reuses an adapter qualification")
    qualifications = list(
        session.scalars(
            select(AdapterQualificationRow).where(
                AdapterQualificationRow.id.in_(qualification_ids),
                AdapterQualificationRow.status == "APPROVED",
            )
        )
    )
    if len(qualifications) != len(qualification_ids):
        raise ValueError("Critical evidence uses an unapproved adapter qualification")
    qualifications_by_id = {row.id: row for row in qualifications}
    domains: set[str] = set()
    for qualification in qualifications:
        if qualification.valid_until and qualification.valid_until < utc_now().date():
            raise ValueError("Critical evidence uses an expired adapter qualification")
        domain = qualification.payload.get("independence_domain")
        if not isinstance(domain, str) or not domain:
            raise ValueError("Adapter qualification lacks an independence domain")
        domains.add(domain)
    if len(domains) != len(qualifications):
        raise ValueError("Critical evidence sources are not independently qualified")
    for row in leaves.values():
        qualification_id = row.payload.get("adapter_qualification_id")
        matched_qualification = qualifications_by_id.get(str(qualification_id))
        try:
            observation = Observation.model_validate(row.payload.get("observation"))
        except ValueError as error:
            raise ValueError("Critical evidence source payload is invalid") from error
        if (
            matched_qualification is None
            or row.id != observation.observation_id
            or row.project_id != project_id
            or row.document_revision_id != observation.location.document_revision_id
            or row.field_name != observation.field_name
            or row.method != observation.method.value
            or row.method_version != observation.method_version
            or row.status != observation.status.value
            or observation.status
            not in {VerificationStatus.UNVERIFIED, VerificationStatus.VERIFIED}
            or observation.method in {EvidenceMethod.MANUAL, EvidenceMethod.RULE_ENGINE}
            or matched_qualification.adapter_version != observation.method_version
            or observation.method.value
            not in matched_qualification.payload.get("supported_methods", [])
            or matched_qualification.payload.get("organization_id") != project.organization_id
            or matched_qualification.payload.get("service_actor_id") != observation.actor_id
        ):
            raise ValueError(
                "Critical evidence source no longer matches its qualified adapter identity"
            )
    return tuple(leaves)
