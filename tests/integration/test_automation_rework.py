from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from tenderguard.api.main import create_app
from tenderguard.application.automation_rework import (
    AUTOMATION_REWORK_CAPABILITY,
    AutomationDispatchDisposition,
    AutomationReworkDispatcher,
    AutomationReworkStatusService,
)
from tenderguard.config import Settings
from tenderguard.domain.access import project_role_mask
from tenderguard.domain.common import content_hash, utc_now
from tenderguard.domain.enums import (
    ActorRole,
    ApprovalState,
    ProjectAccessLevel,
    ProjectMembershipStatus,
)
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.database import (
    create_database_engine,
    create_schema_for_tests,
    create_session_factory,
)
from tenderguard.infrastructure.object_store import LocalObjectStore
from tenderguard.infrastructure.orm import (
    AdapterQualificationRow,
    AutomationReworkDispatchRow,
    CalculationRunRow,
    CalculationSnapshotRow,
    ExpertReworkRequestRow,
    OutboxEventRow,
    ProjectMembershipRow,
    ProjectRow,
    VerificationFindingRow,
)

NOW = datetime(2026, 8, 3, 10, 0, tzinfo=UTC)


def _settings(tmp_path: Path, *, worker_actor_id: str = "automation-worker") -> Settings:
    return Settings(
        app_env="test",
        database_url="sqlite+pysqlite://",
        local_object_store_path=tmp_path / "objects",
        allow_insecure_dev_auth=True,
        audit_signing_key="automation-rework-test-key-at-least-32-bytes",
        automation_rework_adapter="qualified-automation-dispatcher",
        automation_rework_qualification_id="qualification-automation",
        automation_rework_worker_actor_id=worker_actor_id,
        automation_job_lease_seconds=30,
        automation_job_timeout_seconds=20,
        automation_job_max_attempts=3,
        automation_job_retry_base_seconds=1,
        automation_job_retry_max_seconds=2,
    )


