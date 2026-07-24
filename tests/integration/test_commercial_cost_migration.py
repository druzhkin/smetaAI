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
    CommercialCostModelRow,
    ControlledVersionRow,
    DocumentSetRevisionRow,
    ProjectRow,
)


def test_commercial_cost_migration_round_trips_empty_and_refuses_evidence_loss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "commercial-cost-migration.db"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv(
        "AUDIT_SIGNING_KEY",
        "commercial-cost-migration-audit-key-at-least-32-bytes",
    )
    get_settings.cache_clear()
    alembic_config = Config("alembic.ini")

    command.upgrade(alembic_config, "head")
    engine = create_engine(database_url)
    assert "commercial_cost_models" in inspect(engine).get_table_names()
    engine.dispose()

    command.downgrade(alembic_config, "ca3e6a9d1f42")
    engine = create_engine(database_url)
    assert "commercial_cost_models" not in inspect(engine).get_table_names()
    engine.dispose()

    command.upgrade(alembic_config, "head")
    engine = create_engine(database_url)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        session.add(
            ProjectRow(
                id="project-migration-commercial",
                organization_id="org-migration",
                code="MIG-COMMERCIAL",
                name="Migration evidence",
                state="PRICING_IN_PROGRESS",
                row_version=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        session.add_all(
            (
                ControlledVersionRow(
                    id="commercial-policy-migration",
                    kind="commercial_cost_model",
                    version_label="migration",
                    content_hash="a" * 64,
                    status="APPROVED",
                    payload={},
                    approved_by="owner",
                    approved_at=now,
                ),
                DocumentSetRevisionRow(
                    id="document-set-migration-commercial",
                    project_id="project-migration-commercial",
                    manifest_hash="b" * 64,
                    revision_ids=[],
                    status="CONFIRMED",
                    created_by="controller",
                    created_at=now,
                    confirmed_by="reviewer",
                    confirmed_at=now,
                ),
                BoqLineRow(
                    id="line-migration-commercial",
                    project_id="project-migration-commercial",
                    line_key="migration-commercial",
                    wbs_node_id="wbs-migration",
                    work_code="MIGRATION_COMMERCIAL",
                    description="Migration commercial cost",
                    unit="lot",
                    status="VERIFIED",
                    supersedes_line_id=None,
                    is_current=True,
                    payload={},
                    created_at=now,
                    updated_at=now,
                ),
            )
        )
        session.flush()
        session.add(
            CommercialCostModelRow(
                id="commercial-model-migration",
                project_id="project-migration-commercial",
                model_kind="LOGISTICS",
                status="REVIEW_REQUIRED",
                target_line_id="line-migration-commercial",
                target_semantic_key="migration-commercial",
                category="LOGISTICS",
                policy_version_id="commercial-policy-migration",
                document_set_revision_id="document-set-migration-commercial",
                currency="RUB",
                total=Decimal("1"),
                independent_total=Decimal("1"),
                input_hash="c" * 64,
                output_hash="d" * 64,
                payload={"basis_type": "DERIVED_COMMERCIAL_COST"},
                approval_task_ids=["approval-task-migration"],
                approval_record_ids=None,
                supersedes_model_id=None,
                is_current=False,
                created_by="estimator",
                finalized_by=None,
                created_at=now,
                finalized_at=None,
            )
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="Cannot downgrade"):
        command.downgrade(alembic_config, "ca3e6a9d1f42")
    get_settings.cache_clear()
