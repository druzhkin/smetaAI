import base64

import pytest

from tenderguard.domain.exports import (
    EXPORT_FORMAT,
    EXPORT_SCHEMA_VERSION,
    REQUIRED_CONTENT_NAMES,
    ExportManifest,
    SignedExportPackage,
    build_content_entries,
    build_signed_export_package,
    load_signing_material,
    verify_signed_export_package,
)

PRIVATE_KEY_B64 = base64.b64encode(bytes(range(32))).decode("ascii")


def _contents() -> dict[str, object]:
    return {name: {"name": name, "value": "fixed"} for name in REQUIRED_CONTENT_NAMES}


def _package() -> SignedExportPackage:
    contents = _contents()
    manifest = ExportManifest(
        schema_version=EXPORT_SCHEMA_VERSION,
        format=EXPORT_FORMAT,
        project_id="project-1",
        organization_id="organization-1",
        project_code="TG-001",
        snapshot_id="snapshot-1",
        snapshot_hash="a" * 64,
        document_set_revision_id="document-set-1",
        release_decision_id="release-1",
        release_state="APPROVED_FOR_INTERNAL_USE",
        template_version_id="export-template-1",
        audit_cutoff_event_hash="b" * 64,
        content_entries=build_content_entries(contents),
    )
    return build_signed_export_package(
        manifest=manifest,
        contents=contents,
        signing_material=load_signing_material(
            key_id="export-key-2026-01",
            private_key_b64=PRIVATE_KEY_B64,
        ),
    )


def test_signed_export_round_trip_and_trusted_key_verification() -> None:
    package = _package()

    verify_signed_export_package(
        package,
        trusted_public_key_b64=package.signature.public_key_b64,
        trusted_key_id="export-key-2026-01",
    )


def test_signed_export_rejects_content_tampering_and_untrusted_key() -> None:
    package = _package()
    tampered_contents = {**package.contents, "snapshot.json": {"tampered": True}}
    tampered = package.model_copy(update={"contents": tampered_contents})
    tampered_signature = package.model_copy(
        update={
            "signature": package.signature.model_copy(
                update={"value_b64": base64.b64encode(b"x" * 64).decode("ascii")}
            )
        }
    )

    with pytest.raises(ValueError, match="content hashes"):
        verify_signed_export_package(tampered)
    with pytest.raises(ValueError, match="signature verification"):
        verify_signed_export_package(tampered_signature)
    with pytest.raises(ValueError, match="not trusted"):
        verify_signed_export_package(package, trusted_key_id="different-key")


def test_signed_export_requires_complete_content_set_and_valid_private_key() -> None:
    contents = _contents()
    contents.pop("audit_chain.json")
    manifest = _package().manifest.model_copy(
        update={"content_entries": build_content_entries(contents)}
    )

    with pytest.raises(ValueError, match="content set"):
        build_signed_export_package(
            manifest=manifest,
            contents=contents,
            signing_material=load_signing_material(
                key_id="export-key-2026-01",
                private_key_b64=PRIVATE_KEY_B64,
            ),
        )
    with pytest.raises(ValueError, match="32 bytes"):
        load_signing_material(
            key_id="export-key-2026-01",
            private_key_b64=base64.b64encode(b"too-short").decode("ascii"),
        )
