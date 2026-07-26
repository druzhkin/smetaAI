from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from tenderguard.application.rate_limits import DistributedRateLimiter
from tenderguard.config import Settings, get_settings
from tenderguard.domain.enums import ActorRole
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.orm import RateLimitBucketRow


def _require_postgresql() -> str:
    database_url = get_settings().database_url
    if not database_url.startswith("postgresql"):
        pytest.skip("Distributed quota concurrency requires PostgreSQL")
    return database_url


def test_postgresql_rate_limit_upsert_serializes_concurrent_consumption() -> None:
    database_url = _require_postgresql()
    suffix = uuid4().hex
    settings = Settings(
        app_env="test",
        database_url=database_url,
        rate_limit_enabled=True,
        rate_limit_identity_key_id="postgresql-rate-limit-key-1",
        rate_limit_identity_key="postgresql-rate-limit-key-at-least-32-bytes",
        rate_limit_window_seconds=60,
        rate_limit_actor_read_requests=10,
        rate_limit_organization_read_requests=10,
        rate_limit_actor_mutation_requests=10,
        rate_limit_organization_mutation_requests=10,
        rate_limit_actor_upload_requests=10,
        rate_limit_organization_upload_requests=10,
    )
    actor = Actor(
        f"rate-limit-actor-{suffix}",
        f"rate-limit-org-{suffix}",
        frozenset({ActorRole.ESTIMATOR}),
    )
    now = datetime(2026, 7, 24, 12, 0, 30, tzinfo=UTC)

    def consume(_: int) -> bool:
        engine = create_engine(database_url)
        try:
            with Session(engine) as session, session.begin():
                return (
                    DistributedRateLimiter(
                        session=session,
                        settings=settings,
                    )
                    .consume(actor=actor, category="READ", now=now)
                    .allowed
                )
        finally:
            engine.dispose()

    with ThreadPoolExecutor(max_workers=10) as executor:
        allowed = tuple(executor.map(consume, range(20)))
    assert sum(allowed) == 10

    engine = create_engine(database_url)
    with Session(engine) as session:
        rows = tuple(
            session.scalars(
                select(RateLimitBucketRow).where(
                    RateLimitBucketRow.scope.in_(("ACTOR_READ", "ORGANIZATION_READ")),
                    RateLimitBucketRow.request_count == 20,
                    RateLimitBucketRow.updated_at == now,
                )
            )
        )
    engine.dispose()
    assert tuple(row.request_count for row in rows) == (20, 20)

    for row in rows:
        engine = create_engine(database_url)
        with (
            pytest.raises(DBAPIError, match="invalid rate-limit bucket transition"),
            engine.begin() as connection,
        ):
            connection.execute(
                text(
                    """
                    UPDATE rate_limit_buckets
                    SET request_count = 1
                    WHERE scope = :scope AND identity_hash = :identity_hash
                    """
                ),
                {
                    "scope": row.scope,
                    "identity_hash": row.identity_hash,
                },
            )
        engine.dispose()
        engine = create_engine(database_url)
        with (
            pytest.raises(DBAPIError, match="rate-limit bucket cannot be deleted"),
            engine.begin() as connection,
        ):
            connection.execute(
                text(
                    """
                    DELETE FROM rate_limit_buckets
                    WHERE scope = :scope AND identity_hash = :identity_hash
                    """
                ),
                {
                    "scope": row.scope,
                    "identity_hash": row.identity_hash,
                },
            )
        engine.dispose()
