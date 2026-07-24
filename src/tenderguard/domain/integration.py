from __future__ import annotations

import base64
import binascii
import hashlib
import re
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from pydantic import Field, SecretStr, field_validator, model_validator

from tenderguard.domain.common import canonical_json, content_hash, ensure_utc, utc_now
from tenderguard.domain.models import DomainModel

INTEGRATION_EVENT_SCHEMA_VERSION = "tenderguard.integration-event/v1"
INTEGRATION_RECEIPT_SCHEMA_VERSION = "tenderguard.integration-receipt/v1"
_TOPIC = re.compile(r"^[a-z0-9][a-z0-9._-]{0,199}$")


class IntegrationReceiptStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    DUPLICATE = "DUPLICATE"


class IntegrationSignature(DomainModel):
    algorithm: str
    key_id: str = Field(min_length=1, max_length=200)
    value_b64: str = Field(min_length=1, max_length=200)


class IntegrationEventBody(DomainModel):
    schema_version: str
    message_id: str = Field(min_length=1, max_length=128)
    delivery_deduplication_key: str = Field(min_length=1, max_length=200)
    topic: str = Field(min_length=1, max_length=200)
    aggregate_id: str = Field(min_length=1, max_length=128)
    organization_id: str = Field(min_length=1, max_length=64)
    occurred_at: datetime
    sent_at: datetime
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload: dict[str, Any]

    @field_validator("topic")
    @classmethod
    def topic_is_machine_safe(cls, value: str) -> str:
        if not _TOPIC.fullmatch(value):
            raise ValueError("Integration topic is invalid")
        return value

    @field_validator("occurred_at", "sent_at")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        normalized = ensure_utc(value)
        if normalized is None or value.tzinfo is None:
            raise ValueError("Integration timestamps must include a timezone")
        return normalized

    @model_validator(mode="after")
    def body_is_self_consistent(self) -> IntegrationEventBody:
        if self.schema_version != INTEGRATION_EVENT_SCHEMA_VERSION:
            raise ValueError("Unsupported integration event schema version")
        if content_hash(self.payload) != self.payload_hash:
            raise ValueError("Integration event payload hash does not match")
        return self


class SignedIntegrationEnvelope(DomainModel):
    body: IntegrationEventBody
    signature: IntegrationSignature


class IntegrationReceiptBody(DomainModel):
    schema_version: str
    source_message_id: str = Field(min_length=1, max_length=128)
    receiver_message_id: str = Field(min_length=1, max_length=128)
    delivery_deduplication_key: str = Field(min_length=1, max_length=200)
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    receiver_id: str = Field(min_length=1, max_length=200)
    status: IntegrationReceiptStatus
    received_at: datetime

    @field_validator("received_at")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        normalized = ensure_utc(value)
        if normalized is None or value.tzinfo is None:
            raise ValueError("Integration receipt timestamp must include a timezone")
        return normalized

    @model_validator(mode="after")
    def schema_is_supported(self) -> IntegrationReceiptBody:
        if self.schema_version != INTEGRATION_RECEIPT_SCHEMA_VERSION:
            raise ValueError("Unsupported integration receipt schema version")
        return self


class SignedIntegrationReceipt(DomainModel):
    body: IntegrationReceiptBody
    signature: IntegrationSignature


class IntegrationSigningMaterial(DomainModel):
    key_id: str = Field(min_length=1, max_length=200)
    private_key_b64: SecretStr
    public_key_b64: str
    public_key_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


def load_integration_signing_material(
    *,
    key_id: str,
    private_key_b64: str,
) -> IntegrationSigningMaterial:
    normalized_key_id = key_id.strip()
    if not normalized_key_id or normalized_key_id != key_id or len(key_id) > 200:
        raise ValueError("Integration signing key ID is invalid")
    private_key = _private_key(private_key_b64)
    public_bytes = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return IntegrationSigningMaterial(
        key_id=key_id,
        private_key_b64=private_key_b64,
        public_key_b64=base64.b64encode(public_bytes).decode("ascii"),
        public_key_fingerprint=hashlib.sha256(public_bytes).hexdigest(),
    )


def build_signed_integration_envelope(
    *,
    message_id: str,
    delivery_deduplication_key: str,
    topic: str,
    aggregate_id: str,
    organization_id: str,
    occurred_at: datetime,
    payload: dict[str, Any],
    signing_material: IntegrationSigningMaterial,
    sent_at: datetime | None = None,
) -> SignedIntegrationEnvelope:
    body = IntegrationEventBody(
        schema_version=INTEGRATION_EVENT_SCHEMA_VERSION,
        message_id=message_id,
        delivery_deduplication_key=delivery_deduplication_key,
        topic=topic,
        aggregate_id=aggregate_id,
        organization_id=organization_id,
        occurred_at=occurred_at,
        sent_at=sent_at or utc_now(),
        payload_hash=content_hash(payload),
        payload=payload,
    )
    return SignedIntegrationEnvelope(
        body=body,
        signature=_sign(body, signing_material),
    )


