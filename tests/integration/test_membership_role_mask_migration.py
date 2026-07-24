from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from tenderguard.config import get_settings


def test_membership_role_mask_migration_backfills_and_round_trips(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "membership-role-mask.db"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv(
        "AUDIT_SIGNING_KEY",
        "membership-mask-migration-audit-key-at-least-32-bytes",
    )
    get_settings.cache_clear()
    alembic_config = Config("alembic.ini")
    command.upgrade(alembic_config, "e82f5d0b3c74")

    now = datetime(2026, 7, 24, 20, 0, tzinfo=UTC)
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO projects ("
                "id, organization_id, code, name, state, blocked_resume_state, "
                "current_document_set_revision_id, row_version, created_at, updated_at"
                ") VALUES ("
                "'project-role-mask', 'org-role-mask', 'MASK-001', 'Mask migration', "
                "'DRAFT', NULL, NULL, 1, :now, :now)"
            ),
            {"now": now},
        )
        connection.execute(
            text(
                "INSERT INTO project_memberships ("
                "id, project_id, principal_id, roles, access_level, status, version, "
                "supersedes_membership_id, changed_by, reason, created_at"
                ") VALUES ("
                "'membership-role-mask', 'project-role-mask', 'operator-role-mask', "
                ":roles, 'OWNER', 'ACTIVE', 1, NULL, 'migration-test', "
                "'Verified migration fixture', :now)"
            ),
            {
                "roles": json.dumps(["ESTIMATOR", "REVIEWER"]),
                "now": now,
            },
        )
    engine.dispose()

    command.upgrade(alembic_config, "head")
    engine = create_engine(database_url)
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT role_mask FROM project_memberships WHERE id = 'membership-role-mask'")
            ).scalar_one()
            == 9
        )
    assert "role_mask" in {
        column["name"] for column in inspect(engine).get_columns("project_memberships")
    }
    engine.dispose()

    command.downgrade(alembic_config, "e82f5d0b3c74")
    engine = create_engine(database_url)
    assert "role_mask" not in {
        column["name"] for column in inspect(engine).get_columns("project_memberships")
    }
    engine.dispose()

    command.upgrade(alembic_config, "head")
    engine = create_engine(database_url)
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT role_mask FROM project_memberships WHERE id = 'membership-role-mask'")
            ).scalar_one()
            == 9
        )
    engine.dispose()

    command.downgrade(alembic_config, "e82f5d0b3c74")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE project_memberships SET roles = :roles WHERE id = 'membership-role-mask'"),
            {"roles": json.dumps(["SYSTEM"])},
        )
    engine.dispose()
    with pytest.raises(RuntimeError, match="invalid role evidence"):
        command.upgrade(alembic_config, "head")
    engine = create_engine(database_url)
    assert "role_mask" not in {
        column["name"] for column in inspect(engine).get_columns("project_memberships")
    }
    engine.dispose()
    get_settings.cache_clear()
