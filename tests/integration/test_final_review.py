from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from tenderguard.api.main import create_app
from tenderguard.application.final_review import (
    ExpertReworkCommand,
    ExpertReworkIssue,
    FinalReviewService,
)
from tenderguard.application.projects import ProjectService
from tenderguard.config import Settings
from tenderguard.domain.access import project_role_mask
from tenderguard.domain.enums import (
    ActorRole,
    ApprovalState,
    ProjectAccessLevel,
    ProjectMembershipStatus,
)
from tenderguard.domain.models import CalculationSnapshot
from tenderguard.domain.release import ReleaseContext
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.database import (
    create_database_engine,
    create_schema_for_tests,
    create_session_factory,
)
from tenderguard.infrastructure.object_store import LocalObjectStore
from tenderguard.infrastructure.orm import (
    BoqLineRow,
    CalculationRunRow,
    CalculationSnapshotRow,
    ExpertReworkRequestRow,
    OutboxEventRow,
    ProjectMembershipRow,
    ProjectRow,
    WorkflowTransitionRow,
)

NOW = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        app_env="test",
        database_url="sqlite+pysqlite://",
        local_object_store_path=tmp_path / "objects",
        audit_signing_key="test-audit-signing-key-at-least-32-bytes",
    )


def _context(*, finding_document: bool = False) -> ReleaseContext:
    return ReleaseContext(
        current_document_set_confirmed=True,
        current_document_set_revision_id="docs-1",
        missing_critical_document_ids=("document-1",) if finding_document else (),
        project_cost_total=Decimal("100"),
        unverified_cost_total=Decimal("0"),
        max_unverified_cost_share=Decimal("0"),
        snapshot=CalculationSnapshot(
            snapshot_id="snapshot-1",
            project_id="project-1",
            document_set_revision_id="docs-1",
            input_hash="a" * 64,
            output_hash="b" * 64,
            snapshot_hash="c" * 64,
            created_by="system-calculator",
            created_at=NOW,
            fixed=True,
        ),
        snapshot_integrity_valid=True,
        snapshot_controlled_versions_match=True,
    )


def _seed(session: Session, *, reviewer_id: str = "expert-1") -> None:
    session.add_all(
        [
            ProjectRow(
                id="project-1",
                organization_id="org-1",
                code="TG-1",
                name="Final expert review",
                state=ApprovalState.EXPERT_REVIEW.value,
                current_document_set_revision_id="docs-1",
                row_version=1,
                created_at=NOW,
                updated_at=NOW,
            ),
            ProjectMembershipRow(
                id="membership-expert-1",
                project_id="project-1",
                principal_id=reviewer_id,
                roles=[ActorRole.REVIEWER.value],
                role_mask=project_role_mask((ActorRole.REVIEWER,)),
                access_level=ProjectAccessLevel.OWNER.value,
                status=ProjectMembershipStatus.ACTIVE.value,
                version=1,
                supersedes_membership_id=None,
                changed_by="fixture",
                reason="Final expert authority",
                created_at=NOW,
            ),
            CalculationRunRow(
                id="calculation-1",
                project_id="project-1",
                engine_version="deterministic-test-v1",
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
                input_hash="a" * 64,
                output_hash="b" * 64,
                snapshot_hash="c" * 64,
                fixed=True,
                object_key="snapshots/snapshot-1.json",
                created_by="system-calculator",
                created_at=NOW,
            ),
            BoqLineRow(
                id="boq-line-1",
                project_id="project-1",
                line_key="1",
                wbs_node_id="wbs-1",
                work_code="WORK-1",
                description="Cable supply and installation",
                unit="m",
                status="VERIFIED",
                supersedes_line_id=None,
                is_current=True,
                payload={
                    "cost_components": [
                        {
                            "semantic_key": "item-1",
                            "category": "MATERIAL",
                            "basis_kind": "MARKET",
                        }
                    ]
                },
                created_at=NOW,
                updated_at=NOW,
            ),
        ]
    )


