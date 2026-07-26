from __future__ import annotations

from datetime import datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from tenderguard.config import Settings
from tenderguard.domain.audit import AuditEvent, verify_chain
from tenderguard.domain.common import content_hash, ensure_utc
from tenderguard.domain.enums import ActorRole, VersionStatus
from tenderguard.infrastructure.orm import (
    AuditEventRow,
    ControlledVersionRow,
    ProjectControlledVersionRow,
)


class ControlledVersionIntegrityError(ValueError):
    """Raised when a governed version cannot reproduce its approval basis."""


def controlled_version_owner_roles(kind: str) -> tuple[ActorRole, ...]:
    if kind in {
        "catalog",
        "nomenclature_catalog",
        "nomenclature_equivalence_rules",
        "equivalence_rules",
    }:
        return (ActorRole.CATALOG_OWNER,)
    return (ActorRole.METHODOLOGY_OWNER,)


def require_controlled_version_integrity(
    *,
    session: Session,
    settings: Settings,
    row: ControlledVersionRow,
    expected_organization_id: str | None = None,
    expected_kind: str | None = None,
    expected_content_hash: str | None = None,
    required_status: Literal["DRAFT", "APPROVED"] = "APPROVED",
) -> None:
    governance = row.payload.get("_governance")
    expected_row_hash = content_hash(
        {
            "kind": row.kind,
            "version_label": row.version_label,
            "payload": row.payload,
        }
    )
    if (
        not isinstance(governance, dict)
        or not isinstance(governance.get("organization_id"), str)
        or not governance["organization_id"]
        or not isinstance(governance.get("created_by"), str)
        or not governance["created_by"]
        or not isinstance(governance.get("created_at"), str)
        or (
            expected_organization_id is not None
            and governance["organization_id"] != expected_organization_id
        )
        or (expected_kind is not None and row.kind != expected_kind)
        or (expected_content_hash is not None and row.content_hash != expected_content_hash)
        or row.content_hash != expected_row_hash
        or row.status != required_status
    ):
        raise ControlledVersionIntegrityError(
            "Controlled version identity, content hash, organization, or status does not verify"
        )
    created_at = _parse_governance_timestamp(governance["created_at"])
    events = [
        _event(event)
        for event in session.scalars(
            select(AuditEventRow)
            .where(
                AuditEventRow.aggregate_type == "controlled_version",
                AuditEventRow.aggregate_id == row.id,
            )
            .order_by(AuditEventRow.sequence)
        )
    ]
    created = [event for event in events if event.event_type == "controlled_version_created"]
    approved = [event for event in events if event.event_type == "controlled_version_approved"]
    owner_role_values = {role.value for role in controlled_version_owner_roles(row.kind)}
    created_valid = bool(
        len(created) == 1
        and created[0].actor_id == governance["created_by"]
        and owner_role_values.intersection(created[0].actor_roles)
        and created[0].occurred_at >= created_at
        and created[0].payload
        == {
            "kind": row.kind,
            "version_label": row.version_label,
            "content_hash": row.content_hash,
        }
    )
    if (
        not events
        or not verify_chain(events, settings.audit_verification_keyring)
        or not created_valid
    ):
        raise ControlledVersionIntegrityError(
            "Controlled version creation audit chain does not verify"
        )
    if required_status == VersionStatus.DRAFT.value:
        if (
            len(events) != 1
            or approved
            or row.approved_by is not None
            or row.approved_at is not None
        ):
            raise ControlledVersionIntegrityError(
                "Draft controlled version has an invalid lifecycle"
            )
        return
    approved_at = ensure_utc(row.approved_at)
    if (
        len(events) != 2
        or len(approved) != 1
        or not row.approved_by
        or approved_at is None
        or row.approved_by == governance["created_by"]
        or approved[0].actor_id != row.approved_by
        or not owner_role_values.intersection(approved[0].actor_roles)
        or approved[0].occurred_at < approved_at
        or approved_at < created[0].occurred_at
        or approved[0].payload
        != {
            "content_hash": row.content_hash,
            "kind": row.kind,
        }
    ):
        raise ControlledVersionIntegrityError(
            "Controlled version approval or four-eyes audit chain does not verify"
        )


