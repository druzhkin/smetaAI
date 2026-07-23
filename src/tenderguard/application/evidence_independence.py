from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from tenderguard.domain.common import utc_now
from tenderguard.infrastructure.orm import AdapterQualificationRow, ObservationRow


def require_distinct_qualified_independence(
    session: Session,
    *,
    project_id: str,
    observations: Iterable[ObservationRow],
    minimum_sources: int = 2,
) -> tuple[str, ...]:
    """Return qualified leaf evidence IDs or fail closed on claimed independence."""

    pending = list(observations)
    leaves: dict[str, ObservationRow] = {}
    visited: set[str] = set()
    while pending:
        row = pending.pop()
        if row.id in visited:
            continue
        visited.add(row.id)
        if row.project_id != project_id:
            raise ValueError("Evidence independence cannot cross project boundaries")
        raw_sources = row.payload.get("source_observation_ids")
        if isinstance(raw_sources, list) and raw_sources:
            source_ids = {str(item) for item in raw_sources}
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
            pending.extend(source_rows)
            continue
        leaves[row.id] = row

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
    return tuple(sorted(leaves))
