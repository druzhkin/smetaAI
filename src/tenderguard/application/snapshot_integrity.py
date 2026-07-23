from __future__ import annotations

import hashlib
import json
from io import BytesIO
from pathlib import PurePosixPath
from typing import Any

from tenderguard.domain.common import content_hash
from tenderguard.infrastructure.object_store import ObjectStore, copy_limited
from tenderguard.infrastructure.orm import CalculationSnapshotRow


def read_verified_snapshot(
    *,
    object_store: ObjectStore,
    snapshot: CalculationSnapshotRow,
    max_bytes: int = 100 * 1024 * 1024,
) -> dict[str, Any]:
    object_hash = PurePosixPath(snapshot.object_key).name
    if len(object_hash) != 64 or any(
        character not in "0123456789abcdef" for character in object_hash
    ):
        raise RuntimeError("Snapshot object key is not content-addressed")

    buffer = BytesIO()
    with object_store.open(object_hash) as stream:
        copy_limited(stream, buffer, max_bytes)
    snapshot_bytes = buffer.getvalue()
    if hashlib.sha256(snapshot_bytes).hexdigest() != object_hash:
        raise RuntimeError("Immutable snapshot object hash does not match its content")
    payload = json.loads(snapshot_bytes)
    if not isinstance(payload, dict):
        raise RuntimeError("Immutable snapshot payload is not an object")
    _verify_snapshot_payload(snapshot, payload)
    return payload


def _verify_snapshot_payload(
    snapshot: CalculationSnapshotRow,
    payload: dict[str, Any],
) -> None:
    stored_snapshot = payload.get("snapshot")
    inputs = payload.get("inputs")
    policy = payload.get("policy")
    controlled_versions = payload.get("controlled_versions")
    primary = payload.get("primary")
    independent = payload.get("independent")
    if (
        not isinstance(stored_snapshot, dict)
        or not isinstance(inputs, list)
        or not isinstance(policy, dict)
        or not isinstance(controlled_versions, list)
        or not isinstance(primary, dict)
        or not isinstance(independent, dict)
    ):
        raise RuntimeError("Immutable snapshot payload has an invalid structure")

    computed_input_hash = content_hash(
        {
            "atomic_inputs": inputs,
            "calculation_policy": policy,
            "controlled_versions": controlled_versions,
        }
    )
    computed_output_hash = content_hash(
        {
            "primary": primary,
            "independent": independent,
        }
    )
    computed_snapshot_hash = content_hash(
        {
            "project_id": stored_snapshot.get("project_id"),
            "document_set_revision_id": stored_snapshot.get("document_set_revision_id"),
            "input_hash": computed_input_hash,
            "output_hash": computed_output_hash,
            "created_by": stored_snapshot.get("created_by"),
            "created_at": stored_snapshot.get("created_at"),
        }
    )
    if (
        stored_snapshot.get("snapshot_id") != snapshot.id
        or stored_snapshot.get("project_id") != snapshot.project_id
        or stored_snapshot.get("document_set_revision_id") != snapshot.document_set_revision_id
        or stored_snapshot.get("input_hash") != snapshot.input_hash
        or stored_snapshot.get("output_hash") != snapshot.output_hash
        or stored_snapshot.get("snapshot_hash") != snapshot.snapshot_hash
        or computed_input_hash != snapshot.input_hash
        or computed_output_hash != snapshot.output_hash
        or computed_snapshot_hash != snapshot.snapshot_hash
    ):
        raise RuntimeError("Immutable snapshot payload fails integrity verification")
