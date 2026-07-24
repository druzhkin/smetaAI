from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field

from tenderguard.domain.models import DomainModel
from tenderguard.domain.quarantine import QuarantinedUploadView


class DispatchDisposition(StrEnum):
    PROCESSED = "PROCESSED"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    DEAD_LETTERED = "DEAD_LETTERED"
    IDLE = "IDLE"


class OutboxClaim(DomainModel):
    event_id: str
    topic: str
    aggregate_id: str
    payload: dict[str, Any]
    delivery_attempt: int = Field(ge=1)
    worker_id: str
    lease_token: str
    lease_expires_at: datetime


class OutboxSettlement(DomainModel):
    event_id: str
    dead_lettered: bool
    next_available_at: datetime | None = None


class DocumentDispatchResult(DomainModel):
    disposition: DispatchDisposition
    outbox_event_id: str | None = None
    upload: QuarantinedUploadView | None = None
    error_code: str | None = None
