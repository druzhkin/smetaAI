from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from tenderguard.domain.common import content_hash, signed_hash
from tenderguard.domain.models import DomainModel

GENESIS_HASH = "0" * 64


class AuditEvent(DomainModel):
    sequence: int = Field(ge=1)
    event_id: str
    aggregate_type: str
    aggregate_id: str
    event_type: str
    actor_id: str
    actor_roles: tuple[str, ...]
    request_id: str
    reason: str
    occurred_at: datetime
    payload: dict[str, Any]
    previous_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    event_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    signature: str = Field(pattern=r"^[0-9a-f]{64}$")


def _unsigned_record(
    *,
    sequence: int,
    event_id: str,
    aggregate_type: str,
    aggregate_id: str,
    event_type: str,
    actor_id: str,
    actor_roles: tuple[str, ...],
    request_id: str,
    reason: str,
    occurred_at: datetime,
    payload: dict[str, Any],
    previous_hash: str,
) -> dict[str, Any]:
    return {
        "sequence": sequence,
        "event_id": event_id,
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "event_type": event_type,
        "actor_id": actor_id,
        "actor_roles": actor_roles,
        "request_id": request_id,
        "reason": reason,
        "occurred_at": occurred_at,
        "payload": payload,
        "previous_hash": previous_hash,
    }


def append_event(
    *,
    previous: AuditEvent | None,
    event_id: str,
    aggregate_type: str,
    aggregate_id: str,
    event_type: str,
    actor_id: str,
    actor_roles: tuple[str, ...],
    request_id: str,
    reason: str,
    occurred_at: datetime,
    payload: dict[str, Any],
    signing_key: bytes,
) -> AuditEvent:
    sequence = 1 if previous is None else previous.sequence + 1
    previous_hash = GENESIS_HASH if previous is None else previous.event_hash
    unsigned = _unsigned_record(
        sequence=sequence,
        event_id=event_id,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        event_type=event_type,
        actor_id=actor_id,
        actor_roles=actor_roles,
        request_id=request_id,
        reason=reason,
        occurred_at=occurred_at,
        payload=payload,
        previous_hash=previous_hash,
    )
    event_hash = content_hash(unsigned)
    signature = signed_hash({"event_hash": event_hash}, signing_key)
    return AuditEvent(
        **unsigned,
        event_hash=event_hash,
        signature=signature,
    )


def verify_chain(events: list[AuditEvent], signing_key: bytes) -> bool:
    previous_hash = GENESIS_HASH
    expected_sequence = 1
    for event in events:
        if event.sequence != expected_sequence or event.previous_hash != previous_hash:
            return False
        unsigned = _unsigned_record(
            sequence=event.sequence,
            event_id=event.event_id,
            aggregate_type=event.aggregate_type,
            aggregate_id=event.aggregate_id,
            event_type=event.event_type,
            actor_id=event.actor_id,
            actor_roles=event.actor_roles,
            request_id=event.request_id,
            reason=event.reason,
            occurred_at=event.occurred_at,
            payload=event.payload,
            previous_hash=event.previous_hash,
        )
        if content_hash(unsigned) != event.event_hash:
            return False
        if signed_hash({"event_hash": event.event_hash}, signing_key) != event.signature:
            return False
        previous_hash = event.event_hash
        expected_sequence += 1
    return True
