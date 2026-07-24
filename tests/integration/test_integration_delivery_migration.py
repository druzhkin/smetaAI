import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tenderguard.config import get_settings
from tenderguard.infrastructure.orm import (
    AdapterQualificationRow,
    ConnectorDeliveryAttemptRow,
)


def test_integration_delivery_migration_backfills_and_refuses_evidence_loss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "integration-delivery-migration.db"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv(
        "AUDIT_SIGNING_KEY",
        "integration-migration-audit-key-at-least-32-bytes",
    )
    get_settings.cache_clear()
    alembic_config = Config("alembic.ini")
    command.upgrade(alembic_config, "d71e4c9a2b63")
    now = datetime.now(UTC)
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO outbox_events ("
                "id, deduplication_key, topic, aggregate_id, payload, attempts, "
                "available_at, published_at, last_error, locked_by, lease_token, "
                "lease_expires_at, last_attempt_at, dead_lettered_at, created_at"
                ") VALUES ("
                ":id, :deduplication_key, :topic, :aggregate_id, :payload, 0, "
                ":now, NULL, NULL, NULL, NULL, NULL, NULL, NULL, :now"
                ")"
            ),
            {
                "id": "outbox-before-integration-migration",
                "deduplication_key": "pre-integration-delivery-key",
                "topic": "integration.test",
                "aggregate_id": "aggregate-migration",
                "payload": json.dumps({"organization_id": "org-migration"}),
                "now": now,
            },
        )
    engine.dispose()

    command.upgrade(alembic_config, "head")
    engine = create_engine(database_url)
    assert {
        "connector_delivery_attempts",
        "outbox_replays",
        "integration_inbox_messages",
        "integration_inbox_processings",
    }.issubset(inspect(engine).get_table_names())
    with engine.connect() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT delivery_deduplication_key FROM outbox_events "
                    "WHERE id = 'outbox-before-integration-migration'"
                )
            ).scalar_one()
            == "pre-integration-delivery-key"
        )
    engine.dispose()

    command.downgrade(alembic_config, "d71e4c9a2b63")
    engine = create_engine(database_url)
    assert "connector_delivery_attempts" not in inspect(engine).get_table_names()
    assert "delivery_deduplication_key" not in {
        column["name"] for column in inspect(engine).get_columns("outbox_events")
    }
    engine.dispose()

    command.upgrade(alembic_config, "head")
    engine = create_engine(database_url)
    with Session(engine) as session, session.begin():
        session.add(
            AdapterQualificationRow(
                id="qualification-integration-migration",
                adapter_name="migration-connector",
                adapter_version="1",
                status="APPROVED",
                valid_until=None,
                test_evidence_hash="a" * 64,
                payload={
                    "organization_id": "org-migration",
                    "supported_methods": ["INTEGRATION_OUTBOUND_DELIVERY"],
                    "service_actor_id": "migration-worker",
                },
                approved_by="methodology-owner",
                approved_at=now,
            )
        )
        session.flush()
        session.add(
            ConnectorDeliveryAttemptRow(
                id="connector-attempt-migration",
                outbox_event_id="outbox-before-integration-migration",
                connector_qualification_id="qualification-integration-migration",
                attempt_number=1,
                status="RETRYABLE_FAILURE",
                envelope_hash="b" * 64,
                receipt_hash=None,
                external_message_id=None,
                error_code="MIGRATION_TEST_FAILURE",
                payload={},
                started_at=now,
                completed_at=now,
            )
        )
    with (
        pytest.raises(IntegrityError, match="ck_connector_delivery_attempt_timing"),
        engine.begin() as connection,
    ):
        connection.execute(
            text(
                "INSERT INTO connector_delivery_attempts ("
                "id, outbox_event_id, connector_qualification_id, attempt_number, "
                "status, envelope_hash, receipt_hash, external_message_id, error_code, "
                "payload, started_at, completed_at"
                ") VALUES ("
                "'connector-attempt-invalid', 'outbox-before-integration-migration', "
                "'qualification-integration-migration', 0, 'RETRYABLE_FAILURE', :hash, "
                "NULL, NULL, 'INVALID_ATTEMPT', '{}', :now, :now"
                ")"
            ),
            {"hash": "c" * 64, "now": now},
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="Cannot downgrade"):
        command.downgrade(alembic_config, "d71e4c9a2b63")
    get_settings.cache_clear()
