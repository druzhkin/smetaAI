from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from tenderguard.application.risks import RiskItemDraft, RiskService
from tenderguard.application.stage_gates import risk_stage_blockers
from tenderguard.config import Settings
from tenderguard.domain.enums import ActorRole, ApprovalState, EvidenceMethod
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
    ObservationRow,
    ProjectControlledVersionRow,
    ProjectRow,
    QuantityRow,
    RiskCalculationRow,
)


def test_verified_risk_register_produces_versioned_calculation_basis(
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
        frozenset({ActorRole.ESTIMATOR, ActorRole.TECHNICAL_EXPERT}),
    )
    reviewer = Actor("reviewer-1", "org-1", frozenset({ActorRole.REVIEWER}))
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
            content_hash="a" * 64,
            status="APPROVED",
            payload={
                "policy": {
                    "method": "THREE_POINT_EXPECTED_VALUE",
                    "currency": "RUB",
                    "rounding_scale": 2,
                    "rounding_mode": "ROUND_HALF_UP",
                },
                "minimum_risk_items": 1,
                "independently_verified_risk_keys": [],
                "reserve_unit": "project",
                "reserve_cost_component": {
                    "line_id": "boq-line-risk",
                    "semantic_key": "risk-reserve",
                },
            },
            approved_by="methodology-owner",
            approved_at=now,
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
                    created_at=now,
                    updated_at=now,
                ),
                model,
                ProjectControlledVersionRow(
                    project_id="project-risk",
                    controlled_version_id=model.id,
                    purpose="risk_model",
                    bound_by="methodology-owner",
                    bound_at=now,
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
                ObservationRow(
                    id="observation-risk",
                    project_id="project-risk",
                    document_revision_id="revision-risk",
                    field_name="risk_item",
                    method=EvidenceMethod.MANUAL.value,
                    method_version="risk-workshop-v1",
                    status="UNVERIFIED",
                    payload={
                        "observation": {
                            "value": draft.evidence_value(),
                            "unit": None,
                        }
                    },
                    created_at=now,
                ),
            )
        )

    with factory.begin() as session:
        service = RiskService(session=session, settings=settings, object_store=store)
        submitted = service.submit_risk(
            actor=estimator,
            project_id="project-risk",
            draft=draft,
            request_id="request-submit-risk",
            reason="Register risk workshop result",
        )
        with pytest.raises(ValueError, match="different actor"):
            service.verify_risk(
                actor=estimator,
                project_id="project-risk",
                risk_item_id=submitted.row_id,
                request_id="request-self-verify",
                reason="Invalid self-review",
            )
        verified = service.verify_risk(
            actor=reviewer,
            project_id="project-risk",
            risk_item_id=submitted.row_id,
            request_id="request-verify-risk",
            reason="Independent risk review",
        )
        assert verified.risk.status.value == "VERIFIED"
        result = service.calculate_reserve(
            actor=reviewer,
            project_id="project-risk",
            request_id="request-calculate-risk",
            reason="Run approved deterministic risk model",
        )
        assert result.status == "VALIDATED"
        assert result.calculation.expected_reserve == Decimal("70")
        assert not risk_stage_blockers(session, "project-risk")
        persisted = session.get(RiskCalculationRow, result.calculation_id)
        assert persisted is not None
        assert persisted.payload["basis_type"] == "RISK_RESERVE"
        assert persisted.payload["unit_rate"] == "70.00"

    engine.dispose()
