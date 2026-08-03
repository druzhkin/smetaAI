from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from tenderguard.config import Settings
from tenderguard.domain.common import ensure_utc, utc_now
from tenderguard.domain.jobs import OutboxClaim, OutboxSettlement
from tenderguard.infrastructure.orm import OutboxEventRow

_ERROR_CODE = re.compile(r"^[A-Z0-9_.:-]{1,200}$")


class OutboxLeaseLostError(RuntimeError):
    pass


@dataclass(frozen=True)
class OutboxDeliveryPolicy:
    lease_seconds: int
    max_attempts: int
    retry_base_seconds: int
    retry_max_seconds: int

    def __post_init__(self) -> None:
        if self.lease_seconds <= 0:
            raise ValueError("Outbox lease must be positive")
        if self.max_attempts <= 0:
            raise ValueError("Outbox maximum attempts must be positive")
        if self.retry_base_seconds <= 0:
            raise ValueError("Outbox retry base must be positive")
        if self.retry_max_seconds < self.retry_base_seconds:
            raise ValueError("Outbox retry maximum must be at least the base delay")

    @classmethod
    def document(cls, settings: Settings) -> OutboxDeliveryPolicy:
        return cls(
            lease_seconds=settings.document_job_lease_seconds,
            max_attempts=settings.document_job_max_attempts,
            retry_base_seconds=settings.document_job_retry_base_seconds,
            retry_max_seconds=settings.document_job_retry_max_seconds,
        )

    @classmethod
    def integration(cls, settings: Settings) -> OutboxDeliveryPolicy:
        return cls(
            lease_seconds=settings.integration_job_lease_seconds,
            max_attempts=settings.integration_job_max_attempts,
            retry_base_seconds=settings.integration_job_retry_base_seconds,
            retry_max_seconds=settings.integration_job_retry_max_seconds,
        )

    @classmethod
    def automation(cls, settings: Settings) -> OutboxDeliveryPolicy:
        return cls(
            lease_seconds=settings.automation_job_lease_seconds,
            max_attempts=settings.automation_job_max_attempts,
            retry_base_seconds=settings.automation_job_retry_base_seconds,
            retry_max_seconds=settings.automation_job_retry_max_seconds,
        )


