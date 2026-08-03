from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import pytest
from sqlalchemy import select

from tenderguard.application.actuals import (
    ActualDecisionCommand,
    ActualRecordDraft,
    ActualsService,
    CalibrationDecisionCommand,
    CompareActualCommand,
    VarianceDecisionCommand,
)
from tenderguard.application.projects import OptimisticLockError
from tenderguard.config import Settings
from tenderguard.domain.calculation import (
    AtomicCostInput,
    CalculationPolicy,
    calculate_primary,
    create_snapshot,
    validate_independently,
)
from tenderguard.domain.common import canonical_json, ensure_utc
from tenderguard.domain.enums import (
    ActorRole,
    ApprovalDecision,
    ApprovalState,
    CostCategory,
    EvidenceMethod,
    VarianceReason,
    VerificationStatus,
)
from tenderguard.domain.models import EvidenceLocation, Observation
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.database import (
    create_database_engine,
    create_schema_for_tests,
    create_session_factory,
)
from tenderguard.infrastructure.object_store import LocalObjectStore
from tenderguard.infrastructure.orm import (
    ActualRecordRow,
    ApprovalTaskRow,
    AuditEventRow,
    CalculationRunRow,
    CalculationSnapshotRow,
    CalibrationExampleRow,
    ConflictRow,
    ControlledVersionRow,
    CostInputRow,
    DocumentRevisionRow,
    DocumentRow,
    ObservationRow,
    ProjectRow,
    ReleaseDecisionRow,
    VarianceRecordRow,
)
from tests.integration.support import (
    add_governed_controlled_version,
    add_project_controlled_version_binding,
    approval_task_updated_at,
    project_memberships,
)


def _actor(actor_id: str, role: ActorRole) -> Actor:
    return Actor(actor_id, "org-1", frozenset({role}))


def _observation(
    *,
    observation_id: str,
    field_name: str,
    value: object,
    document_id: str,
    revision_id: str,
    object_hash: str,
    actor_id: str,
    now: datetime,
) -> ObservationRow:
    observation = Observation(
        observation_id=observation_id,
        field_name=field_name,
        value=value,
        unit=None,
        method=EvidenceMethod.RULE_ENGINE,
        method_version="actuals-import-v1",
        source_priority=1,
        location=EvidenceLocation(
            document_id=document_id,
            document_revision_id=revision_id,
            original_object_hash=object_hash,
            locator_kind="record",
            locator=f"{field_name}:1",
        ),
        observed_at=now,
        actor_id=actor_id,
        status=VerificationStatus.VERIFIED,
    )
    return ObservationRow(
        id=observation.observation_id,
        project_id="project-actuals",
        document_revision_id=revision_id,
        field_name=field_name,
        method=observation.method.value,
        method_version=observation.method_version,
        status=observation.status.value,
        payload={"observation": observation.model_dump(mode="json")},
        created_at=now,
    )


