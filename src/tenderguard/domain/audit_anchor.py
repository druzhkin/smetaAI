from __future__ import annotations

import base64
import binascii
from datetime import datetime

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import Field

from tenderguard.domain.common import canonical_json
from tenderguard.domain.models import DomainModel

AUDIT_CHECKPOINT_SCHEMA_VERSION = "tenderguard.audit-checkpoint/v1"
AUDIT_ANCHOR_RECEIPT_SCHEMA_VERSION = "tenderguard.audit-anchor-receipt/v1"


class AuditChainTerminal(DomainModel):
    aggregate_type: str = Field(min_length=1, max_length=100)
    aggregate_id: str = Field(min_length=1, max_length=128)
    sequence: int = Field(ge=1)
    event_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class AuditCheckpointManifest(DomainModel):
    schema_version: str
    checkpoint_id: str = Field(min_length=1, max_length=64)
    created_at: datetime
    event_count: int = Field(ge=1)
    terminals: tuple[AuditChainTerminal, ...] = Field(min_length=1)


class AuditAnchorStatement(DomainModel):
    schema_version: str
    provider_id: str = Field(min_length=1, max_length=200)
    provider_key_id: str = Field(min_length=1, max_length=200)
    checkpoint_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    anchored_at: datetime
    external_reference: str = Field(min_length=1, max_length=500)


def verify_anchor_signature(
    *,
    statement: AuditAnchorStatement,
    signature_b64: str,
    trusted_public_key_b64: str,
) -> None:
    if statement.schema_version != AUDIT_ANCHOR_RECEIPT_SCHEMA_VERSION:
        raise ValueError("Unsupported audit anchor receipt schema version")
    public_key = _decode_b64(
        trusted_public_key_b64,
        expected_size=32,
        label="audit anchor public key",
    )
    signature = _decode_b64(
        signature_b64,
        expected_size=64,
        label="audit anchor signature",
    )
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            signature,
            canonical_json(statement),
        )
    except InvalidSignature as error:
        raise ValueError("Audit anchor signature verification failed") from error


def validate_anchor_public_key(public_key_b64: str) -> None:
    _decode_b64(
        public_key_b64,
        expected_size=32,
        label="audit anchor public key",
    )


def _decode_b64(value: str, *, expected_size: int, label: str) -> bytes:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError(f"{label} is not valid base64") from error
    if len(decoded) != expected_size:
        raise ValueError(f"{label} must decode to {expected_size} bytes")
    return decoded
