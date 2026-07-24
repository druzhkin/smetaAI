from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from io import BytesIO
from typing import TYPE_CHECKING
from uuid import uuid4

from fastapi import HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from tenderguard.config import Settings
from tenderguard.domain.audit import AuditEvent, verify_chain
from tenderguard.domain.audit_anchor import (
    AUDIT_ANCHOR_RECEIPT_SCHEMA_VERSION,
    AUDIT_CHECKPOINT_SCHEMA_VERSION,
    AuditAnchorStatement,
    AuditChainTerminal,
    AuditCheckpointManifest,
    verify_anchor_signature,
)
from tenderguard.domain.common import canonical_json, content_hash, ensure_utc, utc_now
from tenderguard.domain.enums import ActorRole
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.object_store import ObjectStore
from tenderguard.infrastructure.orm import (
    AuditAnchorReceiptRow,
    AuditCheckpointRow,
    AuditEventRow,
)

if TYPE_CHECKING:
    from tenderguard.application.projects import ProjectService


class ApplicationModel(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True, extra="forbid")


class AuditCheckpointView(ApplicationModel):
    checkpoint_id: str
    schema_version: str
    event_count: int
    terminal_count: int
    checkpoint_hash: str
    object_hash: str
    created_by: str
    created_at: datetime


class AuditAnchorReceiptView(ApplicationModel):
    receipt_id: str
    checkpoint_id: str
    provider_id: str
    provider_key_id: str
    external_reference: str
    anchored_at: datetime
    receipt_hash: str
    registered_by: str
    created_at: datetime


class AuditAnchorStatus(ApplicationModel):
    valid: bool
    checkpoint_id: str | None = None
    checkpoint_hash: str | None = None
    receipt_id: str | None = None
    anchored_at: datetime | None = None
    age_seconds: int | None = None
    reasons: tuple[str, ...] = ()


