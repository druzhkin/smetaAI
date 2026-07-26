from __future__ import annotations

import base64
import binascii
from datetime import datetime
from typing import Any, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import Field, field_validator, model_validator

from tenderguard.domain.common import canonical_json, content_hash
from tenderguard.domain.enums import ActorRole
from tenderguard.domain.models import DomainModel
from tenderguard.domain.operational_qualification import (
    IMMUTABLE_BUILD_REFERENCE_PATTERN,
    QualificationResultEnvelope,
)

PRODUCTION_GATE_EVIDENCE_PROFILE_SCHEMA = "tenderguard.production-gate-evidence-profile/v1"
PRODUCTION_GATE_EVIDENCE_STATEMENT_SCHEMA = "tenderguard.production-gate-evidence/v1"

ProductionEvidenceGate = Literal[
    "rules_and_catalog_calibration",
    "damaged_conflicting_document_resilience",
    "load_test",
    "security_review",
    "backup_restore",
    "methodology_approval",
]
ProductionEvidenceMode = Literal["INTERNAL_QUALIFICATION_RESULT", "EXTERNAL_ATTESTED_PACKAGE"]

PRODUCTION_EVIDENCE_GATES = frozenset(
    {
        "rules_and_catalog_calibration",
        "damaged_conflicting_document_resilience",
        "load_test",
        "security_review",
        "backup_restore",
        "methodology_approval",
    }
)
_INTERNAL_GATE_RESULT_TYPES = {
    "load_test": "LOAD",
    "backup_restore": "RECOVERY",
}
_HUMAN_APPROVER_ROLES = frozenset(
    {
        ActorRole.AUDITOR,
        ActorRole.METHODOLOGY_OWNER,
        ActorRole.ADMIN,
    }
)


def _reject_floats(value: Any) -> Any:
    if isinstance(value, float):
        raise ValueError("Floating-point values are forbidden in production evidence")
    if isinstance(value, dict):
        for item in value.values():
            _reject_floats(item)
    elif isinstance(value, list | tuple | set | frozenset):
        for item in value:
            _reject_floats(item)
    return value


def _normalized_identifier(value: str, *, label: str, maximum: int) -> str:
    if (
        not value
        or value != value.strip()
        or len(value) > maximum
        or not value[0].isupper()
        or any(
            not character.isupper() and not character.isdigit() and character != "_"
            for character in value
        )
    ):
        raise ValueError(f"{label} must be a normalized uppercase identifier")
    return value


def _decode_public_key(value: str) -> bytes:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("Evidence attester public key is not valid base64") from error
    if len(decoded) != 32:
        raise ValueError("Evidence attester public key must decode to 32 bytes")
    return decoded


def _decode_signature(value: str) -> bytes:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("Evidence attestation signature is not valid base64") from error
    if len(decoded) != 64:
        raise ValueError("Evidence attestation signature must decode to 64 bytes")
    return decoded


