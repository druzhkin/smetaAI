from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from tenderguard.config import get_settings
from tenderguard.infrastructure.orm import (
    BoqLineRow,
    ManualChangeRow,
    ProjectRow,
    QuantityManualChangeApplicationRow,
    QuantityRow,
)


def test_quantity_manual_change_migration_round_trips_and_refuses_evidence_loss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "quantity-manual-change-migration.db"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv(
        "AUDIT_SIGNING_KEY",
        "quantity-change-migration-audit-key-at-least-32-bytes",
    )
    get_settings.cache_clear()
    alembic_config = Config("alembic.ini")

    command.upgrade(alembic_config, "head")
    engine = create_engine(database_url)
    assert "quantity_manual_change_applications" in inspect(engine).get_table_names()
    engine.dispose()

    command.downgrade(alembic_config, "f6a9c3d2e841")
    engine = create_engine(database_url)
    assert "quantity_manual_change_applications" not in inspect(engine).get_table_names()
    engine.dispose()

    command.upgrade(alembic_config, "head")
    engine = create_engine(database_url)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        session.add(
            ProjectRow(
                id="project-quantity-change-migration",
                organization_id="org-migration",
                code="MIG-QTY-CHANGE",
                name="Quantity manual-change migration evidence",
                state="BOQ_REVIEW",
                row_version=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        session.add(
            BoqLineRow(
                id="line-quantity-change-migration",
                project_id="project-quantity-change-migration",
                line_key="migration-line",
                wbs_node_id="wbs-migration",
                work_code="MIGRATION_WORK",
                description="Migration line",
                unit="m",
                status="VERIFIED",
                supersedes_line_id=None,
                is_current=True,
                payload={},
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        session.add(
            QuantityRow(
                id="quantity-change-migration",
                boq_line_id="line-quantity-change-migration",
                value=Decimal("2"),
                unit="m",
                status="VERIFIED",
                supersedes_quantity_id=None,
                is_current=True,
                payload={"record": {"manual_change_id": "manual-change-migration"}},
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            ManualChangeRow(
                id="manual-change-migration",
                project_id="project-quantity-change-migration",
                entity_type="quantity",
                entity_id="line-quantity-change-migration",
                field_name="record",
                critical=True,
                changed_by="estimator-migration",
                reason="Migration evidence",
                payload={"lifecycle_version": "quantity-manual-change-v1"},
                changed_at=now,
            )
        )
        session.flush()
        session.add(
            QuantityManualChangeApplicationRow(
                id="manual-change-application-migration",
                project_id="project-quantity-change-migration",
                manual_change_id="manual-change-migration",
                quantity_id="quantity-change-migration",
                applied_by="estimator-migration",
                payload={"evidence": "must-not-be-dropped"},
                applied_at=now,
            )
        )
    engine.dispose()

    with pytest.raises(
        RuntimeError,
        match="cannot remove governed quantity manual-change application evidence",
    ):
        command.downgrade(alembic_config, "f6a9c3d2e841")
    engine = create_engine(database_url)
    assert "quantity_manual_change_applications" in inspect(engine).get_table_names()
    engine.dispose()
    get_settings.cache_clear()