def verify_signed_integration_envelope(
    envelope: SignedIntegrationEnvelope,
    *,
    trusted_key_id: str,
    trusted_public_key_b64: str,
    expected_organization_id: str,
    allowed_topics: frozenset[str],
    now: datetime | None = None,
    max_message_age_seconds: int | None = None,
    max_future_skew_seconds: int | None = None,
) -> None:
    if envelope.signature.algorithm != "Ed25519":
        raise ValueError("Unsupported integration signature algorithm")
    if envelope.signature.key_id != trusted_key_id:
        raise ValueError("Integration signing key ID is not trusted")
    if envelope.body.organization_id != expected_organization_id:
        raise ValueError("Integration event belongs to another organization")
    if envelope.body.topic not in allowed_topics:
        raise ValueError("Integration topic is not qualified for this source")
    if content_hash(envelope.body.payload) != envelope.body.payload_hash:
        raise ValueError("Integration event payload hash does not match")
    _verify_signature(
        signature=envelope.signature,
        signed_value=envelope.body,
        trusted_public_key_b64=trusted_public_key_b64,
        label="integration event",
    )
    checked_at = ensure_utc(now or utc_now())
    sent_at = ensure_utc(envelope.body.sent_at)
    assert checked_at is not None and sent_at is not None
    if max_message_age_seconds is not None and sent_at < checked_at - timedelta(
        seconds=max_message_age_seconds
    ):
        raise ValueError("Integration event is older than the accepted delivery window")
    if max_future_skew_seconds is not None and sent_at > checked_at + timedelta(
        seconds=max_future_skew_seconds
    ):
        raise ValueError("Integration event timestamp is too far in the future")


def build_signed_integration_receipt(
    *,
    source_message_id: str,
    receiver_message_id: str,
    delivery_deduplication_key: str,
    payload_hash: str,
    receiver_id: str,
    status: IntegrationReceiptStatus,
    signing_material: IntegrationSigningMaterial,
    received_at: datetime | None = None,
) -> SignedIntegrationReceipt:
    body = IntegrationReceiptBody(
        schema_version=INTEGRATION_RECEIPT_SCHEMA_VERSION,
        source_message_id=source_message_id,
        receiver_message_id=receiver_message_id,
        delivery_deduplication_key=delivery_deduplication_key,
        payload_hash=payload_hash,
        receiver_id=receiver_id,
        status=status,
        received_at=received_at or utc_now(),
    )
    return SignedIntegrationReceipt(
        body=body,
        signature=_sign(body, signing_material),
    )


def verify_signed_integration_receipt(
    receipt: SignedIntegrationReceipt,
    *,
    envelope: SignedIntegrationEnvelope,
    trusted_key_id: str,
    trusted_public_key_b64: str,
    expected_receiver_id: str,
) -> None:
    if receipt.signature.algorithm != "Ed25519":
        raise ValueError("Unsupported integration receipt signature algorithm")
    if receipt.signature.key_id != trusted_key_id:
        raise ValueError("Integration receipt signing key ID is not trusted")
    if receipt.body.receiver_id != expected_receiver_id:
        raise ValueError("Integration receipt receiver is not trusted")
    if (
        receipt.body.source_message_id != envelope.body.message_id
        or receipt.body.delivery_deduplication_key != envelope.body.delivery_deduplication_key
        or receipt.body.payload_hash != envelope.body.payload_hash
    ):
        raise ValueError("Integration receipt does not acknowledge the delivered event")
    _verify_signature(
        signature=receipt.signature,
        signed_value=receipt.body,
        trusted_public_key_b64=trusted_public_key_b64,
        label="integration receipt",
    )


def integration_envelope_core_hash(envelope: SignedIntegrationEnvelope) -> str:
    """Hash identity and business payload while allowing a newly signed retry timestamp."""

    return content_hash(
        {
            "schema_version": envelope.body.schema_version,
            "message_id": envelope.body.message_id,
            "delivery_deduplication_key": envelope.body.delivery_deduplication_key,
            "topic": envelope.body.topic,
            "aggregate_id": envelope.body.aggregate_id,
            "organization_id": envelope.body.organization_id,
            "occurred_at": envelope.body.occurred_at,
            "payload_hash": envelope.body.payload_hash,
            "payload": envelope.body.payload,
        }
    )


def validate_integration_public_key(public_key_b64: str) -> None:
    _public_key(public_key_b64)


def _sign(value: DomainModel, material: IntegrationSigningMaterial) -> IntegrationSignature:
    signature = _private_key(material.private_key_b64.get_secret_value()).sign(
        canonical_json(value)
    )
    return IntegrationSignature(
        algorithm="Ed25519",
        key_id=material.key_id,
        value_b64=base64.b64encode(signature).decode("ascii"),
    )


def _verify_signature(
    *,
    signature: IntegrationSignature,
    signed_value: DomainModel,
    trusted_public_key_b64: str,
    label: str,
) -> None:
    signature_bytes = _decode_b64(
        signature.value_b64,
        expected_size=64,
        label=f"{label} signature",
    )
    try:
        _public_key(trusted_public_key_b64).verify(
            signature_bytes,
            canonical_json(signed_value),
        )
    except InvalidSignature as error:
        raise ValueError(f"Signed {label} verification failed") from error


def _private_key(value: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(
        _decode_b64(value, expected_size=32, label="integration private key")
    )


def _public_key(value: str) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(
        _decode_b64(value, expected_size=32, label="integration public key")
    )


def _decode_b64(value: str, *, expected_size: int, label: str) -> bytes:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ValueError(f"Invalid base64 {label}") from error
    if len(decoded) != expected_size:
        raise ValueError(f"{label.capitalize()} must contain {expected_size} bytes")
    return decoded