class ProductionGateEvidenceProfile(DomainModel):
    schema_version: Literal["tenderguard.production-gate-evidence-profile/v1"]
    gate_name: ProductionEvidenceGate
    expected_application_build_reference: str = Field(pattern=IMMUTABLE_BUILD_REFERENCE_PATTERN)
    allowed_environments: tuple[str, ...] = Field(min_length=1)
    maximum_evidence_age_days: int = Field(gt=0, le=3650)
    evidence_mode: ProductionEvidenceMode
    required_artifact_categories: tuple[str, ...] = Field(min_length=1)
    allowed_artifact_categories: tuple[str, ...] = Field(min_length=1)
    maximum_artifact_count: int = Field(gt=0, le=1000)
    maximum_artifact_bytes: int = Field(gt=0, le=10 * 1024 * 1024 * 1024)
    maximum_total_artifact_bytes: int = Field(gt=0, le=100 * 1024 * 1024 * 1024)
    required_claim_keys: tuple[str, ...] = Field(min_length=1)
    approval_roles: tuple[ActorRole, ...] = Field(min_length=1)
    source_profile_version_id: str | None = Field(default=None, max_length=64)
    source_profile_content_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    expected_qualification_type: Literal["LOAD", "RECOVERY"] | None = None
    trusted_attester_id: str | None = Field(default=None, max_length=200)
    trusted_attester_key_id: str | None = Field(default=None, max_length=200)
    trusted_attester_public_key_b64: str | None = Field(default=None, max_length=200)

    @model_validator(mode="before")
    @classmethod
    def floating_point_is_forbidden(cls, value: Any) -> Any:
        return _reject_floats(value)

    @field_validator("allowed_environments")
    @classmethod
    def normalized_environments(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(
            not item or item != item.strip() or len(item) > 100 for item in value
        ):
            raise ValueError("Evidence environments must be unique normalized strings")
        return value

    @field_validator(
        "required_artifact_categories",
        "allowed_artifact_categories",
        "required_claim_keys",
    )
    @classmethod
    def normalized_identifiers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("Evidence profile identifiers must be unique")
        return tuple(
            _normalized_identifier(item, label="Evidence profile value", maximum=100)
            for item in value
        )

    @field_validator(
        "trusted_attester_id",
        "trusted_attester_key_id",
        "source_profile_version_id",
    )
    @classmethod
    def normalized_optional_text(cls, value: str | None) -> str | None:
        if value is not None and (not value or value != value.strip()):
            raise ValueError("Evidence profile optional text must be normalized")
        return value

    @model_validator(mode="after")
    def profile_is_coherent(self) -> ProductionGateEvidenceProfile:
        if not set(self.required_artifact_categories) <= set(self.allowed_artifact_categories):
            raise ValueError("Required artifact categories must be allowed")
        if self.maximum_total_artifact_bytes < self.maximum_artifact_bytes:
            raise ValueError(
                "Total artifact byte limit cannot be smaller than the per-artifact limit"
            )
        if not set(self.approval_roles) <= _HUMAN_APPROVER_ROLES:
            raise ValueError(
                "Production evidence approval is restricted to independent control roles"
            )
        internal_result_type = _INTERNAL_GATE_RESULT_TYPES.get(self.gate_name)
        source_fields = (
            self.source_profile_version_id,
            self.source_profile_content_hash,
            self.expected_qualification_type,
        )
        attester_fields = (
            self.trusted_attester_id,
            self.trusted_attester_key_id,
            self.trusted_attester_public_key_b64,
        )
        if self.evidence_mode == "INTERNAL_QUALIFICATION_RESULT":
            if internal_result_type is None:
                raise ValueError("This production gate has no internal qualification runner")
            if not all(source_fields) or any(attester_fields):
                raise ValueError(
                    "Internal evidence requires an exact source profile and no external key"
                )
            if self.expected_qualification_type != internal_result_type:
                raise ValueError("Internal evidence type does not match the production gate")
            if "QUALIFICATION_RESULT" not in self.required_artifact_categories:
                raise ValueError("Internal evidence must retain its qualification result artifact")
        else:
            if internal_result_type is not None:
                raise ValueError(
                    "Load and recovery gates require their internal qualification results"
                )
            if any(source_fields) or not all(attester_fields):
                raise ValueError(
                    "External evidence requires a complete trusted attester and no source profile"
                )
            assert self.trusted_attester_public_key_b64 is not None
            _decode_public_key(self.trusted_attester_public_key_b64)
        return self


class ProductionEvidenceArtifact(DomainModel):
    category: str = Field(min_length=1, max_length=100)
    object_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)
    media_type: str = Field(min_length=1, max_length=200)
    source_reference: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="before")
    @classmethod
    def floating_point_is_forbidden(cls, value: Any) -> Any:
        return _reject_floats(value)

    @field_validator("category")
    @classmethod
    def normalized_category(cls, value: str) -> str:
        return _normalized_identifier(value, label="Artifact category", maximum=100)

    @field_validator("media_type", "source_reference")
    @classmethod
    def normalized_text(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("Artifact metadata must not contain surrounding whitespace")
        return value


class ProductionGateEvidenceSubmission(DomainModel):
    profile_version_id: str = Field(min_length=1, max_length=64)
    profile_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    environment: str = Field(min_length=1, max_length=100)
    executed_by: str = Field(min_length=1, max_length=200)
    started_at: datetime
    completed_at: datetime
    artifacts: tuple[ProductionEvidenceArtifact, ...] = Field(min_length=1)
    claims: dict[str, str | int | bool] = Field(min_length=1)
    technical_result: QualificationResultEnvelope | None = None
    signed_statement_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    attester_id: str | None = Field(default=None, max_length=200)
    attester_key_id: str | None = Field(default=None, max_length=200)
    attestation_signature_b64: str | None = Field(default=None, max_length=200)

    @model_validator(mode="before")
    @classmethod
    def floating_point_is_forbidden(cls, value: Any) -> Any:
        return _reject_floats(value)

    @field_validator("environment", "executed_by")
    @classmethod
    def normalized_text(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("Evidence submission text must not contain surrounding whitespace")
        return value

    @field_validator("started_at", "completed_at")
    @classmethod
    def timezone_is_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Evidence timestamps must include a timezone")
        return value

    @field_validator("claims")
    @classmethod
    def normalized_claims(
        cls,
        value: dict[str, str | int | bool],
    ) -> dict[str, str | int | bool]:
        for key, item in value.items():
            _normalized_identifier(key, label="Evidence claim key", maximum=100)
            if isinstance(item, str) and (not item or item != item.strip() or len(item) > 4000):
                raise ValueError("Evidence string claims must be normalized and bounded")
            if isinstance(item, int) and not isinstance(item, bool) and abs(item) > 10**18:
                raise ValueError("Evidence integer claim exceeds the supported range")
        return value

    @model_validator(mode="after")
    def submission_is_coherent(self) -> ProductionGateEvidenceSubmission:
        if self.started_at > self.completed_at:
            raise ValueError("Evidence completion cannot precede its start")
        identities = [(artifact.category, artifact.object_hash) for artifact in self.artifacts]
        if len(identities) != len(set(identities)):
            raise ValueError("Evidence artifacts must have unique category/hash identities")
        attestation_fields = (
            self.signed_statement_hash,
            self.attester_id,
            self.attester_key_id,
            self.attestation_signature_b64,
        )
        if any(attestation_fields) and not all(attestation_fields):
            raise ValueError("External evidence attestation is incomplete")
        if self.attestation_signature_b64 is not None:
            _decode_signature(self.attestation_signature_b64)
        return self


class ProductionGateEvidenceStatement(DomainModel):
    schema_version: Literal["tenderguard.production-gate-evidence/v1"]
    organization_id: str = Field(min_length=1, max_length=64)
    gate_name: ProductionEvidenceGate
    profile_version_id: str = Field(min_length=1, max_length=64)
    profile_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    application_build_reference: str = Field(pattern=IMMUTABLE_BUILD_REFERENCE_PATTERN)
    environment: str = Field(min_length=1, max_length=100)
    executed_by: str = Field(min_length=1, max_length=200)
    started_at: datetime
    completed_at: datetime
    artifacts: tuple[ProductionEvidenceArtifact, ...]
    claims: dict[str, str | int | bool]
    technical_result_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="before")
    @classmethod
    def floating_point_is_forbidden(cls, value: Any) -> Any:
        return _reject_floats(value)

    @property
    def statement_hash(self) -> str:
        return content_hash(self)


def verify_production_evidence_signature(
    *,
    statement: ProductionGateEvidenceStatement,
    signature_b64: str,
    trusted_public_key_b64: str,
) -> None:
    try:
        Ed25519PublicKey.from_public_bytes(_decode_public_key(trusted_public_key_b64)).verify(
            _decode_signature(signature_b64),
            canonical_json(statement),
        )
    except InvalidSignature as error:
        raise ValueError("Production evidence attestation signature verification failed") from error
