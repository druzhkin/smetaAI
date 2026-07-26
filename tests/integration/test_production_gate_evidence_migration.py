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
    ControlledVersionRow,
    ProductionGateEvidencePackageRow,
)


def test_production_gate_evidence_migration_refuses_evidence_loss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "production-gate-evidence-migration.db"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv(
        "AUDIT_SIGNING_KEY",
        "production-evidence-migration-audit-key-at-least-32-bytes",
    )
    get_settings.cache_clear()
    alembic_config = Config("alembic.ini")
    tables = {
        "production_gate_evidence_packages",
        "production_gate_evidence_approvals",
        "production_gate_evidence_revocations",
    }

    command.upgrade(alembic_config, "head")
    engine = create_engine(database_url)
    assert tables <= set(inspect(engine).get_table_names())
    engine.dispose()

    command.downgrade(alembic_config, "c7e9a2d4f681")
    engine = create_engine(database_url)
    assert not tables.intersection(inspect(engine).get_table_names())
    engine.dispose()

    command.upgrade(alembic_config, "head")
    engine = create_engine(database_url)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        session.add(
            ControlledVersionRow(
                id="production-evidence-profile-migration",
                kind="production_gate_evidence_profile",
                version_label="migration-production-evidence-profile",
                content_hash="a" * 64,
                status="APPROVED",
                payload={},
                approved_by="profile-approver",
                approved_at=now,
            )
        )
        session.flush()
        session.add(
            ProductionGateEvidencePackageRow(
                id="qualification-evidence-migration",
                organization_id="org-migration",
                gate_name="security_review",
                profile_version_id="production-evidence-profile-migration",
                profile_content_hash="a" * 64,
                application_build_reference="git:" + ("b" * 40),
                environment="qualification",
                evidence_mode="EXTERNAL_ATTESTED_PACKAGE",
                package_hash="c" * 64,
                statement_payload={"evidence": "must-not-be-dropped"},
                technical_result_payload=None,
                attester_id="attester",
                attester_key_id="key-1",
                attestation_signature_b64="signature",
                submitted_by="evidence-registrar",
                submitted_at=now,
            )
        )
    engine.dispose()

    with pytest.raises(
        RuntimeError,
        match="cannot remove governed production gate evidence",
    ):
        command.downgrade(alembic_config, "c7e9a2d4f681")
    engine = create_engine(database_url)
    assert tables <= set(inspect(engine).get_table_names())
    engine.dispose()
    get_settings.cache_clear()
