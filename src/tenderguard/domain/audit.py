from __future__ import annotations

import hmac
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from tenderguard.domain.common import content_hash, signed_hash
from tenderguard.domain.models import DomainModel

GENESIS_HASH = "0" * 64
AUDIT_SIGNATURE_V1 = "HMAC-SHA256-V1"
AUDIT_SIGNATURE_V2 = "HMAC-SHA256-V2"


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
    signing_key_id: str = Field(min_length=1, max_length=200)
    signature_version: Literal["HMAC-SHA256-V1", "HMAC-SHA256-V2"]
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
    signing_key_id: str,
    signature_version: str,
) -> dict[str, Any]:
    record = {
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
    if signature_version == AUDIT_SIGNATURE_V2:
        record["signing_key_id"] = signing_key_id
        record["signature_version"] = signature_version
    elif signature_version != AUDIT_SIGNATURE_V1:
        raise ValueError("Unsupported audit signature version")
    return record


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
    signing_key_id: str,
) -> AuditEvent:
    normalized_key_id = signing_key_id.strip()
    if not normalized_key_id or normalized_key_id != signing_key_id:
        raise ValueError("Audit signing key ID is invalid")
    sequence = 1 if previous is None else previous.sequence + 1
    previous_hash = GENESIS_HASH if previous is None else previous.event_hash
    signature_version = AUDIT_SIGNATURE_V2
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
        signing_key_id=normalized_key_id,
        signature_version=signature_version,
    )
    event_hash = content_hash(unsigned)
    signature = signed_hash({"event_hash": event_hash}, signing_key)
    return AuditEvent(
        **unsigned,
        event_hash=event_hash,
        signature=signature,
    )


def verify_chain(
    events: list[AuditEvent],
    verification_keys: Mapping[str, bytes],
) -> bool:
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
            signing_key_id=event.signing_key_id,
            signature_version=event.signature_version,
        )
        if content_hash(unsigned) != event.event_hash:
            return False
        signing_key = verification_keys.get(event.signing_key_id)
        if signing_key is None:
            return False
        expected_signature = signed_hash({"event_hash": event.event_hash}, signing_key)
        if not hmac.compare_digest(expected_signature, event.signature):
            return False
        previous_hash = event.event_hash
        expected_sequence += 1
    return True
