from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from tenderguard.config import get_settings
from tenderguard.infrastructure.database import CURRENT_SCHEMA_REVISION


def test_final_review_migration_creates_immutable_request_table(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "final-review-migration.db"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv(
        "AUDIT_SIGNING_KEY",
        "final-review-migration-key-at-least-32-bytes",
    )
    get_settings.cache_clear()
    alembic_config = Config("alembic.ini")

    command.upgrade(alembic_config, "head")
    engine = create_engine(database_url)
    inspector = inspect(engine)
    assert "expert_rework_requests" in inspector.get_table_names()
    indexes = {item["name"] for item in inspector.get_indexes("expert_rework_requests")}
    assert {
        "ix_expert_rework_requests_project_id",
        "ix_expert_rework_requests_snapshot_id",
        "ix_expert_rework_requests_gate_hash",
    } <= indexes
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            CURRENT_SCHEMA_REVISION
        )
    engine.dispose()
    get_settings.cache_clear()
