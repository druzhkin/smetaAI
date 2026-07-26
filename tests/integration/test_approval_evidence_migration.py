from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from tenderguard.config import get_settings
from tenderguard.infrastructure.database import CURRENT_SCHEMA_REVISION


def test_approval_evidence_guard_migration_round_trips(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "approval-evidence-migration.db"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv(
        "AUDIT_SIGNING_KEY",
        "approval-evidence-migration-key-at-least-32-bytes",
    )
    get_settings.cache_clear()
    alembic_config = Config("alembic.ini")

    command.upgrade(alembic_config, "head")
    engine = create_engine(database_url)
    indexes = {index["name"]: index for index in inspect(engine).get_indexes("approval_records")}
    assert indexes["uq_approval_records_task_id"]["unique"] == 1
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            CURRENT_SCHEMA_REVISION
        )
    engine.dispose()

    command.downgrade(alembic_config, "fa2c5d7e9014")
    command.upgrade(alembic_config, "head")
    engine = create_engine(database_url)
    assert "approval_tasks" in inspect(engine).get_table_names()
    engine.dispose()
    get_settings.cache_clear()