def _service(session: Session, settings: Settings, tmp_path: Path) -> FinalReviewService:
    return FinalReviewService(
        session=session,
        settings=settings,
        object_store=LocalObjectStore(tmp_path / "objects"),
    )


def test_expert_can_return_an_exact_price_row_to_automatic_rework(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    factory = create_session_factory(engine)
    actor = Actor("expert-1", "org-1", frozenset({ActorRole.REVIEWER}))
    context = _context()
    monkeypatch.setattr(ProjectService, "_build_release_context", lambda *_: context)

    with factory.begin() as session:
        _seed(session)
        session.flush()
        project_service = ProjectService(
            session=session,
            settings=settings,
            object_store=LocalObjectStore(tmp_path / "objects"),
        )
        _, _, gate_hash, _, _ = project_service.evaluate_release_gates(
            actor=actor,
            project_id="project-1",
        )
        result = _service(session, settings, tmp_path).request_rework(
            actor=actor,
            project_id="project-1",
            command=ExpertReworkCommand(
                expected_project_row_version=1,
                gate_target="bid",
                gate_hash=gate_hash,
                issues=(
                    ExpertReworkIssue(
                        kind="BOQ_PRICE_ROW",
                        reference_id="boq-line-1:1",
                        code="EXPERT_RECHECK_REQUESTED",
                        comment="Recheck the supplier equivalence and delivery basis.",
                    ),
                ),
                reason="Return the selected row to automatic price collection and calculation.",
            ),
            request_id="request-final-rework-1",
        )

        assert result.target_stage is ApprovalState.PRICING_IN_PROGRESS
        assert result.project.state is ApprovalState.PRICING_IN_PROGRESS
        stored = session.scalar(select(ExpertReworkRequestRow))
        assert stored is not None
        assert stored.snapshot_id == "snapshot-1"
        assert stored.gate_hash == gate_hash
        assert stored.payload["issues"][0]["boq_item_name"] == ("Cable supply and installation")
        session.flush()
        outbox = session.scalar(
            select(OutboxEventRow).where(
                OutboxEventRow.topic == "project.final-review.rework-requested"
            )
        )
        assert outbox is not None
        transition = session.scalar(
            select(WorkflowTransitionRow).where(WorkflowTransitionRow.project_id == "project-1")
        )
        assert transition is not None
        assert transition.from_state == ApprovalState.EXPERT_REVIEW.value
        assert transition.to_state == ApprovalState.PRICING_IN_PROGRESS.value


def test_rework_rejects_stale_gate_unknown_row_and_non_expert(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    factory = create_session_factory(engine)
    actor = Actor("expert-1", "org-1", frozenset({ActorRole.REVIEWER}))
    context = _context()
    monkeypatch.setattr(ProjectService, "_build_release_context", lambda *_: context)

    with factory.begin() as session:
        _seed(session)
        session.flush()
        service = _service(session, settings, tmp_path)
        base = {
            "expected_project_row_version": 1,
            "gate_target": "bid",
            "issues": (
                ExpertReworkIssue(
                    kind="BOQ_PRICE_ROW",
                    reference_id="boq-line-absent:1",
                    code="EXPERT_RECHECK_REQUESTED",
                    comment="This row does not exist in the current matrix.",
                ),
            ),
            "reason": "Reject a final review decision that is not bound to current evidence.",
        }
        with pytest.raises(ValueError, match="gate changed"):
            service.request_rework(
                actor=actor,
                project_id="project-1",
                command=ExpertReworkCommand(gate_hash="f" * 64, **base),
                request_id="request-stale",
            )
        _, _, gate_hash, _, _ = ProjectService(
            session=session,
            settings=settings,
            object_store=LocalObjectStore(tmp_path / "objects"),
        ).evaluate_release_gates(actor=actor, project_id="project-1")
        with pytest.raises(ValueError, match="absent from the current matrix"):
            service.request_rework(
                actor=actor,
                project_id="project-1",
                command=ExpertReworkCommand(gate_hash=gate_hash, **base),
                request_id="request-unknown-row",
            )
        outsider = Actor("estimator-1", "org-1", frozenset({ActorRole.ESTIMATOR}))
        with pytest.raises(HTTPException) as denied:
            service.request_rework(
                actor=outsider,
                project_id="project-1",
                command=ExpertReworkCommand(gate_hash=gate_hash, **base),
                request_id="request-non-expert",
            )
        assert denied.value.status_code == 403
        assert session.scalars(select(ExpertReworkRequestRow)).all() == []


def test_current_release_finding_routes_to_earliest_automatic_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    factory = create_session_factory(engine)
    actor = Actor("expert-1", "org-1", frozenset({ActorRole.REVIEWER}))
    context = _context(finding_document=True)
    monkeypatch.setattr(ProjectService, "_build_release_context", lambda *_: context)

    with factory.begin() as session:
        _seed(session)
        session.flush()
        _, _, gate_hash, _, _ = ProjectService(
            session=session,
            settings=settings,
            object_store=LocalObjectStore(tmp_path / "objects"),
        ).evaluate_release_gates(actor=actor, project_id="project-1")
        result = _service(session, settings, tmp_path).request_rework(
            actor=actor,
            project_id="project-1",
            command=ExpertReworkCommand(
                expected_project_row_version=1,
                gate_target="bid",
                gate_hash=gate_hash,
                issues=(
                    ExpertReworkIssue(
                        kind="RELEASE_FINDING",
                        reference_id="document-1",
                        code="CRITICAL_DOCUMENT_MISSING",
                        comment="Re-run document acquisition and extraction for this document.",
                    ),
                ),
                reason="Return the missing critical document to the automatic intake stage.",
            ),
            request_id="request-document-rework",
        )
        assert result.target_stage is ApprovalState.EXTRACTION_IN_PROGRESS
        assert result.project.state is ApprovalState.EXTRACTION_IN_PROGRESS


def test_final_review_api_exposes_hash_bound_rework_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        app_env="test",
        database_url="sqlite+pysqlite://",
        local_object_store_path=tmp_path / "objects",
        allow_insecure_dev_auth=True,
        audit_signing_key="test-audit-signing-key-at-least-32-bytes",
    )
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    factory = create_session_factory(engine)
    with factory.begin() as session:
        _seed(session)
    context = _context()
    monkeypatch.setattr(ProjectService, "_build_release_context", lambda *_: context)
    app = create_app(
        settings,
        engine=engine,
        object_store=LocalObjectStore(tmp_path / "objects"),
    )
    headers = {
        "X-Dev-Actor": "expert-1",
        "X-Dev-Organization": "org-1",
        "X-Dev-Roles": "REVIEWER",
    }

    with TestClient(app) as client:
        gates = client.get("/v1/projects/project-1/release-gates", headers=headers)
        assert gates.status_code == 200
        response = client.post(
            "/v1/projects/project-1/final-review/rework",
            headers={**headers, "Idempotency-Key": "final-review-api-1"},
            json={
                "expected_project_row_version": 1,
                "gate_target": "bid",
                "gate_hash": gates.json()["gate_hash"],
                "issues": [
                    {
                        "kind": "BOQ_PRICE_ROW",
                        "reference_id": "boq-line-1:1",
                        "code": "EXPERT_RECHECK_REQUESTED",
                        "comment": "Recheck the supplier and delivery evidence.",
                    }
                ],
                "reason": "Return the selected row to the automatic pricing stage.",
            },
        )
        assert response.status_code == 201
        assert response.json()["target_stage"] == "PRICING_IN_PROGRESS"
        assert response.json()["project"]["state"] == "PRICING_IN_PROGRESS"