class AuditIntegrityService:
    def __init__(
        self,
        *,
        session: Session,
        settings: Settings,
        object_store: ObjectStore,
    ) -> None:
        self.session = session
        self.settings = settings
        self.object_store = object_store

    def create_checkpoint(
        self,
        *,
        actor: Actor,
        request_id: str,
        reason: str,
    ) -> AuditCheckpointView:
        self.require_operator(actor)
        reason = self._required_text(reason, "reason", 2000)
        rows = list(
            self.session.scalars(
                select(AuditEventRow).order_by(
                    AuditEventRow.aggregate_type,
                    AuditEventRow.aggregate_id,
                    AuditEventRow.sequence,
                )
            )
        )
        if not rows:
            raise ValueError("An audit checkpoint requires at least one audit event")
        chains: dict[tuple[str, str], list[AuditEvent]] = defaultdict(list)
        for row in rows:
            event = self._event(row)
            chains[(event.aggregate_type, event.aggregate_id)].append(event)
        terminals: list[AuditChainTerminal] = []
        for key in sorted(chains):
            chain = chains[key]
            if not verify_chain(chain, self.settings.audit_verification_keyring):
                raise ValueError(
                    "Audit checkpoint creation blocked: an audit chain does not verify"
                )
            terminal = chain[-1]
            terminals.append(
                AuditChainTerminal(
                    aggregate_type=terminal.aggregate_type,
                    aggregate_id=terminal.aggregate_id,
                    sequence=terminal.sequence,
                    event_hash=terminal.event_hash,
                )
            )
        now = utc_now()
        checkpoint_id = f"audit-checkpoint-{uuid4()}"
        manifest = AuditCheckpointManifest(
            schema_version=AUDIT_CHECKPOINT_SCHEMA_VERSION,
            checkpoint_id=checkpoint_id,
            created_at=now,
            event_count=len(rows),
            terminals=tuple(terminals),
        )
        checkpoint_hash = content_hash(manifest)
        stored = self.object_store.put(BytesIO(canonical_json(manifest)))
        if stored.object_hash != checkpoint_hash:
            raise RuntimeError("Stored audit checkpoint differs from its canonical manifest")
        checkpoint_row = AuditCheckpointRow(
            id=checkpoint_id,
            schema_version=manifest.schema_version,
            event_count=manifest.event_count,
            terminal_count=len(manifest.terminals),
            checkpoint_hash=checkpoint_hash,
            object_hash=stored.object_hash,
            object_key=stored.object_key,
            size_bytes=stored.size_bytes,
            created_by=actor.actor_id,
            created_at=now,
        )
        self.session.add(checkpoint_row)
        self.session.flush()
        self._project_service().record_event(
            aggregate_type="audit_checkpoint",
            aggregate_id=checkpoint_row.id,
            event_type="audit_checkpoint_created",
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload={
                "checkpoint_hash": checkpoint_row.checkpoint_hash,
                "event_count": checkpoint_row.event_count,
                "terminal_count": checkpoint_row.terminal_count,
                "object_hash": checkpoint_row.object_hash,
            },
        )
        return self._checkpoint_view(checkpoint_row)

    def register_receipt(
        self,
        *,
        actor: Actor,
        checkpoint_id: str,
        anchored_at: datetime,
        external_reference: str,
        signature_b64: str,
        request_id: str,
        reason: str,
    ) -> AuditAnchorReceiptView:
        self.require_operator(actor)
        if not self.settings.audit_anchor_configured:
            raise ValueError("External audit anchor is not configured")
        reason = self._required_text(reason, "reason", 2000)
        external_reference = self._required_text(
            external_reference,
            "external_reference",
            500,
        )
        checkpoint = self.session.scalar(
            select(AuditCheckpointRow)
            .where(AuditCheckpointRow.id == checkpoint_id)
            .with_for_update()
        )
        if checkpoint is None:
            raise LookupError(checkpoint_id)
        if checkpoint.created_by == actor.actor_id:
            raise ValueError("Audit anchor receipt must be registered by a different administrator")
        anchored_at = self._required_utc(anchored_at, "anchored_at")
        checkpoint_created_at = self._required_utc(checkpoint.created_at, "checkpoint created_at")
        now = utc_now()
        if anchored_at < checkpoint_created_at or anchored_at > now:
            raise ValueError("Audit anchor timestamp is outside the valid checkpoint interval")
        existing = self.session.scalar(
            select(AuditAnchorReceiptRow).where(
                AuditAnchorReceiptRow.checkpoint_id == checkpoint.id
            )
        )
        if existing is not None:
            if (
                self._required_utc(existing.anchored_at, "anchored_at") != anchored_at
                or existing.external_reference != external_reference
                or existing.signature_b64 != signature_b64
            ):
                raise ValueError("Audit checkpoint already has a different external anchor receipt")
            return self._receipt_view(existing)
        assert self.settings.audit_anchor_provider_id is not None
        assert self.settings.audit_anchor_provider_key_id is not None
        assert self.settings.audit_anchor_public_key_b64 is not None
        statement = AuditAnchorStatement(
            schema_version=AUDIT_ANCHOR_RECEIPT_SCHEMA_VERSION,
            provider_id=self.settings.audit_anchor_provider_id,
            provider_key_id=self.settings.audit_anchor_provider_key_id,
            checkpoint_hash=checkpoint.checkpoint_hash,
            anchored_at=anchored_at,
            external_reference=external_reference,
        )
        verify_anchor_signature(
            statement=statement,
            signature_b64=signature_b64,
            trusted_public_key_b64=self.settings.audit_anchor_public_key_b64,
        )
        receipt_hash = content_hash(
            {
                "statement": statement,
                "signature_b64": signature_b64,
            }
        )
        row = AuditAnchorReceiptRow(
            id=f"audit-anchor-receipt-{uuid4()}",
            checkpoint_id=checkpoint.id,
            provider_id=statement.provider_id,
            provider_key_id=statement.provider_key_id,
            external_reference=statement.external_reference,
            anchored_at=statement.anchored_at,
            signature_b64=signature_b64,
            receipt_hash=receipt_hash,
            registered_by=actor.actor_id,
            created_at=now,
        )
        self.session.add(row)
        self.session.flush()
        self._project_service().record_event(
            aggregate_type="audit_checkpoint",
            aggregate_id=checkpoint.id,
            event_type="audit_anchor_receipt_registered",
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload={
                "receipt_id": row.id,
                "provider_id": row.provider_id,
                "provider_key_id": row.provider_key_id,
                "external_reference": row.external_reference,
                "anchored_at": row.anchored_at,
                "receipt_hash": row.receipt_hash,
            },
        )
        return self._receipt_view(row)

    def anchor_status(self) -> AuditAnchorStatus:
        if not self.settings.audit_anchor_configured:
            return AuditAnchorStatus(
                valid=False,
                reasons=("External audit anchor is not configured",),
            )
        receipt = self.session.scalar(
            select(AuditAnchorReceiptRow)
            .order_by(AuditAnchorReceiptRow.anchored_at.desc())
            .limit(1)
        )
        if receipt is None:
            return AuditAnchorStatus(
                valid=False,
                reasons=("No external audit anchor receipt is registered",),
            )
        checkpoint = self.session.get(AuditCheckpointRow, receipt.checkpoint_id)
        if checkpoint is None:
            return AuditAnchorStatus(
                valid=False,
                receipt_id=receipt.id,
                reasons=("Audit anchor checkpoint is missing",),
            )
        reasons: list[str] = []
        try:
            manifest = self._read_checkpoint_manifest(checkpoint)
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
            manifest = None
            reasons.append("Audit checkpoint object integrity verification failed")
        anchored_at = self._required_utc(receipt.anchored_at, "anchored_at")
        now = utc_now()
        age_seconds = int((now - anchored_at).total_seconds())
        if age_seconds < 0:
            reasons.append("Audit anchor timestamp is in the future")
        assert self.settings.audit_anchor_max_age_seconds is not None
        if age_seconds > self.settings.audit_anchor_max_age_seconds:
            reasons.append("External audit anchor receipt is stale")
        if (
            receipt.provider_id != self.settings.audit_anchor_provider_id
            or receipt.provider_key_id != self.settings.audit_anchor_provider_key_id
        ):
            reasons.append("Audit anchor provider binding differs from configuration")
        else:
            assert self.settings.audit_anchor_public_key_b64 is not None
            statement = AuditAnchorStatement(
                schema_version=AUDIT_ANCHOR_RECEIPT_SCHEMA_VERSION,
                provider_id=receipt.provider_id,
                provider_key_id=receipt.provider_key_id,
                checkpoint_hash=checkpoint.checkpoint_hash,
                anchored_at=anchored_at,
                external_reference=receipt.external_reference,
            )
            expected_receipt_hash = content_hash(
                {
                    "statement": statement,
                    "signature_b64": receipt.signature_b64,
                }
            )
            if expected_receipt_hash != receipt.receipt_hash:
                reasons.append("Audit anchor receipt hash verification failed")
            try:
                verify_anchor_signature(
                    statement=statement,
                    signature_b64=receipt.signature_b64,
                    trusted_public_key_b64=self.settings.audit_anchor_public_key_b64,
                )
            except ValueError:
                reasons.append("External audit anchor signature verification failed")
        if manifest is not None:
            self._verify_current_audit_against_manifest(
                manifest=manifest,
                reasons=reasons,
            )
        return AuditAnchorStatus(
            valid=not reasons,
            checkpoint_id=checkpoint.id,
            checkpoint_hash=checkpoint.checkpoint_hash,
            receipt_id=receipt.id,
            anchored_at=anchored_at,
            age_seconds=age_seconds,
            reasons=tuple(reasons),
        )

    def require_operator(self, actor: Actor) -> None:
        actor.require_any(ActorRole.ADMIN)
        operator_organization = self.settings.audit_operator_organization_id
        if operator_organization is not None and actor.organization_id != operator_organization:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Audit operator organization is required",
            )

    def _read_checkpoint_manifest(
        self,
        checkpoint: AuditCheckpointRow,
    ) -> AuditCheckpointManifest:
        with self.object_store.open(checkpoint.object_hash) as stream:
            raw = stream.read()
        manifest = AuditCheckpointManifest.model_validate(json.loads(raw))
        if (
            manifest.schema_version != AUDIT_CHECKPOINT_SCHEMA_VERSION
            or manifest.checkpoint_id != checkpoint.id
            or manifest.event_count != checkpoint.event_count
            or len(manifest.terminals) != checkpoint.terminal_count
            or content_hash(manifest) != checkpoint.checkpoint_hash
            or checkpoint.object_hash != checkpoint.checkpoint_hash
            or len(raw) != checkpoint.size_bytes
        ):
            raise ValueError("Audit checkpoint manifest differs from its immutable row")
        return manifest

    def _verify_current_audit_against_manifest(
        self,
        *,
        manifest: AuditCheckpointManifest,
        reasons: list[str],
    ) -> None:
        rows = list(
            self.session.scalars(
                select(AuditEventRow).order_by(
                    AuditEventRow.aggregate_type,
                    AuditEventRow.aggregate_id,
                    AuditEventRow.sequence,
                )
            )
        )
        if len(rows) < manifest.event_count:
            reasons.append("Current audit event set predates the anchored checkpoint")
            return
        chains: dict[tuple[str, str], list[AuditEvent]] = defaultdict(list)
        try:
            for row in rows:
                event = self._event(row)
                chains[(event.aggregate_type, event.aggregate_id)].append(event)
        except ValueError:
            reasons.append("Current audit event data is invalid")
            return
        if any(
            not verify_chain(chain, self.settings.audit_verification_keyring)
            for chain in chains.values()
        ):
            reasons.append("Current audit chain verification failed")
            return
        for terminal in manifest.terminals:
            chain = chains.get((terminal.aggregate_type, terminal.aggregate_id))
            if (
                chain is None
                or len(chain) < terminal.sequence
                or chain[terminal.sequence - 1].event_hash != terminal.event_hash
            ):
                reasons.append("Anchored audit terminal is absent from current history")
                return

    def _project_service(self) -> ProjectService:
        from tenderguard.application.projects import ProjectService

        return ProjectService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        )

    @staticmethod
    def _event(row: AuditEventRow) -> AuditEvent:
        occurred_at = ensure_utc(row.occurred_at)
        if occurred_at is None:
            raise ValueError("Audit event timestamp is missing")
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

    @staticmethod
    def _checkpoint_view(row: AuditCheckpointRow) -> AuditCheckpointView:
        created_at = AuditIntegrityService._required_utc(row.created_at, "created_at")
        return AuditCheckpointView(
            checkpoint_id=row.id,
            schema_version=row.schema_version,
            event_count=row.event_count,
            terminal_count=row.terminal_count,
            checkpoint_hash=row.checkpoint_hash,
            object_hash=row.object_hash,
            created_by=row.created_by,
            created_at=created_at,
        )

    @staticmethod
    def _receipt_view(row: AuditAnchorReceiptRow) -> AuditAnchorReceiptView:
        return AuditAnchorReceiptView(
            receipt_id=row.id,
            checkpoint_id=row.checkpoint_id,
            provider_id=row.provider_id,
            provider_key_id=row.provider_key_id,
            external_reference=row.external_reference,
            anchored_at=AuditIntegrityService._required_utc(
                row.anchored_at,
                "anchored_at",
            ),
            receipt_hash=row.receipt_hash,
            registered_by=row.registered_by,
            created_at=AuditIntegrityService._required_utc(
                row.created_at,
                "created_at",
            ),
        )

    @staticmethod
    def _required_utc(value: datetime | None, field: str) -> datetime:
        normalized = ensure_utc(value)
        if normalized is None:
            raise ValueError(f"{field} is required")
        return normalized

    @staticmethod
    def _required_text(value: str, field: str, max_length: int) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{field} is required")
        if normalized != value:
            raise ValueError(f"{field} must not contain surrounding whitespace")
        if len(normalized) > max_length:
            raise ValueError(f"{field} exceeds {max_length} characters")
        return normalized