def _seed(
    session: object,
    *,
    target_stage: ApprovalState = ApprovalState.PRICING_IN_PROGRESS,
    supported_rework_stages: tuple[ApprovalState, ...] = (ApprovalState.PRICING_IN_PROGRESS,),
    source_payload_update: dict[str, object] | None = None,
) -> None:
    request_id = "expert-rework-1"
    base_payload: dict[str, object] = {
        "project_id": "project-1",
        "snapshot_id": "snapshot-1",
        "document_set_revision_id": "docs-1",
        "project_row_version": 1,
        "gate_target": "bid",
        "gate_hash": "d" * 64,
        "requested_state": ApprovalState.APPROVED_FOR_BID.value,
        "target_stage": target_stage.value,
        "reason": "Repeat the selected evidence processing automatically.",
        "issues": [
            {
                "kind": "BOQ_PRICE_ROW",
                "reference_id": "boq-line-1:1",
                "code": "EXPERT_RECHECK_REQUESTED",
                "comment": "Refresh all available sources and recalculate.",
                "boq_line_id": "boq-line-1",
                "item_id": "item-1",
                "boq_item_name": "Power cable",
                "current_row_status": "VERIFIED",
                "current_blockers": [],
            }
        ],
    }
    request_hash = content_hash(base_payload)
    event_payload: dict[str, object] = {
        "project_id": "project-1",
        "rework_request_id": request_id,
        "snapshot_id": "snapshot-1",
        "target_stage": target_stage.value,
        "request_hash": request_hash,
    }
    if source_payload_update:
        event_payload.update(source_payload_update)
    session.add_all(  # type: ignore[attr-defined]
        [
            ProjectRow(
                id="project-1",
                organization_id="org-1",
                code="AUTO-1",
                name="Automatic final rework",
                state=target_stage.value,
                blocked_resume_state=(
                    ApprovalState.EXPERT_REVIEW.value
                    if target_stage is ApprovalState.BLOCKED
                    else None
                ),
                current_document_set_revision_id="docs-1",
                row_version=2,
                created_at=NOW,
                updated_at=NOW,
            ),
            ProjectMembershipRow(
                id="membership-reviewer",
                project_id="project-1",
                principal_id="reviewer-1",
                roles=[ActorRole.REVIEWER.value],
                role_mask=project_role_mask((ActorRole.REVIEWER,)),
                access_level=ProjectAccessLevel.OWNER.value,
                status=ProjectMembershipStatus.ACTIVE.value,
                version=1,
                supersedes_membership_id=None,
                changed_by="fixture",
                reason="Read automation status",
                created_at=NOW,
            ),
            AdapterQualificationRow(
                id="qualification-automation",
                adapter_name="qualified-automation-dispatcher",
                adapter_version="1",
                status="APPROVED",
                valid_until=date(2027, 8, 3),
                test_evidence_hash="a" * 64,
                payload={
                    "organization_id": "org-1",
                    "service_actor_id": "automation-worker",
                    "supported_methods": [AUTOMATION_REWORK_CAPABILITY],
                    "supported_rework_stages": [stage.value for stage in supported_rework_stages],
                },
                approved_by="qualification-reviewer",
                approved_at=NOW,
            ),
            CalculationRunRow(
                id="calculation-1",
                project_id="project-1",
                engine_version="engine-1",
                status="VALIDATED",
                currency="RUB",
                grand_total=Decimal("100"),
                payload={},
                created_at=NOW,
            ),
            CalculationSnapshotRow(
                id="snapshot-1",
                project_id="project-1",
                calculation_run_id="calculation-1",
                document_set_revision_id="docs-1",
                input_hash="b" * 64,
                output_hash="c" * 64,
                snapshot_hash="e" * 64,
                fixed=True,
                object_key="snapshots/automation.json",
                created_by="calculator",
                created_at=NOW,
            ),
            ExpertReworkRequestRow(
                id=request_id,
                project_id="project-1",
                snapshot_id="snapshot-1",
                requested_state=ApprovalState.APPROVED_FOR_BID.value,
                gate_hash="d" * 64,
                target_stage=target_stage.value,
                payload={**base_payload, "request_hash": request_hash},
                requested_by="reviewer-1",
                requested_at=NOW,
            ),
            OutboxEventRow(
                id="outbox-final-rework-1",
                deduplication_key="final-rework:1",
                delivery_deduplication_key="final-rework:1",
                topic="project.final-review.rework-requested",
                aggregate_id=request_id,
                payload=event_payload,
                attempts=0,
                available_at=NOW,
                created_at=NOW,
            ),
        ]
    )


def _dispatcher(tmp_path: Path, settings: Settings, factory: object) -> AutomationReworkDispatcher:
    return AutomationReworkDispatcher(
        session_factory=factory,  # type: ignore[arg-type]
        settings=settings,
        object_store=LocalObjectStore(tmp_path / "objects"),
    )