class OutboxDeliveryService:
    """Short-transaction claim and settlement operations for durable outbox delivery."""

    def __init__(
        self,
        *,
        session: Session,
        settings: Settings,
        policy: OutboxDeliveryPolicy | None = None,
    ) -> None:
        self.session = session
        self.policy = policy or OutboxDeliveryPolicy.document(settings)

    def claim_next(
        self,
        *,
        topics: Collection[str],
        worker_id: str,
        aggregate_id: str | None = None,
    ) -> OutboxClaim | None:
        normalized_topics = tuple(sorted({self._topic(topic) for topic in topics}))
        if not normalized_topics:
            raise ValueError("At least one outbox topic is required")
        worker_id = self._required(worker_id, "worker_id", 128)
        now = utc_now()
        query = (
            select(OutboxEventRow)
            .where(
                OutboxEventRow.topic.in_(normalized_topics),
                OutboxEventRow.published_at.is_(None),
                OutboxEventRow.dead_lettered_at.is_(None),
                OutboxEventRow.available_at <= now,
                or_(
                    OutboxEventRow.lease_expires_at.is_(None),
                    OutboxEventRow.lease_expires_at <= now,
                ),
            )
            .order_by(
                OutboxEventRow.available_at,
                OutboxEventRow.created_at,
                OutboxEventRow.id,
            )
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if aggregate_id is not None:
            query = query.where(
                OutboxEventRow.aggregate_id == self._required(aggregate_id, "aggregate_id", 128)
            )
        row = self.session.scalar(query)
        if row is None:
            return None
        lease_token = f"outbox-lease-{uuid4()}"
        lease_expires_at = now + timedelta(seconds=self.policy.lease_seconds)
        row.attempts += 1
        row.locked_by = worker_id
        row.lease_token = lease_token
        row.lease_expires_at = lease_expires_at
        row.last_attempt_at = now
        self.session.flush()
        occurred_at = ensure_utc(row.created_at)
        assert occurred_at is not None
        return OutboxClaim(
            event_id=row.id,
            deduplication_key=row.deduplication_key,
            delivery_deduplication_key=row.delivery_deduplication_key,
            topic=row.topic,
            aggregate_id=row.aggregate_id,
            payload=row.payload,
            occurred_at=occurred_at,
            delivery_attempt=row.attempts,
            worker_id=worker_id,
            lease_token=lease_token,
            lease_expires_at=lease_expires_at,
        )

    def acknowledge(self, claim: OutboxClaim) -> OutboxSettlement:
        row = self._locked_event(claim.event_id)
        if row.published_at is not None:
            return OutboxSettlement(event_id=row.id, dead_lettered=False)
        if row.dead_lettered_at is not None:
            raise OutboxLeaseLostError("Cannot acknowledge a dead-lettered outbox event")
        self._require_owner(row, claim)
        row.published_at = utc_now()
        self._clear_lease(row)
        self.session.flush()
        return OutboxSettlement(event_id=row.id, dead_lettered=False)

    def reject(
        self,
        claim: OutboxClaim,
        *,
        error_code: str,
        force_dead_letter: bool = False,
        allow_dead_letter: bool = True,
    ) -> OutboxSettlement:
        error_code = self._error_code(error_code)
        row = self._locked_event(claim.event_id)
        if row.published_at is not None:
            raise OutboxLeaseLostError("Cannot reject an acknowledged outbox event")
        if row.dead_lettered_at is not None:
            return OutboxSettlement(event_id=row.id, dead_lettered=True)
        self._require_owner(row, claim)
        now = utc_now()
        row.last_error = error_code
        dead_lettered = force_dead_letter or (
            allow_dead_letter and row.attempts >= self.policy.max_attempts
        )
        next_available_at = None
        if dead_lettered:
            row.dead_lettered_at = now
        else:
            exponent = min(max(row.attempts - 1, 0), 30)
            delay = min(
                self.policy.retry_base_seconds * (2**exponent),
                self.policy.retry_max_seconds,
            )
            next_available_at = now + timedelta(seconds=delay)
            row.available_at = next_available_at
        self._clear_lease(row)
        self.session.flush()
        return OutboxSettlement(
            event_id=row.id,
            dead_lettered=dead_lettered,
            next_available_at=next_available_at,
        )

    def _locked_event(self, event_id: str) -> OutboxEventRow:
        row = self.session.scalar(
            select(OutboxEventRow).where(OutboxEventRow.id == event_id).with_for_update()
        )
        if row is None:
            raise LookupError(event_id)
        return row

    @staticmethod
    def _require_owner(row: OutboxEventRow, claim: OutboxClaim) -> None:
        lease_expires_at = ensure_utc(row.lease_expires_at)
        claim_expires_at = ensure_utc(claim.lease_expires_at)
        if (
            row.locked_by != claim.worker_id
            or row.lease_token != claim.lease_token
            or lease_expires_at is None
            or claim_expires_at is None
            or lease_expires_at != claim_expires_at
            or lease_expires_at <= utc_now()
        ):
            raise OutboxLeaseLostError("Outbox event lease is owned by another worker")

    @staticmethod
    def _clear_lease(row: OutboxEventRow) -> None:
        row.locked_by = None
        row.lease_token = None
        row.lease_expires_at = None

    @staticmethod
    def _required(value: str, field: str, max_length: int) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{field} is required")
        if len(normalized) > max_length:
            raise ValueError(f"{field} exceeds {max_length} characters")
        return normalized

    @classmethod
    def _topic(cls, topic: str) -> str:
        return cls._required(topic, "topic", 200)

    @staticmethod
    def _error_code(error_code: str) -> str:
        normalized = error_code.strip().upper()
        if not _ERROR_CODE.fullmatch(normalized):
            raise ValueError("error_code must be an uppercase machine code")
        return normalized
