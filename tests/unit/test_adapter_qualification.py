from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from tenderguard.application.projects import ProjectService
from tenderguard.config import Settings
from tenderguard.infrastructure.database import (
    create_database_engine,
    create_schema_for_tests,
    create_session_factory,
)
from tenderguard.infrastructure.object_store import LocalObjectStore
from tenderguard.infrastructure.orm import AdapterQualificationRow


def test_normative_adapter_requires_current_database_qualification(tmp_path: Path) -> None:
    settings = Settings(
        app_env="test",
        database_url="sqlite+pysqlite://",
        local_object_store_path=tmp_path / "objects",
        normative_adapter="licensed-engine",
        normative_adapter_qualification_id="qualification-1",
        audit_signing_key="test-audit-signing-key-at-least-32-bytes",
    )
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    session_factory = create_session_factory(engine)
    store = LocalObjectStore(tmp_path / "objects")

    with session_factory() as session:
        service = ProjectService(session=session, settings=settings, object_store=store)
        assert not service.normative_engine_qualified()

        session.add(
            AdapterQualificationRow(
                id="qualification-1",
                adapter_name="licensed-engine",
                adapter_version="1.0",
                status="APPROVED",
                valid_until=date.today() - timedelta(days=1),
                test_evidence_hash="a" * 64,
                payload={"test_suite": "qualified-golden-set-v1"},
                approved_by="methodology-owner-2",
                approved_at=datetime.now(UTC),
            )
        )
        session.flush()
        assert not service.normative_engine_qualified()

        qualification = session.get(AdapterQualificationRow, "qualification-1")
        assert qualification is not None
        qualification.valid_until = date.today() + timedelta(days=30)
        session.flush()
        assert service.normative_engine_qualified()

    engine.dispose()
