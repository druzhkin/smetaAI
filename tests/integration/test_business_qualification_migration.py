from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from tenderguard.config import get_settings
from tenderguard.infrastructure.orm import (
    BusinessQualificationCampaignRow,
    ControlledVersionRow,
)


def test_business_qualification_migration_round_trips_and_refuses_evidence_loss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "business-qualification-migration.db"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv(
        "AUDIT_SIGNING_KEY",
        "business-qualification-migration-audit-key-at-least-32-bytes",
    )
    get_settings.cache_clear()
    alembic_config = Config("alembic.ini")
    tables = {
        "business_qualification_campaigns",
        "business_qualification_cases",
        "business_qualification_references",
        "business_qualification_evaluations",
        "business_qualification_discrepancies",
        "business_qualification_discrepancy_reviews",
        "business_qualification_approvals",
    }

    command.upgrade(alembic_config, "head")
    engine = create_engine(database_url)
    assert tables <= set(inspect(engine).get_table_names())
    engine.dispose()

    command.downgrade(alembic_config, "b4d8e1f6c205")
    engine = create_engine(database_url)
    assert not tables.intersection(inspect(engine).get_table_names())
    engine.dispose()

    command.upgrade(alembic_config, "head")
    engine = create_engine(database_url)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        session.add_all(
            (
                ControlledVersionRow(
                    id="business-profile-migration",
                    kind="business_qualification_profile",
                    version_label="migration-profile",
                    content_hash="a" * 64,
                    status="APPROVED",
                    payload={},
                    approved_by="methodology-approver",
                    approved_at=now,
                ),
                ControlledVersionRow(
                    id="business-dataset-migration",
                    kind="business_qualification_dataset",
                    version_label="migration-dataset",
                    content_hash="b" * 64,
                    status="APPROVED",
                    payload={},
                    approved_by="methodology-approver",
                    approved_at=now,
                ),
            )
        )
        session.flush()
        session.add(
            BusinessQualificationCampaignRow(
                id="business-qualification-migration",
                organization_id="org-migration",
                profile_version_id="business-profile-migration",
                dataset_version_id="business-dataset-migration",
                profile_hash="a" * 64,
                dataset_hash="b" * 64,
                application_build_reference="git:" + ("c" * 40),
                status="INPUTS_LOCKED",
                input_hash="d" * 64,
                payload={"evidence": "must-not-be-dropped"},
                created_by="qualification-auditor",
                locked_at=now,
            )
        )
    engine.dispose()

    with pytest.raises(
        RuntimeError,
        match="cannot remove governed business qualification campaign evidence",
    ):
        command.downgrade(alembic_config, "b4d8e1f6c205")
    engine = create_engine(database_url)
    assert tables <= set(inspect(engine).get_table_names())
    engine.dispose()
    get_settings.cache_clear()