def test_dispatcher_queues_exactly_one_stage_command_and_exposes_status(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    factory = create_session_factory(engine)
    with factory.begin() as session:
        _seed(session)

    dispatcher = _dispatcher(tmp_path, settings, factory)
    result = dispatcher.dispatch_next(worker_id="automation-instance-1")
    assert result.disposition is AutomationDispatchDisposition.STAGE_COMMAND_QUEUED
    assert result.dispatch is not None
    assert result.dispatch.command_topic == "project.automation.pricing.requested"

    with factory() as session:
        source = session.get(OutboxEventRow, "outbox-final-rework-1")
        assert source is not None and source.published_at is not None
        dispatch = session.scalar(select(AutomationReworkDispatchRow))
        assert dispatch is not None
        command = session.get(OutboxEventRow, dispatch.command_outbox_event_id)
        assert command is not None
        assert command.published_at is None
        assert command.payload["request_hash"] == dispatch.request_hash
        assert command.payload["issue_references"] == [
            {
                "kind": "BOQ_PRICE_ROW",
                "reference_id": "boq-line-1:1",
                "code": "EXPERT_RECHECK_REQUESTED",
            }
        ]
        assert "comment" not in command.payload["issue_references"][0]
        page = AutomationReworkStatusService(
            session=session,
            settings=settings,
            object_store=LocalObjectStore(tmp_path / "objects"),
        ).list_status(
            actor=Actor("reviewer-1", "org-1", frozenset({ActorRole.REVIEWER})),
            project_id="project-1",
            limit=20,
        )
        assert page.items[0].status == "STAGE_COMMAND_QUEUED"
        assert page.items[0].command_delivery_status == "PENDING"

    assert (
        dispatcher.dispatch_next(worker_id="automation-instance-1").disposition
        is AutomationDispatchDisposition.IDLE
    )


def test_dispatch_retry_reuses_the_immutable_dispatch_without_duplicate_command(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    factory = create_session_factory(engine)
    with factory.begin() as session:
        _seed(session)
    dispatcher = _dispatcher(tmp_path, settings, factory)
    actor, qualification_id, qualified_stages = dispatcher._qualified_worker()
    claim = dispatcher._claim(
        worker_id="automation-instance-crashed",
        rework_request_id="expert-rework-1",
    )
    assert claim is not None
    first = dispatcher._process_claim(
        claim=claim,
        actor=actor,
        qualification_id=qualification_id,
        qualified_stages=qualified_stages,
    )

    with factory.begin() as session:
        source = session.get(OutboxEventRow, claim.event_id)
        assert source is not None
        source.lease_expires_at = utc_now() - timedelta(seconds=1)

    retried = dispatcher.dispatch_next(worker_id="automation-instance-retry")
    assert retried.disposition is AutomationDispatchDisposition.STAGE_COMMAND_QUEUED
    assert retried.dispatch is not None
    assert retried.dispatch.dispatch_hash == first.dispatch_hash
    with factory() as session:
        assert session.scalar(select(func.count(AutomationReworkDispatchRow.id))) == 1
        assert (
            session.scalar(
                select(func.count(OutboxEventRow.id)).where(
                    OutboxEventRow.topic == "project.automation.pricing.requested"
                )
            )
            == 1
        )


def test_tampered_source_event_is_dead_lettered_without_stage_command(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    factory = create_session_factory(engine)
    with factory.begin() as session:
        _seed(session, source_payload_update={"request_hash": "f" * 64})

    result = _dispatcher(tmp_path, settings, factory).dispatch_next(
        worker_id="automation-instance-1"
    )
    assert result.disposition is AutomationDispatchDisposition.DEAD_LETTERED
    assert result.error_code == "AUTOMATION_REWORK_INTEGRITY_FAILED"
    with factory() as session:
        source = session.get(OutboxEventRow, "outbox-final-rework-1")
        assert source is not None and source.dead_lettered_at is not None
        assert session.scalars(select(AutomationReworkDispatchRow)).all() == []


def test_stale_project_context_is_dead_lettered_without_stage_command(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    factory = create_session_factory(engine)
    with factory.begin() as session:
        _seed(session)
        session.flush()
        project = session.get(ProjectRow, "project-1")
        assert project is not None
        project.current_document_set_revision_id = "docs-newer"

    result = _dispatcher(tmp_path, settings, factory).dispatch_next(
        worker_id="automation-instance-1"
    )
    assert result.disposition is AutomationDispatchDisposition.DEAD_LETTERED
    assert result.error_code == "AUTOMATION_REWORK_INTEGRITY_FAILED"
    with factory() as session:
        assert session.scalars(select(AutomationReworkDispatchRow)).all() == []
        assert (
            session.scalar(
                select(func.count(OutboxEventRow.id)).where(
                    OutboxEventRow.topic == "project.automation.pricing.requested"
                )
            )
            == 0
        )


def test_unautomatable_request_is_recorded_blocked_without_command(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    factory = create_session_factory(engine)
    with factory.begin() as session:
        _seed(session, target_stage=ApprovalState.BLOCKED)

    result = _dispatcher(tmp_path, settings, factory).dispatch_next(
        worker_id="automation-instance-1"
    )
    assert result.disposition is AutomationDispatchDisposition.BLOCKED
    assert result.dispatch is not None
    assert result.dispatch.command_outbox_event_id is None
    with factory() as session:
        finding = session.scalar(
            select(VerificationFindingRow).where(
                VerificationFindingRow.code == "AUTOMATION_REWORK_NOT_AUTOMATABLE"
            )
        )
        assert finding is not None and not finding.resolved


def test_unqualified_stage_is_recorded_blocked_without_command(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    factory = create_session_factory(engine)
    with factory.begin() as session:
        _seed(
            session,
            target_stage=ApprovalState.CALCULATION_IN_PROGRESS,
            supported_rework_stages=(ApprovalState.PRICING_IN_PROGRESS,),
        )

    result = _dispatcher(tmp_path, settings, factory).dispatch_next(
        worker_id="automation-instance-1"
    )
    assert result.disposition is AutomationDispatchDisposition.BLOCKED
    assert result.dispatch is not None
    assert result.dispatch.command_outbox_event_id is None
    with factory() as session:
        finding = session.scalar(
            select(VerificationFindingRow).where(
                VerificationFindingRow.code == "AUTOMATION_REWORK_STAGE_NOT_QUALIFIED"
            )
        )
        assert finding is not None and not finding.resolved


def test_invalid_worker_binding_does_not_claim_pending_work(tmp_path: Path) -> None:
    settings = _settings(tmp_path, worker_actor_id="wrong-worker")
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    factory = create_session_factory(engine)
    with factory.begin() as session:
        _seed(session)

    with pytest.raises(ValueError, match="qualification is invalid"):
        _dispatcher(tmp_path, settings, factory).dispatch_next(worker_id="automation-instance-1")
    with factory() as session:
        source = session.get(OutboxEventRow, "outbox-final-rework-1")
        assert source is not None
        assert source.attempts == 0
        assert source.locked_by is None


def test_status_api_lists_pending_undispatched_rework(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    factory = create_session_factory(engine)
    with factory.begin() as session:
        _seed(session)
    app = create_app(
        settings,
        engine=engine,
        object_store=LocalObjectStore(tmp_path / "objects"),
    )
    headers = {
        "X-Dev-Actor": "reviewer-1",
        "X-Dev-Organization": "org-1",
        "X-Dev-Roles": "REVIEWER",
    }
    with TestClient(app) as client:
        response = client.get(
            "/v1/projects/project-1/final-review/rework-status",
            headers=headers,
        )
    assert response.status_code == 200
    assert response.json()["items"][0]["status"] == "PENDING_DISPATCH"


def test_status_api_fails_closed_when_stored_dispatch_is_tampered(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    factory = create_session_factory(engine)
    with factory.begin() as session:
        _seed(session)
    dispatcher = _dispatcher(tmp_path, settings, factory)
    assert (
        dispatcher.dispatch_next(worker_id="automation-instance-1").disposition
        is AutomationDispatchDisposition.STAGE_COMMAND_QUEUED
    )
    with factory.begin() as session:
        dispatch = session.scalar(select(AutomationReworkDispatchRow))
        assert dispatch is not None
        dispatch.worker_actor_id = "tampered-worker"

    app = create_app(
        settings,
        engine=engine,
        object_store=LocalObjectStore(tmp_path / "objects"),
    )
    headers = {
        "X-Dev-Actor": "reviewer-1",
        "X-Dev-Organization": "org-1",
        "X-Dev-Roles": "REVIEWER",
    }
    with TestClient(app) as client:
        response = client.get(
            "/v1/projects/project-1/final-review/rework-status",
            headers=headers,
        )
    item = response.json()["items"][0]
    assert response.status_code == 200
    assert item["status"] == "BLOCKED"
    assert item["command_delivery_status"] == "INTEGRITY_FAILED"
    assert item["integrity_error_code"] == "AUTOMATION_REWORK_INTEGRITY_FAILED"
