from __future__ import annotations

import base64
import binascii
import hashlib
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from pydantic import Field, SecretStr

from tenderguard.domain.common import canonical_json, content_hash
from tenderguard.domain.models import DomainModel

EXPORT_SCHEMA_VERSION = "tenderguard.signed-estimate-audit/v1"
EXPORT_FORMAT = "TENDERGUARD_SIGNED_JSON"
EXPORT_MEDIA_TYPE = "application/vnd.tenderguard.signed-estimate-audit+json"
REQUIRED_CONTENT_NAMES = frozenset(
    {
        "approvals.json",
        "audit_chain.json",
        "controlled_versions.json",
        "lineage.json",
        "project.json",
        "release_decision.json",
        "snapshot.json",
        "workflow.json",
    }
)


class ExportContentEntry(DomainModel):
    name: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExportManifest(DomainModel):
    schema_version: str
    format: str
    project_id: str
    organization_id: str
    project_code: str
    snapshot_id: str
    snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_set_revision_id: str
    release_decision_id: str
    release_state: str
    template_version_id: str
    audit_cutoff_event_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_entries: tuple[ExportContentEntry, ...]


class ExportSignature(DomainModel):
    algorithm: str
    key_id: str
    public_key_b64: str
    public_key_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    value_b64: str


class SignedExportPackage(DomainModel):
    manifest: ExportManifest
    contents: dict[str, Any]
    signature: ExportSignature


class ExportSigningMaterial(DomainModel):
    key_id: str
    private_key_b64: SecretStr
    public_key_b64: str
    public_key_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


def build_content_entries(contents: dict[str, Any]) -> tuple[ExportContentEntry, ...]:
    return tuple(
        ExportContentEntry(name=name, sha256=content_hash(contents[name]))
        for name in sorted(contents)
    )


def load_signing_material(*, key_id: str, private_key_b64: str) -> ExportSigningMaterial:
    if not key_id.strip():
        raise ValueError("Export signing key ID is empty")
    private_key = _private_key(private_key_b64)
    public_bytes = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return ExportSigningMaterial(
        key_id=key_id,
        private_key_b64=private_key_b64,
        public_key_b64=base64.b64encode(public_bytes).decode("ascii"),
        public_key_fingerprint=hashlib.sha256(public_bytes).hexdigest(),
    )


def build_signed_export_package(
    *,
    manifest: ExportManifest,
    contents: dict[str, Any],
    signing_material: ExportSigningMaterial,
) -> SignedExportPackage:
    _validate_manifest_contents(manifest, contents)
    manifest_bytes = canonical_json(manifest)
    signature = _private_key(signing_material.private_key_b64.get_secret_value()).sign(
        manifest_bytes
    )
    return SignedExportPackage(
        manifest=manifest,
        contents=contents,
        signature=ExportSignature(
            algorithm="Ed25519",
            key_id=signing_material.key_id,
            public_key_b64=signing_material.public_key_b64,
            public_key_fingerprint=signing_material.public_key_fingerprint,
            manifest_hash=content_hash(manifest),
            value_b64=base64.b64encode(signature).decode("ascii"),
        ),
    )


def verify_signed_export_package(
    package: SignedExportPackage,
    *,
    trusted_public_key_b64: str | None = None,
    trusted_key_id: str | None = None,
) -> None:
    if package.manifest.schema_version != EXPORT_SCHEMA_VERSION:
        raise ValueError("Unsupported signed export schema version")
    if package.manifest.format != EXPORT_FORMAT:
        raise ValueError("Unsupported signed export format")
    if package.signature.algorithm != "Ed25519":
        raise ValueError("Unsupported signed export signature algorithm")
    if trusted_key_id is not None and package.signature.key_id != trusted_key_id:
        raise ValueError("Signed export key ID is not trusted")
    _validate_manifest_contents(package.manifest, package.contents)
    expected_manifest_hash = content_hash(package.manifest)
    if package.signature.manifest_hash != expected_manifest_hash:
        raise ValueError("Signed export manifest hash does not match")
    public_bytes = _decode_b64(
        package.signature.public_key_b64,
        expected_size=32,
        label="export public key",
    )
    if hashlib.sha256(public_bytes).hexdigest() != package.signature.public_key_fingerprint:
        raise ValueError("Signed export public-key fingerprint does not match")
    if trusted_public_key_b64 is not None:
        trusted_public_bytes = _decode_b64(
            trusted_public_key_b64,
            expected_size=32,
            label="trusted export public key",
        )
        if public_bytes != trusted_public_bytes:
            raise ValueError("Signed export public key is not trusted")
    signature = _decode_b64(
        package.signature.value_b64,
        expected_size=64,
        label="export signature",
    )
    try:
        Ed25519PublicKey.from_public_bytes(public_bytes).verify(
            signature,
            canonical_json(package.manifest),
        )
    except InvalidSignature as error:
        raise ValueError("Signed export signature verification failed") from error


def _validate_manifest_contents(
    manifest: ExportManifest,
    contents: dict[str, Any],
) -> None:
    content_names = frozenset(contents)
    if content_names != REQUIRED_CONTENT_NAMES:
        missing = sorted(REQUIRED_CONTENT_NAMES - content_names)
        extra = sorted(content_names - REQUIRED_CONTENT_NAMES)
        raise ValueError(f"Signed export content set is invalid; missing={missing}, extra={extra}")
    expected_entries = build_content_entries(contents)
    if manifest.content_entries != expected_entries:
        raise ValueError("Signed export content hashes do not match the manifest")


def _private_key(private_key_b64: str) -> Ed25519PrivateKey:
    private_bytes = _decode_b64(
        private_key_b64,
        expected_size=32,
        label="export private key",
    )
    return Ed25519PrivateKey.from_private_bytes(private_bytes)


def _decode_b64(value: str, *, expected_size: int, label: str) -> bytes:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ValueError(f"Invalid base64 {label}") from error
    if len(decoded) != expected_size:
        raise ValueError(f"{label.capitalize()} must contain {expected_size} bytes")
    return decoded
