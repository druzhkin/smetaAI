import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from tenderguard.config import get_settings


def test_idempotency_migration_backfills_existing_outbox_and_round_trips(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "idempotency-migration.db"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv(
        "AUDIT_SIGNING_KEY",
        "idempotency-migration-audit-key-at-least-32-bytes",
    )
    monkeypatch.setenv("AUDIT_SIGNING_KEY_ID", "idempotency-migration-key-1")
    get_settings.cache_clear()
    alembic_config = Config("alembic.ini")
    command.upgrade(alembic_config, "b92d5f8c0e31")
    now = datetime.now(UTC)
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO outbox_events ("
                "id, topic, aggregate_id, payload, attempts, available_at, "
                "published_at, last_error, locked_by, lease_token, lease_expires_at, "
                "last_attempt_at, dead_lettered_at, created_at"
                ") VALUES ("
                ":id, :topic, :aggregate_id, :payload, 1, :now, :now, NULL, "
                "NULL, NULL, NULL, :now, NULL, :now"
                ")"
            ),
            {
                "id": "legacy-terminal-outbox",
                "topic": "legacy.test",
                "aggregate_id": "legacy-aggregate",
                "payload": json.dumps({"legacy": True}),
                "now": now,
            },
        )
    engine.dispose()

    command.upgrade(alembic_config, "head")
    engine = create_engine(database_url)
    with engine.connect() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT deduplication_key FROM outbox_events "
                    "WHERE id = 'legacy-terminal-outbox'"
                )
            ).scalar_one()
            == "legacy-outbox:legacy-terminal-outbox"
        )
    assert "deduplication_key" in {
        column["name"] for column in inspect(engine).get_columns("outbox_events")
    }
    engine.dispose()

    command.downgrade(alembic_config, "b92d5f8c0e31")
    engine = create_engine(database_url)
    assert "deduplication_key" not in {
        column["name"] for column in inspect(engine).get_columns("outbox_events")
    }
    engine.dispose()

    command.upgrade(alembic_config, "head")
    engine = create_engine(database_url)
    with engine.connect() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT deduplication_key FROM outbox_events "
                    "WHERE id = 'legacy-terminal-outbox'"
                )
            ).scalar_one()
            == "legacy-outbox:legacy-terminal-outbox"
        )
    engine.dispose()
    get_settings.cache_clear()
