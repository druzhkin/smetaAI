from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from tenderguard.config import get_settings
from tenderguard.infrastructure.database import CURRENT_SCHEMA_REVISION


def test_automation_rework_migration_creates_dispatch_table(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "automation-rework-migration.db"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv(
        "AUDIT_SIGNING_KEY",
        "automation-rework-migration-key-at-least-32-bytes",
    )
    get_settings.cache_clear()
    alembic_config = Config("alembic.ini")

    command.upgrade(alembic_config, "head")
    engine = create_engine(database_url)
    inspector = inspect(engine)
    assert "automation_rework_dispatches" in inspector.get_table_names()
    indexes = {item["name"] for item in inspector.get_indexes("automation_rework_dispatches")}
    assert {
        "ix_automation_rework_dispatches_project_id",
        "ix_automation_rework_dispatches_rework_request_id",
        "ix_automation_rework_dispatches_status",
    } <= indexes
    unique_constraints = {
        item["name"] for item in inspector.get_unique_constraints("automation_rework_dispatches")
    }
    assert {
        "uq_automation_rework_dispatch_command_event",
        "uq_automation_rework_dispatch_hash",
        "uq_automation_rework_dispatch_request",
        "uq_automation_rework_dispatch_source_event",
    } <= unique_constraints
    foreign_keys = {
        tuple(item["constrained_columns"]): item["referred_table"]
        for item in inspector.get_foreign_keys("automation_rework_dispatches")
    }
    assert foreign_keys[("worker_qualification_id",)] == "adapter_qualifications"
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            CURRENT_SCHEMA_REVISION
        )
    engine.dispose()
    get_settings.cache_clear()
