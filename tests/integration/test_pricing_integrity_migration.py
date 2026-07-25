from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from tenderguard.config import get_settings


def test_pricing_integrity_guard_migration_round_trips(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "pricing-integrity-migration.db"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv(
        "AUDIT_SIGNING_KEY",
        "pricing-integrity-migration-key-at-least-32-bytes",
    )
    get_settings.cache_clear()
    alembic_config = Config("alembic.ini")

    command.upgrade(alembic_config, "head")
    engine = create_engine(database_url)
    assert "price_quotes" in inspect(engine).get_table_names()
    assert "normalized_prices" in inspect(engine).get_table_names()
    assert "price_decisions" in inspect(engine).get_table_names()
    engine.dispose()

    command.downgrade(alembic_config, "b8d2e7f4a961")
    command.upgrade(alembic_config, "head")
    engine = create_engine(database_url)
    assert "price_decisions" in inspect(engine).get_table_names()
    engine.dispose()
    get_settings.cache_clear()
