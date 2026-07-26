from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from tenderguard.api.main import create_app
from tenderguard.application.rate_limits import (
    DistributedRateLimiter,
    request_rate_limit_category,
)
from tenderguard.config import Settings
from tenderguard.domain.enums import ActorRole
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.database import (
    create_database_engine,
    create_schema_for_tests,
    create_session_factory,
)
from tenderguard.infrastructure.object_store import LocalObjectStore
from tenderguard.infrastructure.orm import RateLimitBucketRow

NOW = datetime(2026, 7, 24, 12, 0, 30, tzinfo=UTC)


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "test",
        "database_url": "sqlite+pysqlite://",
        "local_object_store_path": tmp_path / "objects",
        "allow_insecure_dev_auth": True,
        "rate_limit_enabled": True,
        "rate_limit_identity_key_id": "rate-limit-key-1",
        "rate_limit_identity_key": "test-rate-limit-identity-key-at-least-32-bytes",
        "rate_limit_window_seconds": 60,
        "rate_limit_actor_read_requests": 2,
        "rate_limit_organization_read_requests": 3,
        "rate_limit_actor_mutation_requests": 1,
        "rate_limit_organization_mutation_requests": 2,
        "rate_limit_actor_upload_requests": 1,
        "rate_limit_organization_upload_requests": 2,
    }
    values.update(overrides)
    return Settings(**values)


def test_distributed_rate_limit_is_atomic_scoped_and_resets_by_window(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    sessions = create_session_factory(engine)
    actor = Actor(
        "estimator-rate-limit",
        "org-rate-limit",
        frozenset({ActorRole.ESTIMATOR}),
    )

    decisions = []
    for _ in range(3):
        with sessions.begin() as session:
            decisions.append(
                DistributedRateLimiter(
                    session=session,
                    settings=settings,
                ).consume(actor=actor, category="READ", now=NOW)
            )
    assert [decision.allowed for decision in decisions] == [True, True, False]
    assert decisions[-1].remaining == 0
    assert decisions[-1].retry_after_seconds == 30
    assert decisions[-1].reset_epoch_seconds == 1_784_894_460
    assert decisions[-1].reset_at == datetime(2026, 7, 24, 12, 1, tzinfo=UTC)

    second_actor = Actor(
        "reviewer-rate-limit",
        actor.organization_id,
        frozenset({ActorRole.REVIEWER}),
    )
    with sessions.begin() as session:
        organization_limited = DistributedRateLimiter(
            session=session,
            settings=settings,
        ).consume(actor=second_actor, category="READ", now=NOW)
    assert not organization_limited.allowed

    with sessions.begin() as session:
        reset = DistributedRateLimiter(
            session=session,
            settings=settings,
        ).consume(actor=actor, category="READ", now=NOW + timedelta(seconds=31))
        upload = DistributedRateLimiter(
            session=session,
            settings=settings,
        ).consume(actor=actor, category="UPLOAD", now=NOW)
        rows = tuple(session.scalars(select(RateLimitBucketRow)))
    assert reset.allowed
    assert reset.remaining == 1
    assert upload.allowed
    assert len(rows) == 5
    assert all(len(row.identity_hash) == 64 for row in rows)
    assert all(actor.actor_id not in row.identity_hash for row in rows)

    changed_policy = _settings(tmp_path, rate_limit_actor_read_requests=3)
    with (
        sessions.begin() as session,
        pytest.raises(RuntimeError, match="policy or window differs"),
    ):
        DistributedRateLimiter(
            session=session,
            settings=changed_policy,
        ).consume(actor=actor, category="READ", now=NOW + timedelta(seconds=31))

    with (
        sessions.begin() as session,
        pytest.raises(RuntimeError, match="policy or window differs"),
    ):
        DistributedRateLimiter(
            session=session,
            settings=settings,
        ).consume(actor=actor, category="READ", now=NOW)

    engine.dispose()


def test_api_enforces_read_and_mutation_quotas_and_fails_closed(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    app = create_app(
        settings,
        engine=engine,
        object_store=LocalObjectStore(tmp_path / "objects"),
    )
    headers = {
        "X-Dev-Actor": "rate-limited-estimator",
        "X-Dev-Organization": "org-rate-limited-api",
        "X-Dev-Roles": "ESTIMATOR",
    }
    with TestClient(app) as client:
        first = client.get("/v1/projects", headers=headers)
        second = client.get("/v1/projects", headers=headers)
        blocked = client.get("/v1/projects", headers=headers)
        assert first.status_code == second.status_code == 200
        assert first.headers["x-ratelimit-category"] == "READ"
        assert second.headers["ratelimit-remaining"] == "0"
        assert second.headers["ratelimit-reset"].isdigit()
        assert blocked.status_code == 429
        assert blocked.headers["retry-after"]
        assert blocked.json()["detail"] == (
            "Distributed actor or organization request quota exceeded"
        )

        created = client.post(
            "/v1/projects",
            headers=headers,
            json={
                "code": "RATE-1",
                "name": "Rate-limited project",
                "reason": "Create a project inside the configured request quota",
            },
        )
        mutation_blocked = client.post(
            "/v1/projects",
            headers=headers,
            json={
                "code": "RATE-2",
                "name": "Second project",
                "reason": "This request must be rejected by the quota",
            },
        )
        assert created.status_code == 201
        assert created.headers["x-ratelimit-category"] == "MUTATION"
        assert mutation_blocked.status_code == 429

        with engine.begin() as connection:
            connection.execute(text("DROP TABLE rate_limit_buckets"))
        unavailable = client.get(
            "/v1/projects",
            headers={
                **headers,
                "X-Dev-Actor": "different-estimator",
            },
        )
        assert unavailable.status_code == 503
        assert unavailable.json()["detail"] == "Distributed request quota is unavailable"

    engine.dispose()


def test_request_categories_separate_uploads_from_other_mutations() -> None:
    assert (
        request_rate_limit_category(
            method="POST",
            path="/v1/projects/project-1/documents",
            content_type="application/json",
        )
        == "UPLOAD"
    )
    assert (
        request_rate_limit_category(
            method="POST",
            path="/v1/projects/project-1/evidence/observations",
            content_type="application/json",
        )
        == "MUTATION"
    )
    assert (
        request_rate_limit_category(
            method="GET",
            path="/v1/projects",
            content_type=None,
        )
        == "READ"
    )
