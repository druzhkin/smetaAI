from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from tenderguard.application.audit_integrity import AuditIntegrityService
from tenderguard.config import Settings
from tenderguard.domain.audit import verify_chain
from tenderguard.domain.common import canonical_json, content_hash
from tenderguard.domain.enums import VersionStatus
from tenderguard.domain.operational_qualification import QualificationResultEnvelope
from tenderguard.infrastructure.orm import AuditEventRow, ControlledVersionRow

ProfileT = TypeVar("ProfileT", bound=BaseModel)


def load_approved_profile(
    *,
    session: Session,
    settings: Settings,
    version_id: str,
    expected_content_hash: str,
    expected_kind: str,
    profile_type: type[ProfileT],
) -> tuple[ProfileT, ControlledVersionRow]:
    if len(expected_content_hash) != 64 or any(
        character not in "0123456789abcdef" for character in expected_content_hash
    ):
        raise ValueError("Expected controlled profile hash is not a SHA-256 digest")
    row = session.scalar(select(ControlledVersionRow).where(ControlledVersionRow.id == version_id))
    if row is None:
        raise LookupError(version_id)
    expected_row_hash = content_hash(
        {
            "kind": row.kind,
            "version_label": row.version_label,
            "payload": row.payload,
        }
    )
    governance = row.payload.get("_governance")
    if (
        row.kind != expected_kind
        or row.status != VersionStatus.APPROVED.value
        or row.content_hash != expected_content_hash
        or row.content_hash != expected_row_hash
        or not row.approved_by
        or row.approved_at is None
        or not isinstance(governance, dict)
        or not isinstance(governance.get("created_by"), str)
        or not governance["created_by"]
        or row.approved_by == governance["created_by"]
    ):
        raise ValueError("Controlled qualification profile is not validly approved and bound")
    events = [
        AuditIntegrityService._event(event)
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
    if (
        not events
        or not verify_chain(events, settings.audit_verification_keyring)
        or len(created) != 1
        or len(approved) != 1
        or created[0].payload.get("content_hash") != row.content_hash
        or approved[0].payload.get("content_hash") != row.content_hash
        or created[0].actor_id != governance["created_by"]
        or approved[0].actor_id != row.approved_by
    ):
        raise ValueError("Controlled qualification profile audit approval does not verify")
    raw_profile = {key: value for key, value in row.payload.items() if key != "_governance"}
    return profile_type.model_validate(raw_profile), row


def build_result_envelope(
    *,
    qualification_type: str,
    status: str,
    profile_version_id: str,
    profile_content_hash: str,
    started_at: Any,
    completed_at: Any,
    findings: tuple[Any, ...],
    evidence: Mapping[str, object],
) -> QualificationResultEnvelope:
    body = {
        "schema_version": "tenderguard.qualification-result/v1",
        "qualification_type": qualification_type,
        "status": status,
        "profile_version_id": profile_version_id,
        "profile_content_hash": profile_content_hash,
        "started_at": started_at,
        "completed_at": completed_at,
        "findings": findings,
        "evidence": dict(evidence),
    }
    return QualificationResultEnvelope.model_validate(
        {
            **body,
            "result_hash": content_hash(body),
        }
    )


def write_result_exclusive(
    result: QualificationResultEnvelope,
    destination: Path,
) -> None:
    resolved = destination.resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json(result)
    descriptor = os.open(
        resolved,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        resolved.unlink(missing_ok=True)
        raise


def read_json_object(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Qualification input is not readable UTF-8 JSON") from error
    if not isinstance(raw, dict):
        raise ValueError("Qualification input JSON must be an object")
    return raw