def test_actual_variance_and_calibration_require_three_separate_decisions(
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
    recorder = Actor(
        "actual-recorder",
        "org-1",
        frozenset({ActorRole.PROCUREMENT, ActorRole.AUDITOR}),
    )
    actual_reviewer = _actor("actual-reviewer", ActorRole.AUDITOR)
    classifier = Actor(
        "variance-classifier",
        "org-1",
        frozenset({ActorRole.REVIEWER, ActorRole.TECHNICAL_EXPERT}),
    )
    variance_reviewer = Actor(
        "variance-reviewer",
        "org-1",
        frozenset({ActorRole.TECHNICAL_EXPERT, ActorRole.METHODOLOGY_OWNER}),
    )
    calibration_owner = _actor("calibration-owner", ActorRole.METHODOLOGY_OWNER)
    methodology_creator = _actor("methodology-creator", ActorRole.METHODOLOGY_OWNER)
    methodology_approver = _actor("methodology-approver", ActorRole.METHODOLOGY_OWNER)
    now = datetime(2026, 7, 23, tzinfo=UTC)
    actors = (
        recorder,
        actual_reviewer,
        classifier,
        variance_reviewer,
        calibration_owner,
        methodology_creator,
        methodology_approver,
    )

    with factory.begin() as session:
        policy_version = ControlledVersionRow(
            id="actuals-policy-v1",
            kind="actuals_policy",
            version_label="1",
            content_hash="",
            status="DRAFT",
            payload={
                "metric_definitions": [
                    {
                        "metric": "unit_rate",
                        "entity_type": "COST_INPUT",
                        "evidence_field_name": "actual_unit_rate",
                        "forecast_basis": "ATOMIC_UNIT_RATE",
                        "allowed_units": ["RUB/m"],
                        "allowed_source_classes": ["SUPPLIER_INVOICE"],
                    }
                ],
                "required_metric_keys": ["unit_rate"],
                "independently_verified_metric_keys": [],
                "record_roles": ["PROCUREMENT"],
                "actual_review_role": "AUDITOR",
                "variance_classifier_roles": ["REVIEWER"],
                "variance_review_role": "TECHNICAL_EXPERT",
                "calibration_approval_role": "METHODOLOGY_OWNER",
                "project_outcome_field_name": "project_execution_status",
                "eligible_project_outcomes": ["COMPLETED"],
                "relative_variance_scale": 6,
                "relative_variance_rounding_mode": "ROUND_HALF_EVEN",
            },
            approved_by=None,
            approved_at=None,
        )
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
        session.add_all(
            project_memberships(
                "project-actuals",
                actors,
                owner_id=recorder.actor_id,
                now=now,
            )
        )
        session.flush()
        add_governed_controlled_version(
            session=session,
            settings=settings,
            object_store=store,
            row=policy_version,
            organization_id="org-1",
            creator=methodology_creator,
            approver=methodology_approver,
        )
        add_project_controlled_version_binding(
            session=session,
            settings=settings,
            object_store=store,
            project_id="project-actuals",
            version=policy_version,
            purpose="actuals_policy",
            actor=methodology_approver,
        )
        for document_id, revision_id, object_hash, title in (
            ("document-outcome", "revision-outcome", "a" * 64, "Completion act"),
            ("document-invoice", "revision-invoice", "b" * 64, "Supplier invoice"),
        ):
            session.add(
                DocumentRow(
                    id=document_id,
                    project_id="project-actuals",
                    logical_key=document_id,
                    title=title,
                    document_type="ACTUAL_EVIDENCE",
                    critical=True,
                    cancelled=False,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                DocumentRevisionRow(
                    id=revision_id,
                    document_id=document_id,
                    revision_label="1",
                    issue_date=date(2026, 7, 23),
                    object_hash=object_hash,
                    object_key=f"objects/{object_hash}",
                    original_filename=f"{document_id}.pdf",
                    media_type="application/pdf",
                    size_bytes=100,
                    supersedes_revision_id=None,
                    is_current=True,
                    corrupt=False,
                    protected=False,
                    inspection_payload={},
                    created_at=now,
                    updated_at=now,
                )
            )
        session.add(
            _observation(
                observation_id="observation-project-outcome",
                field_name="project_execution_status",
                value="COMPLETED",
                document_id="document-outcome",
                revision_id="revision-outcome",
                object_hash="a" * 64,
                actor_id="erp-importer",
                now=now,
            )
        )

        atomic_input = AtomicCostInput(
            cost_input_id="pipe-cost",
            line_id="boq-line-pipe",
            wbs_node_id="wbs-pipe",
            semantic_key="pipe",
            category=CostCategory.MATERIAL,
            quantity=Decimal("10"),
            unit="m",
            unit_rate=Decimal("100"),
            currency="RUB",
            approved_assumption_id="approved-pipe-price",
        )
        calculation_policy = CalculationPolicy(
            policy_version="calculation-policy-v1",
            currency="RUB",
            line_rounding_scale=2,
            total_rounding_scale=2,
            rounding_mode="ROUND_HALF_UP",
            independent_tolerance=Decimal("0"),
            expected_semantic_keys=frozenset({"pipe"}),
        )
        primary = calculate_primary(
            (atomic_input,),
            calculation_policy,
            engine_version="calculation-model-v1",
            calculated_at=now,
        )
        independent = validate_independently(
            (atomic_input,),
            primary,
            calculation_policy,
            validator_version="independent-calculation-v1",
            validated_at=now,
        )
        snapshot = create_snapshot(
            project_id="project-actuals",
            document_set_revision_id="document-set-forecast",
            inputs=(atomic_input,),
            policy=calculation_policy,
            controlled_versions=(),
            primary=primary,
            independent=independent,
            created_by="bid-estimator",
            created_at=now,
        )
        stored = store.put(
            BytesIO(
                canonical_json(
                    {
                        "snapshot": snapshot,
                        "inputs": (atomic_input,),
                        "policy": calculation_policy,
                        "controlled_versions": (),
                        "primary": primary,
                        "independent": independent,
                    }
                )
            )
        )
        session.add(
            CalculationRunRow(
                id="calculation-run-actuals",
                project_id="project-actuals",
                engine_version="calculation-model-v1",
                status="VALIDATED",
                currency="RUB",
                grand_total=primary.grand_total,
                payload={
                    "primary": primary.model_dump(mode="json"),
                    "independent_validation": independent.model_dump(mode="json"),
                    "policy": calculation_policy.model_dump(mode="json"),
                },
                created_at=now,
            )
        )
        session.add(
            CalculationSnapshotRow(
                id=snapshot.snapshot_id,
                project_id="project-actuals",
                calculation_run_id="calculation-run-actuals",
                document_set_revision_id=snapshot.document_set_revision_id,
                input_hash=snapshot.input_hash,
                output_hash=snapshot.output_hash,
                snapshot_hash=snapshot.snapshot_hash,
                fixed=True,
                object_key=stored.object_key,
                created_by=snapshot.created_by,
                created_at=now,
            )
        )
        session.add(
            CostInputRow(
                id="cost-input-pipe",
                project_id="project-actuals",
                calculation_run_id="calculation-run-actuals",
                semantic_key=atomic_input.semantic_key,
                category=atomic_input.category.value,
                amount_basis_id=atomic_input.approved_assumption_id,
                payload=atomic_input.model_dump(mode="json"),
                created_at=now,
            )
        )
        session.add(
            ReleaseDecisionRow(
                id="release-bid-actuals",
                project_id="project-actuals",
                snapshot_id=snapshot.snapshot_id,
                requested_state=ApprovalState.APPROVED_FOR_BID.value,
                resulting_state=ApprovalState.APPROVED_FOR_BID.value,
                allowed=True,
                payload={"allowed": True},
                decided_by="bid-approver",
                decided_at=now,
            )
        )
        session.add(
            _observation(
                observation_id="observation-actual-price",
                field_name="actual_unit_rate",
                value={
                    "actual_key": "pipe-price-2026-08",
                    "entity_type": "COST_INPUT",
                    "entity_id": "cost-input-pipe",
                    "metric": "unit_rate",
                    "value": "112",
                    "unit": "RUB/m",
                    "source_class": "SUPPLIER_INVOICE",
                    "occurred_on": "2026-08-01",
                },
                document_id="document-invoice",
                revision_id="revision-invoice",
                object_hash="b" * 64,
                actor_id="erp-importer",
                now=now,
            )
        )

    with factory.begin() as session:
        service = ActualsService(session=session, settings=settings, object_store=store)
        with pytest.raises(LookupError, match="project-missing"):
            service.require_verified_actual_integrity(
                project_id="project-missing",
                actual_id="actual-missing",
            )
        with pytest.raises(ValueError, match="limit must be between"):
            service.context(
                actor=recorder,
                project_id="project-actuals",
                selected_metric="unit_rate",
                limit=0,
            )
        context = service.context(
            actor=recorder,
            project_id="project-actuals",
            selected_metric="unit_rate",
            limit=20,
        )
        assert context.record_roles == (ActorRole.PROCUREMENT,)
        assert context.actual_review_role is ActorRole.AUDITOR
        assert context.variance_classifier_roles == (ActorRole.REVIEWER,)
        assert context.variance_review_role is ActorRole.TECHNICAL_EXPERT
        assert context.calibration_approval_role is ActorRole.METHODOLOGY_OWNER
        candidate = next(
            item
            for item in context.evidence_candidates
            if item.observation.observation_id == "observation-actual-price"
        )
        assert candidate.eligible and candidate.evidence_value is not None
        assert candidate.observation_created_at == now
        with pytest.raises(OptimisticLockError, match="evidence changed"):
            service.record_actual(
                actor=recorder,
                project_id="project-actuals",
                draft=ActualRecordDraft(
                    metric="unit_rate",
                    source_observation_id=candidate.observation.observation_id,
                    expected_observation_created_at=now + timedelta(seconds=1),
                ),
                actuals_policy_version_id=context.policy_version_id,
                request_id="request-record-stale-actual",
                reason="Reject a stale evidence selection",
            )
        recorded = service.record_actual(
            actor=recorder,
            project_id="project-actuals",
            draft=ActualRecordDraft(
                metric="unit_rate",
                source_observation_id=candidate.observation.observation_id,
                expected_observation_created_at=now,
            ),
            actuals_policy_version_id=context.policy_version_id,
            request_id="request-record-actual",
            reason="Record exact accepted supplier invoice rate",
        )
        assert recorded.actual.value == Decimal("112")
        recorded_row = session.get(ActualRecordRow, recorded.actual.actual_id)
        assert recorded_row is not None
        missing_actual_task_savepoint = session.begin_nested()
        missing_actual_task = session.get(
            ApprovalTaskRow,
            str(recorded_row.payload["approval_task_id"]),
        )
        assert missing_actual_task is not None
        session.delete(missing_actual_task)
        session.flush()
        missing_task_context = service.context(
            actor=actual_reviewer,
            project_id="project-actuals",
            selected_metric="unit_rate",
            limit=20,
        )
        missing_task_review = next(
            item
            for item in missing_task_context.records
            if item.record.actual.actual_id == recorded.actual.actual_id
        )
        assert missing_task_review.decision_blockers == ("TASK_MISSING",)
        with pytest.raises(ValueError, match="review task is missing"):
            service.decide_actual(
                actor=actual_reviewer,
                project_id="project-actuals",
                actual_id=recorded.actual.actual_id,
                command=ActualDecisionCommand(
                    decision=ApprovalDecision.APPROVED,
                    expected_actual_created_at=recorded.created_at,
                    expected_task_updated_at=recorded.task_updated_at,
                ),
                request_id="request-missing-actual-task",
                reason="Reject an actual without its dedicated task",
            )
        missing_actual_task_savepoint.rollback()
        with pytest.raises(OptimisticLockError, match="Actual fact changed"):
            service.decide_actual(
                actor=actual_reviewer,
                project_id="project-actuals",
                actual_id=recorded.actual.actual_id,
                command=ActualDecisionCommand(
                    decision=ApprovalDecision.APPROVED,
                    expected_actual_created_at=recorded.created_at + timedelta(seconds=1),
                    expected_task_updated_at=recorded.task_updated_at,
                ),
                request_id="request-stale-actual-row",
                reason="Reject stale actual review context",
            )
        with pytest.raises(OptimisticLockError, match="review task changed"):
            service.decide_actual(
                actor=actual_reviewer,
                project_id="project-actuals",
                actual_id=recorded.actual.actual_id,
                command=ActualDecisionCommand(
                    decision=ApprovalDecision.APPROVED,
                    expected_actual_created_at=recorded.created_at,
                    expected_task_updated_at=recorded.task_updated_at + timedelta(seconds=1),
                ),
                request_id="request-stale-actual-task",
                reason="Reject stale actual task context",
            )
        with pytest.raises(ValueError, match="FOUR_EYES"):
            service.decide_actual(
                actor=recorder,
                project_id="project-actuals",
                actual_id=recorded.actual.actual_id,
                command=ActualDecisionCommand(
                    decision=ApprovalDecision.APPROVED,
                    expected_actual_created_at=recorded.created_at,
                    expected_task_updated_at=recorded.task_updated_at,
                ),
                request_id="request-self-review-actual",
                reason="Invalid self review",
            )
        verified = service.decide_actual(
            actor=actual_reviewer,
            project_id="project-actuals",
            actual_id=recorded.actual.actual_id,
            command=ActualDecisionCommand(
                decision=ApprovalDecision.APPROVED,
                expected_actual_created_at=recorded.created_at,
                expected_task_updated_at=recorded.task_updated_at,
            ),
            request_id="request-verify-actual",
            reason="Reconcile invoice with accepted delivery and ledger",
        )
        assert verified.record.actual.verified
        with pytest.raises(ValueError, match="ACTUAL_NOT_IN_REVIEW"):
            service.decide_actual(
                actor=actual_reviewer,
                project_id="project-actuals",
                actual_id=recorded.actual.actual_id,
                command=ActualDecisionCommand(
                    decision=ApprovalDecision.APPROVED,
                    expected_actual_created_at=recorded.created_at,
                    expected_task_updated_at=verified.record.task_updated_at,
                ),
                request_id="request-repeat-actual-review",
                reason="Reject a repeated actual decision",
            )

        session.add(
            ReleaseDecisionRow(
                id="release-bid-actuals-repeat",
                project_id="project-actuals",
                snapshot_id=snapshot.snapshot_id,
                requested_state=ApprovalState.APPROVED_FOR_BID.value,
                resulting_state=ApprovalState.APPROVED_FOR_BID.value,
                allowed=True,
                payload={"source": "independent-repeat-release"},
                decided_by="bid-approver",
                decided_at=now + timedelta(seconds=1),
            )
        )
        session.flush()
        forecast_page = service.forecast_candidates(
            actor=classifier,
            project_id="project-actuals",
            actual_id=recorded.actual.actual_id,
            limit=1,
            cursor=None,
        )
        assert forecast_page.next_cursor is not None
        forecast = forecast_page.items[0]
        older_forecast_page = service.forecast_candidates(
            actor=classifier,
            project_id="project-actuals",
            actual_id=recorded.actual.actual_id,
            limit=1,
            cursor=forecast_page.next_cursor,
        )
        assert older_forecast_page.next_cursor is None
        assert older_forecast_page.items[0].forecast == forecast.forecast
        assert (
            older_forecast_page.items[0].released_by_decision_id != forecast.released_by_decision_id
        )
        with pytest.raises(ValueError, match="cursor"):
            service.forecast_candidates(
                actor=classifier,
                project_id="project-actuals",
                actual_id=recorded.actual.actual_id,
                limit=1,
                cursor="not-a-valid-cursor",
            )
        with pytest.raises(ValueError, match="cursor"):
            service.context(
                actor=classifier,
                project_id="project-actuals",
                selected_metric="unit_rate",
                limit=1,
                cursor=forecast_page.next_cursor,
            )
        compare_command = CompareActualCommand(
            forecast_id=forecast.forecast.forecast_id,
            released_by_decision_id=forecast.released_by_decision_id,
            reason=VarianceReason.PRICE_CHANGE,
            reason_detail="Supplier indexation accepted under the purchase order",
            expected_actual_created_at=recorded.created_at,
            actuals_policy_version_id=context.policy_version_id,
        )
        with pytest.raises(OptimisticLockError, match="candidate changed"):
            service.compare_to_forecast(
                actor=classifier,
                project_id="project-actuals",
                actual_id=recorded.actual.actual_id,
                command=compare_command.model_copy(
                    update={"forecast_id": "forecast-no-longer-current"}
                ),
                request_id="request-missing-forecast",
                reason="Reject a forecast outside the released snapshot",
            )
        with pytest.raises(OptimisticLockError, match="candidate changed"):
            service.compare_to_forecast(
                actor=classifier,
                project_id="project-actuals",
                actual_id=recorded.actual.actual_id,
                command=compare_command.model_copy(
                    update={"released_by_decision_id": "release-no-longer-current"}
                ),
                request_id="request-stale-release-decision",
                reason="Reject a replaced release-decision provenance link",
            )
        with pytest.raises(OptimisticLockError, match="forecast context"):
            service.compare_to_forecast(
                actor=classifier,
                project_id="project-actuals",
                actual_id=recorded.actual.actual_id,
                command=compare_command.model_copy(
                    update={
                        "expected_actual_created_at": (recorded.created_at + timedelta(seconds=1))
                    }
                ),
                request_id="request-stale-variance-context",
                reason="Reject stale forecast comparison context",
            )
        comparison = service.compare_to_forecast(
            actor=classifier,
            project_id="project-actuals",
            actual_id=recorded.actual.actual_id,
            command=compare_command,
            request_id="request-classify-variance",
            reason="Classify verified forecast-to-actual variance",
        )
        assert comparison.variance.absolute_variance == Decimal("12")
        classifier_context = service.context(
            actor=classifier,
            project_id="project-actuals",
            selected_metric="unit_rate",
            limit=20,
        )
        assert next(
            item
            for item in classifier_context.records
            if item.record.actual.actual_id == recorded.actual.actual_id
        ).has_classified_variance
        classifier_variance = next(
            item
            for item in classifier_context.variances
            if item.variance_record_id == comparison.variance_record_id
        )
        assert not classifier_variance.decision_allowed
        assert "FOUR_EYES_VARIANCE_CLASSIFIER" in classifier_variance.decision_blockers
        reviewer_context = service.context(
            actor=variance_reviewer,
            project_id="project-actuals",
            selected_metric="unit_rate",
            limit=20,
        )
        reviewable_variance = next(
            item
            for item in reviewer_context.variances
            if item.variance_record_id == comparison.variance_record_id
        )
        assert reviewable_variance.assigned_role is ActorRole.TECHNICAL_EXPERT
        assert reviewable_variance.decision_allowed
        assert reviewable_variance.decision_blockers == ()
        with pytest.raises(ValueError, match="already has a variance"):
            service.compare_to_forecast(
                actor=classifier,
                project_id="project-actuals",
                actual_id=recorded.actual.actual_id,
                command=compare_command,
                request_id="request-repeat-variance-classification",
                reason="Reject duplicate variance classification",
            )
        variance_row = session.get(
            VarianceRecordRow,
            comparison.variance_record_id,
        )
        assert variance_row is not None
        variance_task_at = approval_task_updated_at(
            session,
            str(variance_row.payload["approval_task_id"]),
        )
        variance_created_at = ensure_utc(variance_row.created_at)
        assert variance_created_at is not None
        with pytest.raises(LookupError, match="variance-missing"):
            service.decide_variance(
                actor=variance_reviewer,
                project_id="project-actuals",
                variance_id="variance-missing",
                command=VarianceDecisionCommand(
                    decision=ApprovalDecision.APPROVED,
                    expected_variance_created_at=variance_created_at,
                    expected_task_updated_at=variance_task_at,
                ),
                request_id="request-missing-variance",
                reason="Reject an unknown variance",
            )
        variance_payload = variance_row.payload
        variance_row.payload = {
            key: value for key, value in variance_payload.items() if key != "approval_task_id"
        }
        session.flush()
        with pytest.raises(ValueError, match="review task is missing"):
            service.decide_variance(
                actor=variance_reviewer,
                project_id="project-actuals",
                variance_id=variance_row.id,
                command=VarianceDecisionCommand(
                    decision=ApprovalDecision.APPROVED,
                    expected_variance_created_at=variance_created_at,
                    expected_task_updated_at=variance_task_at,
                ),
                request_id="request-missing-variance-task",
                reason="Reject a variance without its dedicated task",
            )
        variance_row.payload = variance_payload
        session.flush()
        with pytest.raises(OptimisticLockError, match="Variance changed"):
            service.decide_variance(
                actor=variance_reviewer,
                project_id="project-actuals",
                variance_id=variance_row.id,
                command=VarianceDecisionCommand(
                    decision=ApprovalDecision.APPROVED,
                    expected_variance_created_at=variance_created_at + timedelta(seconds=1),
                    expected_task_updated_at=variance_task_at,
                ),
                request_id="request-stale-variance-row",
                reason="Reject stale variance review context",
            )
        with pytest.raises(OptimisticLockError, match="Variance task changed"):
            service.decide_variance(
                actor=variance_reviewer,
                project_id="project-actuals",
                variance_id=variance_row.id,
                command=VarianceDecisionCommand(
                    decision=ApprovalDecision.APPROVED,
                    expected_variance_created_at=variance_created_at,
                    expected_task_updated_at=variance_task_at + timedelta(seconds=1),
                ),
                request_id="request-stale-variance-task",
                reason="Reject stale variance task context",
            )
        with pytest.raises(ValueError, match="FOUR_EYES"):
            service.decide_variance(
                actor=classifier,
                project_id="project-actuals",
                variance_id=variance_row.id,
                command=VarianceDecisionCommand(
                    decision=ApprovalDecision.APPROVED,
                    expected_variance_created_at=variance_created_at,
                    expected_task_updated_at=variance_task_at,
                ),
                request_id="request-self-review-variance",
                reason="Invalid self review",
            )
        variance_decision = service.decide_variance(
            actor=variance_reviewer,
            project_id="project-actuals",
            variance_id=variance_row.id,
            command=VarianceDecisionCommand(
                decision=ApprovalDecision.APPROVED,
                expected_variance_created_at=variance_created_at,
                expected_task_updated_at=variance_task_at,
            ),
            request_id="request-review-variance",
            reason="Independent cause and arithmetic review",
        )
        assert variance_decision.calibration_example is not None
        assert "VARIANCE_NOT_IN_REVIEW" in variance_decision.variance.decision_blockers
        assert "TASK_NOT_PENDING" in variance_decision.variance.decision_blockers
        assert "VARIANCE_INTEGRITY_FAILED" not in variance_decision.variance.decision_blockers
        with pytest.raises(ValueError, match="VARIANCE_NOT_IN_REVIEW"):
            service.decide_variance(
                actor=variance_reviewer,
                project_id="project-actuals",
                variance_id=variance_row.id,
                command=VarianceDecisionCommand(
                    decision=ApprovalDecision.APPROVED,
                    expected_variance_created_at=variance_created_at,
                    expected_task_updated_at=variance_decision.variance.task_updated_at,
                ),
                request_id="request-repeat-variance-review",
                reason="Reject a repeated variance decision",
            )
        calibration_row = session.get(
            CalibrationExampleRow,
            variance_decision.calibration_example.example_id,
        )
        assert calibration_row is not None and not calibration_row.approved
        calibration_creator_context = service.context(
            actor=variance_reviewer,
            project_id="project-actuals",
            selected_metric="unit_rate",
            limit=20,
        )
        creator_calibration = next(
            item
            for item in calibration_creator_context.calibration_examples
            if item.example.example_id == calibration_row.id
        )
        assert not creator_calibration.decision_allowed
        assert "CALIBRATION_FOUR_EYES_REQUIRED" in creator_calibration.decision_blockers
        owner_context = service.context(
            actor=calibration_owner,
            project_id="project-actuals",
            selected_metric="unit_rate",
            limit=20,
        )
        owner_calibration = next(
            item
            for item in owner_context.calibration_examples
            if item.example.example_id == calibration_row.id
        )
        assert owner_calibration.assigned_role is ActorRole.METHODOLOGY_OWNER
        assert owner_calibration.decision_allowed
        assert owner_calibration.decision_blockers == ()
        calibration_task_at = approval_task_updated_at(
            session,
            str(calibration_row.payload["approval_task_id"]),
        )
        calibration_created_at = ensure_utc(calibration_row.created_at)
        assert calibration_created_at is not None
        with pytest.raises(LookupError, match="calibration-missing"):
            service.decide_calibration_example(
                actor=calibration_owner,
                project_id="project-actuals",
                example_id="calibration-missing",
                command=CalibrationDecisionCommand(
                    decision=ApprovalDecision.APPROVED,
                    expected_example_created_at=calibration_created_at,
                    expected_task_updated_at=calibration_task_at,
                ),
                request_id="request-missing-calibration",
                reason="Reject an unknown calibration example",
            )
        calibration_payload = calibration_row.payload
        calibration_row.payload = {
            key: value for key, value in calibration_payload.items() if key != "approval_task_id"
        }
        session.flush()
        with pytest.raises(ValueError, match="review task is missing"):
            service.decide_calibration_example(
                actor=calibration_owner,
                project_id="project-actuals",
                example_id=calibration_row.id,
                command=CalibrationDecisionCommand(
                    decision=ApprovalDecision.APPROVED,
                    expected_example_created_at=calibration_created_at,
                    expected_task_updated_at=calibration_task_at,
                ),
                request_id="request-missing-calibration-task",
                reason="Reject a calibration example without its dedicated task",
            )
        calibration_row.payload = calibration_payload
        session.flush()
        calibration_row.payload = {
            **calibration_payload,
            "released_by_decision_id": "release-unrelated",
        }
        session.flush()
        with pytest.raises(ValueError, match="CALIBRATION_INTEGRITY_FAILED"):
            service.decide_calibration_example(
                actor=calibration_owner,
                project_id="project-actuals",
                example_id=calibration_row.id,
                command=CalibrationDecisionCommand(
                    decision=ApprovalDecision.APPROVED,
                    expected_example_created_at=calibration_created_at,
                    expected_task_updated_at=calibration_task_at,
                ),
                request_id="request-calibration-release-mismatch",
                reason="Reject a calibration label with mismatched release provenance",
            )
        calibration_row.payload = calibration_payload
        session.flush()
        with pytest.raises(OptimisticLockError, match="Calibration example changed"):
            service.decide_calibration_example(
                actor=calibration_owner,
                project_id="project-actuals",
                example_id=calibration_row.id,
                command=CalibrationDecisionCommand(
                    decision=ApprovalDecision.APPROVED,
                    expected_example_created_at=calibration_created_at + timedelta(seconds=1),
                    expected_task_updated_at=calibration_task_at,
                ),
                request_id="request-stale-calibration-row",
                reason="Reject stale calibration context",
            )
        with pytest.raises(OptimisticLockError, match="review task changed"):
            service.decide_calibration_example(
                actor=calibration_owner,
                project_id="project-actuals",
                example_id=calibration_row.id,
                command=CalibrationDecisionCommand(
                    decision=ApprovalDecision.APPROVED,
                    expected_example_created_at=calibration_created_at,
                    expected_task_updated_at=calibration_task_at + timedelta(seconds=1),
                ),
                request_id="request-stale-calibration-task",
                reason="Reject stale calibration task context",
            )
        with pytest.raises(ValueError, match="CALIBRATION_FOUR_EYES_REQUIRED"):
            service.decide_calibration_example(
                actor=variance_reviewer,
                project_id="project-actuals",
                example_id=calibration_row.id,
                command=CalibrationDecisionCommand(
                    decision=ApprovalDecision.APPROVED,
                    expected_example_created_at=calibration_created_at,
                    expected_task_updated_at=calibration_task_at,
                ),
                request_id="request-self-review-calibration",
                reason="Reject calibration approval by its variance reviewer",
            )
        approved_example = service.approve_calibration_example(
            actor=calibration_owner,
            project_id="project-actuals",
            example_id=calibration_row.id,
            expected_example_created_at=calibration_created_at,
            expected_task_updated_at=calibration_task_at,
            request_id="request-approve-calibration",
            reason="Methodology owner accepts the independently reviewed fact",
        )
        session.flush()
        session.refresh(calibration_row)
        assert calibration_row.approved
        assert approved_example.target_value == Decimal("112")
        calibration_created_event = session.scalar(
            select(AuditEventRow).where(
                AuditEventRow.aggregate_id == "project-actuals",
                AuditEventRow.event_type == "calibration_example_created",
            )
        )
        assert calibration_created_event is not None
        assert calibration_created_event.actor_id == variance_reviewer.actor_id
        assert calibration_created_event.payload["calibration_example_id"] == calibration_row.id
        assert (
            calibration_created_event.payload["approval_task_id"]
            == calibration_row.payload["approval_task_id"]
        )
        assert (
            calibration_created_event.payload["released_by_decision_id"]
            == forecast.released_by_decision_id
        )
        approved_task_at = approval_task_updated_at(
            session,
            str(calibration_row.payload["approval_task_id"]),
        )
        with pytest.raises(ValueError, match="CALIBRATION_NOT_IN_REVIEW"):
            service.decide_calibration_example(
                actor=calibration_owner,
                project_id="project-actuals",
                example_id=calibration_row.id,
                command=CalibrationDecisionCommand(
                    decision=ApprovalDecision.APPROVED,
                    expected_example_created_at=calibration_created_at,
                    expected_task_updated_at=approved_task_at,
                ),
                request_id="request-repeat-calibration-review",
                reason="Reject a repeated calibration decision",
            )

        replacement_observation = _observation(
            observation_id="observation-actual-price-replacement",
            field_name="actual_unit_rate",
            value={
                "actual_key": "pipe-price-2026-08",
                "entity_type": "COST_INPUT",
                "entity_id": "cost-input-pipe",
                "metric": "unit_rate",
                "value": "115",
                "unit": "RUB/m",
                "source_class": "SUPPLIER_INVOICE",
                "occurred_on": "2026-08-02",
            },
            document_id="document-invoice",
            revision_id="revision-invoice",
            object_hash="b" * 64,
            actor_id="erp-importer",
            now=now,
        )
        session.add(replacement_observation)
        session.flush()
        replacement = service.record_actual(
            actor=recorder,
            project_id="project-actuals",
            draft=ActualRecordDraft(
                metric="unit_rate",
                source_observation_id=replacement_observation.id,
                expected_observation_created_at=now,
            ),
            actuals_policy_version_id=context.policy_version_id,
            request_id="request-supersede-actual",
            reason="Replace the actual with a later controlled invoice",
        )
        assert replacement.supersedes_actual_id == recorded.actual.actual_id
        assert replacement.actual.value == Decimal("115")

        invalid_value = _observation(
            observation_id="observation-actual-invalid-value",
            field_name="actual_unit_rate",
            value={"unexpected": "payload"},
            document_id="document-invoice",
            revision_id="revision-invoice",
            object_hash="b" * 64,
            actor_id="erp-importer",
            now=now + timedelta(seconds=2),
        )
        inconsistent_row = _observation(
            observation_id="observation-actual-inconsistent-row",
            field_name="actual_unit_rate",
            value={
                "actual_key": "pipe-price-inconsistent",
                "entity_type": "COST_INPUT",
                "entity_id": "cost-input-pipe",
                "metric": "unit_rate",
                "value": "117",
                "unit": "RUB/m",
                "source_class": "SUPPLIER_INVOICE",
                "occurred_on": "2026-08-03",
            },
            document_id="document-invoice",
            revision_id="revision-invoice",
            object_hash="b" * 64,
            actor_id="erp-importer",
            now=now + timedelta(seconds=3),
        )
        inconsistent_row.method = EvidenceMethod.MANUAL.value
        unverified_candidate = _observation(
            observation_id="observation-actual-unverified",
            field_name="actual_unit_rate",
            value={
                "actual_key": "pipe-price-unverified",
                "entity_type": "COST_INPUT",
                "entity_id": "cost-input-pipe",
                "metric": "unit_rate",
                "value": "118",
                "unit": "RUB/m",
                "source_class": "SUPPLIER_INVOICE",
                "occurred_on": "2026-08-04",
            },
            document_id="document-invoice",
            revision_id="revision-invoice",
            object_hash="b" * 64,
            actor_id="erp-importer",
            now=now + timedelta(seconds=4),
        )
        unverified_candidate.status = VerificationStatus.UNVERIFIED.value
        unverified_observation = Observation.model_validate(
            unverified_candidate.payload["observation"]
        ).model_copy(update={"status": VerificationStatus.UNVERIFIED})
        unverified_candidate.payload = {
            "observation": unverified_observation.model_dump(mode="json")
        }
        session.add_all((invalid_value, inconsistent_row, unverified_candidate))
        replacement_row = session.get(ActualRecordRow, replacement.actual.actual_id)
        assert replacement_row is not None
        pagination_actual = ActualRecordRow(
            id="actual-pagination-second",
            project_id=replacement_row.project_id,
            actual_key="zz-pagination-second",
            entity_type=replacement_row.entity_type,
            entity_id=replacement_row.entity_id,
            metric=replacement_row.metric,
            value=replacement_row.value,
            unit=replacement_row.unit,
            verified=False,
            source_observation_id=replacement_row.source_observation_id,
            occurred_on=replacement_row.occurred_on,
            payload={
                **replacement_row.payload,
                "evidence_value": {
                    **dict(replacement_row.payload["evidence_value"]),
                    "actual_key": "zz-pagination-second",
                },
                "approval_task_id": "missing-pagination-task",
            },
            supersedes_actual_id=None,
            is_current=True,
            created_at=now + timedelta(seconds=5),
        )
        session.add(pagination_actual)
        session.flush()
        first_page = service.context(
            actor=recorder,
            project_id="project-actuals",
            selected_metric="unit_rate",
            limit=1,
        )
        assert first_page.next_cursor is not None
        second_page = service.context(
            actor=recorder,
            project_id="project-actuals",
            selected_metric="unit_rate",
            limit=1,
            cursor=first_page.next_cursor,
        )
        assert second_page.next_cursor is None
        assert {
            *(item.record.actual.actual_id for item in first_page.records),
            *(item.record.actual.actual_id for item in second_page.records),
        } == {replacement_row.id, pagination_actual.id}
        with pytest.raises(ValueError, match="cursor"):
            service.context(
                actor=recorder,
                project_id="project-actuals",
                selected_metric="unit_rate",
                limit=1,
                cursor="not-a-valid-cursor",
            )
        replacement_row.verified = True
        session.flush()
        conflict_savepoint = session.begin_nested()
        session.add(
            ConflictRow(
                id="conflict-actual-unit-rate-late",
                project_id="project-actuals",
                field_name="actual_unit_rate",
                status="UNRESOLVED",
                payload={"reason": "Late invoice discrepancy"},
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        damaged_context = service.context(
            actor=recorder,
            project_id="project-actuals",
            selected_metric="unit_rate",
            limit=3,
        )
        by_id = {
            item.observation.observation_id: item for item in damaged_context.evidence_candidates
        }
        assert replacement.actual.source_observation_id in by_id
        assert "ACTUAL_VALUE_INVALID" in by_id[invalid_value.id].blockers
        assert "EVIDENCE_INTEGRITY_FAILED" in by_id[inconsistent_row.id].blockers
        assert "SOURCE_LINEAGE_NOT_QUALIFIED" in by_id[inconsistent_row.id].blockers
        assert "VERIFIED_EVIDENCE_REQUIRED" in by_id[unverified_candidate.id].blockers
        assert "UNRESOLVED_EVIDENCE_CONFLICT" in by_id[unverified_candidate.id].blockers
        with pytest.raises(ValueError, match="Verified actual"):
            service.forecast_candidates(
                actor=classifier,
                project_id="project-actuals",
                actual_id=replacement.actual.actual_id,
                limit=10,
                cursor=None,
            )
        conflict_savepoint.rollback()
        replacement_row.verified = False

    engine.dispose()
