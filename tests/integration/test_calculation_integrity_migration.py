from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from tenderguard.config import get_settings


def test_calculation_integrity_guard_migration_round_trips(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "calculation-integrity-migration.db"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv(
        "AUDIT_SIGNING_KEY",
        "calculation-integrity-migration-key-at-least-32-bytes",
    )
    get_settings.cache_clear()
    alembic_config = Config("alembic.ini")

    command.upgrade(alembic_config, "head")
    engine = create_engine(database_url)
    tables = set(inspect(engine).get_table_names())
    assert {
        "calculation_runs",
        "atomic_cost_inputs",
        "release_decisions",
        "scenario_runs",
    } <= tables
    engine.dispose()

    command.downgrade(alembic_config, "a3f7c9d2e614")
    command.upgrade(alembic_config, "head")
    engine = create_engine(database_url)
    assert "calculation_runs" in inspect(engine).get_table_names()
    engine.dispose()
    get_settings.cache_clear()
