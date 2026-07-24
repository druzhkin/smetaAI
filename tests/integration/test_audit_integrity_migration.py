import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from tenderguard.config import get_settings
from tenderguard.domain.audit import GENESIS_HASH
from tenderguard.domain.common import content_hash, signed_hash

CORRECT_KEY_ID = "migration-legacy-key-1"
CORRECT_KEY = "migration-legacy-audit-key-at-least-32-bytes"


def _set_migration_environment(
    monkeypatch: pytest.MonkeyPatch,
    *,
    database_url: str,
    signing_key: str,
) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("AUDIT_SIGNING_KEY_ID", CORRECT_KEY_ID)
    monkeypatch.setenv("AUDIT_SIGNING_KEY", signing_key)
    get_settings.cache_clear()


def _insert_legacy_project_and_event(database_url: str) -> None:
    occurred_at = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
    unsigned = {
        "sequence": 1,
        "event_id": "legacy-event-1",
        "aggregate_type": "project",
        "aggregate_id": "legacy-project-1",
        "event_type": "project_created",
        "actor_id": "legacy-user",
        "actor_roles": ("ADMIN",),
        "request_id": "legacy-request",
        "reason": "Verified legacy migration fixture",
        "occurred_at": occurred_at,
        "payload": {
            "code": "LEGACY-001",
            "name": "Legacy tender",
            "state": "DRAFT",
        },
        "previous_hash": GENESIS_HASH,
    }
    event_hash = content_hash(unsigned)
    signature = signed_hash(
        {"event_hash": event_hash},
        CORRECT_KEY.encode("utf-8"),
    )
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO projects ("
                "id, organization_id, code, name, state, blocked_resume_state, "
                "current_document_set_revision_id, row_version, created_at, updated_at"
                ") VALUES ("
                ":id, :organization_id, :code, :name, :state, NULL, NULL, 1, "
                ":created_at, :updated_at"
                ")"
            ),
            {
                "id": "legacy-project-1",
                "organization_id": "legacy-org",
                "code": "LEGACY-001",
                "name": "Legacy tender",
                "state": "DRAFT",
                "created_at": occurred_at,
                "updated_at": occurred_at,
            },
        )
        connection.execute(
            text(
                "INSERT INTO audit_events ("
                "id, aggregate_type, aggregate_id, sequence, event_type, actor_id, "
                "actor_roles, request_id, reason, payload, previous_hash, event_hash, "
                "signature, occurred_at"
                ") VALUES ("
                ":id, :aggregate_type, :aggregate_id, :sequence, :event_type, :actor_id, "
                ":actor_roles, :request_id, :reason, :payload, :previous_hash, "
                ":event_hash, :signature, :occurred_at"
                ")"
            ),
            {
                "id": unsigned["event_id"],
                "aggregate_type": unsigned["aggregate_type"],
                "aggregate_id": unsigned["aggregate_id"],
                "sequence": unsigned["sequence"],
                "event_type": unsigned["event_type"],
                "actor_id": unsigned["actor_id"],
                "actor_roles": json.dumps(unsigned["actor_roles"]),
                "request_id": unsigned["request_id"],
                "reason": unsigned["reason"],
                "payload": json.dumps(unsigned["payload"]),
                "previous_hash": unsigned["previous_hash"],
                "event_hash": event_hash,
                "signature": signature,
                "occurred_at": occurred_at,
            },
        )
    engine.dispose()


def test_audit_key_version_migration_verifies_legacy_history_before_ddl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "legacy-audit.db"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    alembic_config = Config("alembic.ini")
    _set_migration_environment(
        monkeypatch,
        database_url=database_url,
        signing_key=CORRECT_KEY,
    )
    command.upgrade(alembic_config, "f42d8a1b6c53")
    _insert_legacy_project_and_event(database_url)
    command.upgrade(alembic_config, "a81c4e7d9b20")
    engine = create_engine(database_url)
    with engine.connect() as connection:
        membership = connection.execute(
            text(
                "SELECT principal_id, access_level, status "
                "FROM project_memberships WHERE project_id = 'legacy-project-1'"
            )
        ).one()
        assert membership == ("legacy-user", "OWNER", "ACTIVE")
    engine.dispose()

    _set_migration_environment(
        monkeypatch,
        database_url=database_url,
        signing_key="wrong-migration-audit-key-at-least-32-bytes",
    )
    with pytest.raises(RuntimeError, match="every legacy audit chain must verify"):
        command.upgrade(alembic_config, "head")
    engine = create_engine(database_url)
    assert "signing_key_id" not in {
        column["name"] for column in inspect(engine).get_columns("audit_events")
    }
    engine.dispose()

    _set_migration_environment(
        monkeypatch,
        database_url=database_url,
        signing_key=CORRECT_KEY,
    )
    command.upgrade(alembic_config, "head")
    engine = create_engine(database_url)
    with engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT signing_key_id, signature_version "
                "FROM audit_events WHERE id = 'legacy-event-1'"
            )
        ).one() == (CORRECT_KEY_ID, "HMAC-SHA256-V1")
    with pytest.raises(RuntimeError, match="after audit evidence exists"):
        command.downgrade(alembic_config, "a81c4e7d9b20")
    engine.dispose()
    get_settings.cache_clear()
