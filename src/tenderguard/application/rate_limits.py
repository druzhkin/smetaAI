from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import and_, case, or_
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from tenderguard.config import Settings
from tenderguard.domain.common import content_hash, ensure_utc, utc_now
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.orm import RateLimitBucketRow

RateLimitCategory = Literal["READ", "MUTATION", "UPLOAD"]


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    category: RateLimitCategory
    actor_limit: int
    organization_limit: int
    remaining: int
    reset_at: datetime
    reset_epoch_seconds: int
    retry_after_seconds: int


class DistributedRateLimiter:
    def __init__(
        self,
        *,
        session: Session,
        settings: Settings,
    ) -> None:
        self.session = session
        self.settings = settings

    def consume(
        self,
        *,
        actor: Actor,
        category: RateLimitCategory,
        now: datetime | None = None,
    ) -> RateLimitDecision:
        if not self.settings.distributed_rate_limit_configured:
            raise RuntimeError("Distributed rate limiting is not fully configured")
        current = ensure_utc(now or utc_now())
        if current is None:
            raise ValueError("Rate-limit timestamp is missing")
        window_seconds = self.settings.rate_limit_window_seconds
        key_id = self.settings.rate_limit_identity_key_id
        secret = self.settings.rate_limit_identity_key
        assert window_seconds is not None
        assert key_id is not None
        assert secret is not None
        actor_limit, organization_limit = self._limits(category)
        window_number = self._epoch_seconds(current) // window_seconds
        policy_hash = self._policy_hash()
        counts = (
            self._increment(
                scope=f"ACTOR_{category}",
                identity_hash=self._identity_hash(
                    key=secret.get_secret_value().encode("utf-8"),
                    key_id=key_id,
                    identity=(f"organization:{actor.organization_id}\x00actor:{actor.actor_id}"),
                ),
                policy_hash=policy_hash,
                window_number=window_number,
                now=current,
            ),
            self._increment(
                scope=f"ORGANIZATION_{category}",
                identity_hash=self._identity_hash(
                    key=secret.get_secret_value().encode("utf-8"),
                    key_id=key_id,
                    identity=f"organization:{actor.organization_id}",
                ),
                policy_hash=policy_hash,
                window_number=window_number,
                now=current,
            ),
        )
        actor_count, organization_count = counts
        reset_epoch_seconds = (window_number + 1) * window_seconds
        reset_at = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(seconds=reset_epoch_seconds)
        remaining = min(
            max(actor_limit - actor_count, 0),
            max(organization_limit - organization_count, 0),
        )
        retry_delta = reset_at - current
        retry_after_seconds = max(
            1,
            retry_delta.days * 86_400
            + retry_delta.seconds
            + (1 if retry_delta.microseconds else 0),
        )
        return RateLimitDecision(
            allowed=actor_count <= actor_limit and organization_count <= organization_limit,
            category=category,
            actor_limit=actor_limit,
            organization_limit=organization_limit,
            remaining=remaining,
            reset_at=reset_at,
            reset_epoch_seconds=reset_epoch_seconds,
            retry_after_seconds=retry_after_seconds,
        )

    def _increment(
        self,
        *,
        scope: str,
        identity_hash: str,
        policy_hash: str,
        window_number: int,
        now: datetime,
    ) -> int:
        values = {
            "scope": scope,
            "identity_hash": identity_hash,
            "policy_hash": policy_hash,
            "window_number": window_number,
            "request_count": 1,
            "updated_at": now,
        }
        dialect_name = self.session.get_bind().dialect.name
        statement: Any
        if dialect_name == "postgresql":
            statement = postgresql_insert(RateLimitBucketRow).values(**values)
        elif dialect_name == "sqlite":
            statement = sqlite_insert(RateLimitBucketRow).values(**values)
        else:
            raise RuntimeError("Distributed rate limiting supports only PostgreSQL and SQLite")
        statement = statement.on_conflict_do_update(
            index_elements=[
                RateLimitBucketRow.scope,
                RateLimitBucketRow.identity_hash,
            ],
            set_={
                "policy_hash": policy_hash,
                "window_number": window_number,
                "request_count": case(
                    (
                        and_(
                            RateLimitBucketRow.window_number == window_number,
                            RateLimitBucketRow.policy_hash == policy_hash,
                        ),
                        RateLimitBucketRow.request_count + 1,
                    ),
                    else_=1,
                ),
                "updated_at": now,
            },
            where=or_(
                RateLimitBucketRow.window_number < window_number,
                and_(
                    RateLimitBucketRow.window_number == window_number,
                    RateLimitBucketRow.policy_hash == policy_hash,
                ),
            ),
        ).returning(
            RateLimitBucketRow.request_count,
            RateLimitBucketRow.policy_hash,
            RateLimitBucketRow.window_number,
        )
        result = self.session.execute(statement).one_or_none()
        if result is None:
            raise RuntimeError("Rate-limit policy or window differs across application instances")
        count, persisted_policy_hash, persisted_window = result
        if (
            persisted_policy_hash != policy_hash
            or persisted_window != window_number
            or not isinstance(count, int)
            or count < 1
        ):
            raise RuntimeError("Rate-limit bucket did not reproduce")
        return count

    def _limits(self, category: RateLimitCategory) -> tuple[int, int]:
        values = {
            "READ": (
                self.settings.rate_limit_actor_read_requests,
                self.settings.rate_limit_organization_read_requests,
            ),
            "MUTATION": (
                self.settings.rate_limit_actor_mutation_requests,
                self.settings.rate_limit_organization_mutation_requests,
            ),
            "UPLOAD": (
                self.settings.rate_limit_actor_upload_requests,
                self.settings.rate_limit_organization_upload_requests,
            ),
        }[category]
        actor_limit, organization_limit = values
        assert actor_limit is not None
        assert organization_limit is not None
        return actor_limit, organization_limit

    def _policy_hash(self) -> str:
        return content_hash(
            {
                "identity_key_id": self.settings.rate_limit_identity_key_id,
                "window_seconds": self.settings.rate_limit_window_seconds,
                "actor_read_requests": self.settings.rate_limit_actor_read_requests,
                "organization_read_requests": (self.settings.rate_limit_organization_read_requests),
                "actor_mutation_requests": (self.settings.rate_limit_actor_mutation_requests),
                "organization_mutation_requests": (
                    self.settings.rate_limit_organization_mutation_requests
                ),
                "actor_upload_requests": self.settings.rate_limit_actor_upload_requests,
                "organization_upload_requests": (
                    self.settings.rate_limit_organization_upload_requests
                ),
            }
        )

    @staticmethod
    def _identity_hash(*, key: bytes, key_id: str, identity: str) -> str:
        return hmac.new(
            key,
            f"{key_id}\x00{identity}".encode(),
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def _epoch_seconds(value: datetime) -> int:
        delta = value - datetime(1970, 1, 1, tzinfo=UTC)
        return delta.days * 86_400 + delta.seconds


def request_rate_limit_category(
    *,
    method: str,
    path: str,
    content_type: str | None,
) -> RateLimitCategory:
    normalized_method = method.upper()
    normalized_content_type = (content_type or "").lower()
    if normalized_content_type.startswith("multipart/form-data") or (
        normalized_method == "POST" and path.endswith("/documents")
    ):
        return "UPLOAD"
    if normalized_method in {"POST", "PUT", "PATCH", "DELETE"}:
        return "MUTATION"
    return "READ"
