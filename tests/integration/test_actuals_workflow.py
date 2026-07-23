from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from tenderguard.application.actuals import (
    ActualRecordDraft,
    ActualsService,
    CompareActualCommand,
)
from tenderguard.config import Settings
from tenderguard.domain.enums import ActorRole, ApprovalState, EvidenceMethod, VarianceReason
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.database import (
    create_database_engine,
    create_schema_for_tests,
    create_session_factory,
)
from tenderguard.infrastructure.object_store import LocalObjectStore
from tenderguard.infrastructure.orm import (
    CalculationRunRow,
    CalculationSnapshotRow,
    CalibrationExampleRow,
    CostInputRow,
    ObservationRow,
    ProjectRow,
)


def test_verified_actual_becomes_calibration_label_only_after_reason_and_approval(
    tmp_path: Path,
) -> None:
    settings = Settings(
        app_env="test",
        database_url="sqlite+pysqlite://",
        local_object_store_path=tmp_path / "objects",
        audit_signing_key="test-audit-signing-key-at-least-32-bytes",
    )
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    factory = create_session_factory(engine)
    store = LocalObjectStore(tmp_path / "objects")
    recorder = Actor("controller-1", "org-1", frozenset({ActorRole.PROCUREMENT}))
    reviewer = Actor("auditor-1", "org-1", frozenset({ActorRole.AUDITOR}))
    owner = Actor(
        "methodology-owner",
        "org-1",
        frozenset({ActorRole.METHODOLOGY_OWNER}),
    )
    now = datetime(2026, 7, 23, tzinfo=UTC)

    with factory.begin() as session:
        session.add(
            ProjectRow(
                id="project-actuals",
                organization_id="org-1",
                code="ACTUAL-1",
                name="Actuals workflow",
                state=ApprovalState.APPROVED_FOR_BID.value,
                row_version=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            CalculationRunRow(
                id="calculation-run-actuals",
                project_id="project-actuals",
                engine_version="calculation-model-v1",
                status="VALIDATED",
                currency="RUB",
                grand_total=Decimal("1000"),
                payload={},
                created_at=now,
            )
        )
        session.add(
            CalculationSnapshotRow(
                id="snapshot-actuals",
                project_id="project-actuals",
                calculation_run_id="calculation-run-actuals",
                document_set_revision_id="document-set-1",
                input_hash="a" * 64,
                output_hash="b" * 64,
                snapshot_hash="c" * 64,
                fixed=True,
                object_key="snapshots/" + "c" * 64,
                created_by="estimator-1",
                created_at=now,
            )
        )
        session.add(
            CostInputRow(
                id="cost-input-pipe",
                project_id="project-actuals",
                calculation_run_id="calculation-run-actuals",
                semantic_key="pipe",
                category="MATERIAL",
                amount_basis_id="observation-forecast-price",
                payload={
                    "cost_input_id": "pipe-cost",
                    "line_id": "boq-line-pipe",
                    "wbs_node_id": "wbs-pipe",
                    "semantic_key": "pipe",
                    "category": "MATERIAL",
                    "quantity": "10",
                    "unit": "m",
                    "unit_rate": "100",
                    "currency": "RUB",
                    "source_observation_id": "observation-forecast-price",
                },
                created_at=now,
            )
        )
        session.add(
            ObservationRow(
                id="observation-actual-price",
                project_id="project-actuals",
                document_revision_id="invoice-revision-1",
                field_name="actual_unit_rate",
                method=EvidenceMethod.RULE_ENGINE.value,
                method_version="reconciliation-v1",
                status="VERIFIED",
                payload={
                    "observation": {
                        "value": "112.0",
                        "unit": "RUB/m",
                    }
                },
                created_at=now,
            )
        )

    with factory.begin() as session:
        service = ActualsService(session=session, settings=settings, object_store=store)
        recorded = service.record_actual(
            actor=recorder,
            project_id="project-actuals",
            draft=ActualRecordDraft(
                actual_key="pipe-price-2026-08",
                entity_type="COST_INPUT",
                entity_id="cost-input-pipe",
                metric="unit_rate",
                value=Decimal("112"),
                unit="RUB/m",
                source_observation_id="observation-actual-price",
                occurred_on=date(2026, 8, 1),
            ),
            request_id="request-record-actual",
            reason="Record accepted supplier invoice rate",
        )
        assert not recorded.actual.verified
        verified = service.verify_actual(
            actor=reviewer,
            project_id="project-actuals",
            actual_id=recorded.actual.actual_id,
            request_id="request-verify-actual",
            reason="Reconcile invoice to accepted delivery",
        )
        assert verified.actual.verified
        comparison = service.compare_to_forecast(
            actor=reviewer,
            project_id="project-actuals",
            actual_id=recorded.actual.actual_id,
            command=CompareActualCommand(
                snapshot_id="snapshot-actuals",
                cost_input_row_id="cost-input-pipe",
                forecast_metric="unit_rate",
                reason=VarianceReason.PRICE_CHANGE,
                reason_detail="Supplier indexation accepted under the purchase order",
            ),
            request_id="request-classify-variance",
            reason="Classify verified forecast-to-actual variance",
        )
        assert comparison.variance.absolute_variance == Decimal("12")
        assert not comparison.calibration_approved
        example = session.get(
            CalibrationExampleRow,
            comparison.calibration_example.example_id,
        )
        assert example is not None and not example.approved
        approved = service.approve_calibration_example(
            actor=owner,
            project_id="project-actuals",
            example_id=comparison.calibration_example.example_id,
            request_id="request-approve-calibration",
            reason="Verified actual and variance classification are suitable for calibration",
        )
        assert approved.target_value == Decimal("112")
        assert example.approved

    engine.dispose()
