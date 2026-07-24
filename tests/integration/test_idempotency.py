from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from tenderguard.api.main import create_app
from tenderguard.config import Settings
from tenderguard.infrastructure.database import (
    create_database_engine,
    create_schema_for_tests,
    create_session_factory,
)
from tenderguard.infrastructure.object_store import LocalObjectStore
from tenderguard.infrastructure.orm import (
    AuditEventRow,
    IdempotencyRecordRow,
    OutboxEventRow,
    ProjectRow,
)


def _headers(actor_id: str, idempotency_key: str | None = None) -> dict[str, str]:
    headers = {
        "X-Dev-Actor": actor_id,
        "X-Dev-Organization": "org-idempotency",
        "X-Dev-Roles": "ESTIMATOR",
    }
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def test_mutations_use_atomic_persisted_idempotency_and_universal_outbox(
    tmp_path: Path,
) -> None:
    settings = Settings(
        app_env="test",
        database_url="sqlite+pysqlite://",
        local_object_store_path=tmp_path / "objects",
        allow_insecure_dev_auth=True,
        audit_signing_key="idempotency-test-audit-key-at-least-32-bytes",
        audit_signing_key_id="idempotency-test-audit-key-1",
        require_idempotency_keys=True,
    )
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    app = create_app(
        settings,
        engine=engine,
        object_store=LocalObjectStore(tmp_path / "objects"),
    )
    body = {
        "code": "IDEM-001",
        "name": "Atomic retry",
        "reason": "Register exactly once",
    }
    with TestClient(app) as client:
        openapi = client.get("/openapi.json").json()
        idempotency_parameter = next(
            parameter
            for parameter in openapi["paths"]["/v1/projects"]["post"]["parameters"]
            if parameter["name"] == "Idempotency-Key"
        )
        assert idempotency_parameter["required"] is True

        missing = client.post(
            "/v1/projects",
            headers=_headers("estimator-idempotency"),
            json=body,
        )
        assert missing.status_code == 428

        invalid = client.post(
            "/v1/projects",
            headers=_headers("estimator-idempotency", "short"),
            json=body,
        )
        assert invalid.status_code == 422

        first = client.post(
            "/v1/projects",
            headers={
                **_headers("estimator-idempotency", "project-create-0001"),
                "X-Request-Id": "first-network-attempt",
            },
            json=body,
        )
        assert first.status_code == 201
        assert first.headers["Idempotency-Replayed"] == "false"

        replay = client.post(
            "/v1/projects",
            headers={
                **_headers("estimator-idempotency", "project-create-0001"),
                "X-Request-Id": "retry-after-timeout",
            },
            json=body,
        )
        assert replay.status_code == 201
        assert replay.json() == first.json()
        assert replay.headers["Idempotency-Replayed"] == "true"

        conflict = client.post(
            "/v1/projects",
            headers=_headers("estimator-idempotency", "project-create-0001"),
            json={**body, "name": "Changed payload must not reuse the key"},
        )
        assert conflict.status_code == 409

        other_actor = client.post(
            "/v1/projects",
            headers=_headers("estimator-other", "project-create-0001"),
            json={
                "code": "IDEM-002",
                "name": "Actor-scoped key",
                "reason": "Same key is valid in another actor scope",
            },
        )
        assert other_actor.status_code == 201

        failed = client.post(
            "/v1/projects",
            headers=_headers("estimator-idempotency", "rollback-retry-0001"),
            json={**body, "name": "Duplicate project code"},
        )
        assert failed.status_code == 409
        recovered = client.post(
            "/v1/projects",
            headers=_headers("estimator-idempotency", "rollback-retry-0001"),
            json={
                "code": "IDEM-003",
                "name": "Retry after rolled-back failure",
                "reason": "Failed attempts must not poison the key",
            },
        )
        assert recovered.status_code == 201

        with create_session_factory(engine)() as session:
            assert session.scalar(select(func.count()).select_from(ProjectRow)) == 3
            assert session.scalar(select(func.count()).select_from(IdempotencyRecordRow)) == 3
            first_project_id = first.json()["id"]
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(AuditEventRow)
                    .where(
                        AuditEventRow.aggregate_type == "project",
                        AuditEventRow.aggregate_id == first_project_id,
                    )
                )
                == 2
            )
            universal_events = list(
                session.scalars(
                    select(OutboxEventRow).where(OutboxEventRow.topic == "audit.event.recorded")
                )
            )
            assert universal_events
            assert len({event.deduplication_key for event in universal_events}) == len(
                universal_events
            )
            assert all(
                event.deduplication_key == f"audit-event:{event.payload['audit_event_id']}"
                for event in universal_events
            )