def controlled_version_integrity_valid(
    *,
    session: Session,
    settings: Settings,
    row: ControlledVersionRow,
    expected_organization_id: str | None = None,
    expected_kind: str | None = None,
    expected_content_hash: str | None = None,
    required_status: Literal["DRAFT", "APPROVED"] = "APPROVED",
) -> bool:
    try:
        require_controlled_version_integrity(
            session=session,
            settings=settings,
            row=row,
            expected_organization_id=expected_organization_id,
            expected_kind=expected_kind,
            expected_content_hash=expected_content_hash,
            required_status=required_status,
        )
    except (ArithmeticError, KeyError, LookupError, TypeError, ValueError):
        return False
    return True


def require_bound_controlled_version(
    *,
    session: Session,
    settings: Settings,
    project_id: str,
    organization_id: str,
    purpose: str,
    kind: str,
    expected_version_id: str | None = None,
) -> ControlledVersionRow:
    """Return one exact project binding only after reproducing its approval.

    The database uniqueness constraint is necessary but not sufficient: this
    check also rejects a missing binding, a purpose/kind substitution, a
    client-selected stale version, cross-organisation content, or an approval
    whose audit chain no longer verifies.
    """

    rows = list(
        session.scalars(
            select(ControlledVersionRow)
            .join(
                ProjectControlledVersionRow,
                ProjectControlledVersionRow.controlled_version_id == ControlledVersionRow.id,
            )
            .where(
                ProjectControlledVersionRow.project_id == project_id,
                ProjectControlledVersionRow.purpose == purpose,
            )
        )
    )
    if len(rows) != 1:
        raise ControlledVersionIntegrityError(
            f"Project must bind exactly one controlled version for purpose {purpose}"
        )
    row = rows[0]
    binding = session.scalar(
        select(ProjectControlledVersionRow).where(
            ProjectControlledVersionRow.project_id == project_id,
            ProjectControlledVersionRow.controlled_version_id == row.id,
            ProjectControlledVersionRow.purpose == purpose,
        )
    )
    if (
        binding is None
        or row.kind != kind
        or binding.project_id != project_id
        or binding.purpose != purpose
        or not binding.bound_by
        or ensure_utc(binding.bound_at) is None
        or (expected_version_id is not None and row.id != expected_version_id)
    ):
        raise ControlledVersionIntegrityError(
            "Controlled version binding identity, purpose, or requested version does not verify"
        )
    require_controlled_version_integrity(
        session=session,
        settings=settings,
        row=row,
        expected_organization_id=organization_id,
        expected_kind=kind,
    )
    project_events = [
        _event(event)
        for event in session.scalars(
            select(AuditEventRow)
            .where(
                AuditEventRow.aggregate_type == "project",
                AuditEventRow.aggregate_id == project_id,
            )
            .order_by(AuditEventRow.sequence)
        )
    ]
    purpose_events = [
        event
        for event in project_events
        if event.event_type == "controlled_version_bound"
        and event.payload.get("purpose") == purpose
    ]
    latest_binding_event = purpose_events[-1] if purpose_events else None
    owner_role_values = {role.value for role in controlled_version_owner_roles(row.kind)}
    bound_at = ensure_utc(binding.bound_at)
    if (
        not project_events
        or not verify_chain(project_events, settings.audit_verification_keyring)
        or latest_binding_event is None
        or bound_at is None
        or latest_binding_event.actor_id != binding.bound_by
        or not owner_role_values.intersection(latest_binding_event.actor_roles)
        or latest_binding_event.occurred_at < bound_at
        or latest_binding_event.payload
        != {
            "version_id": row.id,
            "kind": row.kind,
            "purpose": purpose,
            "content_hash": row.content_hash,
        }
    ):
        raise ControlledVersionIntegrityError(
            "Controlled version binding audit chain does not verify"
        )
    return row


def _parse_governance_timestamp(value: str) -> datetime:
    try:
        parsed = ensure_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError as error:
        raise ControlledVersionIntegrityError(
            "Controlled version creation timestamp is invalid"
        ) from error
    if parsed is None:
        raise ControlledVersionIntegrityError("Controlled version creation timestamp is missing")
    return parsed


def _event(row: AuditEventRow) -> AuditEvent:
    occurred_at = ensure_utc(row.occurred_at)
    if occurred_at is None:
        raise ControlledVersionIntegrityError("Controlled version audit timestamp is missing")
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
