from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from tenderguard.application.risks import (
    RiskItemDecisionCommand,
    RiskItemDraft,
    RiskService,
)
from tenderguard.application.stage_gates import risk_stage_blockers
from tenderguard.config import Settings
from tenderguard.domain.common import content_hash
from tenderguard.domain.enums import (
    ActorRole,
    ApprovalDecision,
    ApprovalState,
    EvidenceMethod,
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
    BoqLineRow,
    ControlledVersionRow,
    DocumentRevisionRow,
    DocumentRow,
    DocumentSetRevisionRow,
    ObservationRow,
    ProjectRow,
    QuantityRow,
    RiskCalculationRow,
)
from tests.integration.support import (
    add_document_set_confirmation_audit,
    add_governed_controlled_version,
    add_project_controlled_version_binding,
    approval_task_updated_at,
    project_memberships,
)


def test_verified_risk_register_produces_replayed_versioned_reserve(
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
    estimator = Actor(
        "estimator-1",
        "org-1",
        frozenset(
            {
                ActorRole.ESTIMATOR,
                ActorRole.TECHNICAL_EXPERT,
                ActorRole.REVIEWER,
            }
        ),
    )
    reviewer = Actor("reviewer-1", "org-1", frozenset({ActorRole.REVIEWER}))
    methodology_creator = Actor(
        "methodology-creator",
        "org-1",
        frozenset({ActorRole.METHODOLOGY_OWNER}),
    )
    methodology_approver = Actor(
        "methodology-approver",
        "org-1",
        frozenset({ActorRole.METHODOLOGY_OWNER}),
    )
    now = datetime(2026, 7, 23, tzinfo=UTC)
    draft = RiskItemDraft(
        risk_key="supplier-delay",
        description="Supplier delay and expediting exposure",
        probability=Decimal("0.3"),
        impact_min=Decimal("100"),
        impact_most_likely=Decimal("200"),
        impact_max=Decimal("400"),
        currency="RUB",
        observation_ids=("observation-risk",),
    )

    with factory.begin() as session:
        model = ControlledVersionRow(
            id="risk-model-v1",
            kind="risk_model",
            version_label="1",
            content_hash="",
            status="DRAFT",
            payload={
                "policy": {
                    "method": "THREE_POINT_EXPECTED_VALUE",
                    "currency": "RUB",
                    "rounding_scale": 2,
                    "rounding_mode": "ROUND_HALF_UP",
                },
                "risk_keys": ["supplier-delay"],
                "required_risk_keys": ["supplier-delay"],
                "minimum_risk_items": 1,
                "independently_verified_risk_keys": [],
                "evidence_field_names": {
                    "supplier-delay": "risk_supplier_delay",
                },
                "review_role": "REVIEWER",
                "reserve_unit": "project",
                "reserve_cost_component": {
                    "line_id": "boq-line-risk",
                    "semantic_key": "risk-reserve",
                },
            },
            approved_by=None,
            approved_at=None,
        )
        document_set = DocumentSetRevisionRow(
            id="document-set-risk",
            project_id="project-risk",
            manifest_hash=content_hash(["revision-risk"]),
            revision_ids=["revision-risk"],
            status="CONFIRMED",
            created_by=estimator.actor_id,
            created_at=now,
            confirmed_by=reviewer.actor_id,
            confirmed_at=now,
        )
        session.add_all(
            (
                ProjectRow(
                    id="project-risk",
                    organization_id="org-1",
                    code="RISK-1",
                    name="Risk workflow",
                    state=ApprovalState.PRICING_IN_PROGRESS.value,
                    row_version=1,
                    current_document_set_revision_id=document_set.id,
                    created_at=now,
                    updated_at=now,
                ),
                *project_memberships(
                    "project-risk",
                    (estimator, reviewer),
                    owner_id=estimator.actor_id,
                    now=now,
                ),
                document_set,
                DocumentRow(
                    id="document-risk",
                    project_id="project-risk",
                    logical_key="risk-workshop",
                    title="Risk workshop record",
                    document_type="RISK_REGISTER",
                    critical=True,
                    cancelled=False,
                    created_at=now,
                    updated_at=now,
                ),
                DocumentRevisionRow(
                    id="revision-risk",
                    document_id="document-risk",
                    revision_label="1",
                    issue_date=date(2026, 7, 23),
                    object_hash="a" * 64,
                    object_key="objects/risk-workshop",
                    original_filename="risk-workshop.pdf",
                    media_type="application/pdf",
                    size_bytes=100,
                    supersedes_revision_id=None,
                    is_current=True,
                    corrupt=False,
                    protected=False,
                    inspection_payload={},
                    created_at=now,
                    updated_at=now,
                ),
                BoqLineRow(
                    id="boq-line-risk",
                    project_id="project-risk",
                    line_key="risk",
                    wbs_node_id="wbs-risk",
                    work_code="PROJECT_RISK_RESERVE",
                    description="Project risk reserve",
                    unit="project",
                    status="VERIFIED",
                    payload={
                        "cost_components": [
                            {
                                "semantic_key": "risk-reserve",
                                "category": "RISK",
                                "basis_kind": "RISK_MODEL",
                            }
                        ]
                    },
                    created_at=now,
                    updated_at=now,
                ),
                QuantityRow(
                    id="quantity-risk",
                    boq_line_id="boq-line-risk",
                    value=Decimal("1"),
                    unit="project",
                    status="VERIFIED",
                    supersedes_quantity_id=None,
                    is_current=True,
                    payload={},
                    created_at=now,
                    updated_at=now,
                ),
            )
        )
        session.flush()
        add_governed_controlled_version(
            session=session,
            settings=settings,
            object_store=store,
            row=model,
            organization_id="org-1",
            creator=methodology_creator,
            approver=methodology_approver,
        )
        add_project_controlled_version_binding(
            session=session,
            settings=settings,
            object_store=store,
            project_id="project-risk",
            version=model,
            purpose="risk_model",
            actor=methodology_approver,
        )
        add_document_set_confirmation_audit(
            session=session,
            settings=settings,
            object_store=store,
            row=document_set,
            actor=reviewer,
        )
        observation = Observation(
            observation_id="observation-risk",
            field_name="risk_supplier_delay",
            value=draft.evidence_value(),
            unit=None,
            method=EvidenceMethod.RULE_ENGINE,
            method_version="risk-extraction-v1",
            source_priority=1,
            location=EvidenceLocation(
                document_id="document-risk",
                document_revision_id="revision-risk",
                original_object_hash="a" * 64,
                locator_kind="table",
                locator="risk-table:row:1",
                page=4,
            ),
            observed_at=now,
            actor_id="risk-extraction-service",
        )
        session.add(
            ObservationRow(
                id=observation.observation_id,
                project_id="project-risk",
                document_revision_id="revision-risk",
                field_name=observation.field_name,
                method=observation.method.value,
                method_version=observation.method_version,
                status=observation.status.value,
                payload={"observation": observation.model_dump(mode="json")},
                created_at=now,
            )
        )

    with factory.begin() as session:
        service = RiskService(session=session, settings=settings, object_store=store)
        context = service.context(
            actor=estimator,
            project_id="project-risk",
            selected_risk_key="supplier-delay",
            limit=100,
        )
        assert context.evidence_candidates[0].eligible
        submitted = service.submit_risk(
            actor=estimator,
            project_id="project-risk",
            draft=draft,
            expected_document_set_revision_id=context.document_set_revision_id,
            risk_model_version_id=context.risk_model_version_id,
            request_id="request-submit-risk",
            reason="Register exact risk evidence",
        )
        with pytest.raises(ValueError, match="FOUR_EYES"):
            service.decide_risk(
                actor=estimator,
                project_id="project-risk",
                risk_item_id=submitted.row_id,
                command=RiskItemDecisionCommand(
                    decision=ApprovalDecision.APPROVED,
                    expected_risk_updated_at=submitted.updated_at,
                    expected_task_updated_at=approval_task_updated_at(
                        session,
                        submitted.approval_task_id,
                    ),
                ),
                request_id="request-self-verify",
                reason="Invalid self-review",
            )
        verified = service.decide_risk(
            actor=reviewer,
            project_id="project-risk",
            risk_item_id=submitted.row_id,
            command=RiskItemDecisionCommand(
                decision=ApprovalDecision.APPROVED,
                expected_risk_updated_at=submitted.updated_at,
                expected_task_updated_at=approval_task_updated_at(
                    session,
                    submitted.approval_task_id,
                ),
            ),
            request_id="request-verify-risk",
            reason="Independent risk evidence review",
        ).item
        assert verified.risk.status.value == "VERIFIED"
        result = service.calculate_reserve(
            actor=reviewer,
            project_id="project-risk",
            expected_document_set_revision_id=context.document_set_revision_id,
            risk_model_version_id=context.risk_model_version_id,
            request_id="request-calculate-risk",
            reason="Run approved deterministic risk model",
        )
        assert result.status == "VALIDATED"
        assert result.calculation.expected_reserve == Decimal("70")
        assert result.independent_validation_passed
        assert not risk_stage_blockers(
            session,
            settings,
            "project-risk",
        )
        persisted = session.get(RiskCalculationRow, result.calculation_id)
        assert persisted is not None
        assert persisted.payload["basis_type"] == "RISK_RESERVE"
        assert persisted.payload["unit_rate"] == "70.00"
        persisted.expected_reserve = Decimal("69")
        session.flush()
        assert risk_stage_blockers(
            session,
            settings,
            "project-risk",
        ) == (f"risk-calculation:{persisted.id}:integrity-failed",)

    engine.dispose()
