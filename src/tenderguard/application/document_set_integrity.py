from __future__ import annotations

from collections.abc import Collection

from sqlalchemy import select
from sqlalchemy.orm import Session

from tenderguard.config import Settings
from tenderguard.domain.audit import AuditEvent, verify_chain
from tenderguard.domain.common import content_hash, ensure_utc
from tenderguard.domain.enums import ActorRole
from tenderguard.infrastructure.orm import AuditEventRow, DocumentSetRevisionRow


class DocumentSetIntegrityError(ValueError):
    """Raised when a purported current document set cannot be reproduced."""


def require_confirmed_document_set_integrity(
    *,
    session: Session,
    settings: Settings,
    project_id: str,
    document_set_revision_id: str | None,
) -> DocumentSetRevisionRow:
    """Return the exact confirmed set only after manifest and four-eyes checks."""

    if not document_set_revision_id:
        raise DocumentSetIntegrityError("A confirmed current document set is required")
    row = session.scalar(
        select(DocumentSetRevisionRow).where(
            DocumentSetRevisionRow.id == document_set_revision_id,
            DocumentSetRevisionRow.project_id == project_id,
            DocumentSetRevisionRow.status == "CONFIRMED",
        )
    )
    if row is None:
        raise DocumentSetIntegrityError("A confirmed current document set is required")
    revision_ids = row.revision_ids
    created_at = ensure_utc(row.created_at)
    confirmed_at = ensure_utc(row.confirmed_at)
    if (
        not isinstance(revision_ids, list)
        or not revision_ids
        or any(
            not isinstance(revision_id, str) or not revision_id or len(revision_id) > 64
            for revision_id in revision_ids
        )
        or len(set(revision_ids)) != len(revision_ids)
        or row.manifest_hash != content_hash(revision_ids)
        or not row.created_by
        or not row.confirmed_by
        or created_at is None
        or confirmed_at is None
        or row.confirmed_by == row.created_by
        or confirmed_at < created_at
    ):
        raise DocumentSetIntegrityError(
            "Confirmed current document set manifest and four-eyes evidence do not verify"
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
    confirmation_events = [
        event for event in project_events if event.event_type == "document_set_confirmed"
    ]
    latest_confirmation = confirmation_events[-1] if confirmation_events else None
    confirmation_roles = {ActorRole.REVIEWER.value, ActorRole.APPROVER.value}
    if (
        not project_events
        or not verify_chain(project_events, settings.audit_verification_keyring)
        or latest_confirmation is None
        or latest_confirmation.actor_id != row.confirmed_by
        or not confirmation_roles.intersection(latest_confirmation.actor_roles)
        or latest_confirmation.occurred_at < confirmed_at
        or latest_confirmation.payload
        != {
            "document_set_revision_id": row.id,
            "manifest_hash": row.manifest_hash,
            "revision_ids": row.revision_ids,
        }
    ):
        raise DocumentSetIntegrityError(
            "Confirmed current document set audit chain does not verify"
        )
    return row


def require_observation_in_document_set(
    *,
    document_revision_ids: Collection[str],
    document_revision_id: str,
) -> None:
    if document_revision_id not in document_revision_ids:
        raise DocumentSetIntegrityError(
            "Evidence observation does not belong to the confirmed current document set"
        )


def _event(row: AuditEventRow) -> AuditEvent:
    occurred_at = ensure_utc(row.occurred_at)
    if occurred_at is None:
        raise DocumentSetIntegrityError("Document-set audit timestamp is missing")
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
