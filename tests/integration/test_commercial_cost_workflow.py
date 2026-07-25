from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from tenderguard.application.approvals import ApprovalDecisionCommand, ApprovalService
from tenderguard.application.calculations import CalculationService
from tenderguard.application.commercial_costs import CommercialCostService
from tenderguard.application.contracts import (
    ContractCostImpactCommand,
    ContractService,
)
from tenderguard.application.lineage import LineageService
from tenderguard.application.stage_gates import pricing_stage_blockers
from tenderguard.config import Settings
from tenderguard.domain.calculation import (
    AtomicCostInput,
    CalculationPolicy,
)
from tenderguard.domain.commercial_costs import (
    CommercialCostModelInput,
    ContractCashFlow,
    ContractFinancePlan,
    FundingRatePeriod,
    LogisticsPlan,
    TransportLeg,
)
from tenderguard.domain.enums import (
    ActorRole,
    ApprovalDecision,
    ApprovalState,
    CommercialCostModelKind,
    ContractCashFlowKind,
    ContractTermKind,
    CostCategory,
)
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.database import (
    create_database_engine,
    create_schema_for_tests,
    create_session_factory,
)
from tenderguard.infrastructure.object_store import LocalObjectStore
from tenderguard.infrastructure.orm import (
    BoqLineRow,
    CommercialCostModelRow,
    ContractTermRow,
    ControlledVersionRow,
    DocumentRevisionRow,
    DocumentRow,
    DocumentSetRevisionRow,
    ObservationRow,
    ProjectControlledVersionRow,
    ProjectRow,
    QuantityRow,
)
from tests.integration.support import approval_task_updated_at, project_memberships


def test_commercial_cost_model_requires_evidence_independent_recalculation_and_four_eyes(
    tmp_path: Path,
) -> None:
    settings = Settings(
        app_env="test",
        database_url="sqlite+pysqlite://",
        local_object_store_path=tmp_path / "objects",
        audit_signing_key="commercial-cost-audit-key-at-least-32-bytes",
    )
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    factory = create_session_factory(engine)
    store = LocalObjectStore(tmp_path / "objects")
    estimator = Actor("estimator-commercial", "org-1", frozenset({ActorRole.ESTIMATOR}))
    reviewer = Actor("reviewer-commercial", "org-1", frozenset({ActorRole.REVIEWER}))
    now = datetime(2026, 7, 24, tzinfo=UTC)

    versions = (
        ControlledVersionRow(
            id="commercial-cost-policy-v1",
            kind="commercial_cost_model",
            version_label="1",
            content_hash="a" * 64,
            status="APPROVED",
            payload={
                "policy": {
                    "currency": "RUB",
                    "line_rounding_scale": 2,
                    "total_rounding_scale": 2,
                    "rounding_mode": "ROUND_HALF_UP",
                    "independent_tolerance": "0.00",
                    "day_count_basis": 365,
                    "max_finance_horizon_days": 3650,
                    "max_model_components": 1000,
                    "allow_zero_total": False,
                    "required_model_kinds": [
                        "LOGISTICS",
                        "CONTRACT_FINANCE",
                    ],
                    "required_logistics_sections": ["TRANSPORT"],
                    "required_mobilisation_kinds": [],
                    "required_cash_flow_kinds": [
                        "DIRECT_COST",
                        "CUSTOMER_PAYMENT",
                    ],
                    "required_contract_term_kinds": [
                        "ADVANCE",
                        "RETENTION",
                    ],
                }
            },
            approved_by="methodology-owner",
            approved_at=now,
        ),
        ControlledVersionRow(
            id="approval-policy-commercial-v1",
            kind="approval_policy",
            version_label="commercial-1",
            content_hash="b" * 64,
            status="APPROVED",
            payload={
                "rules": [
                    {
                        "reason": "LOGISTICS_MODEL",
                        "assigned_role": "REVIEWER",
                        "required": True,
                    },
                    {
                        "reason": "CONTRACT_FINANCE_MODEL",
                        "assigned_role": "REVIEWER",
                        "required": True,
                    },
                    {
                        "reason": "CONTRACT_COST_IMPACT",
                        "assigned_role": "REVIEWER",
                        "required": True,
                    },
                ]
            },
            approved_by="methodology-owner",
            approved_at=now,
        ),
        ControlledVersionRow(
            id="calculation-model-commercial-v1",
            kind="calculation_model",
            version_label="commercial-1",
            content_hash="c" * 64,
            status="APPROVED",
            payload={
                "policy": {
                    "currency": "RUB",
                    "line_rounding_scale": 2,
                    "total_rounding_scale": 2,
                    "rounding_mode": "ROUND_HALF_UP",
                    "independent_tolerance": "0.00",
                }
            },
            approved_by="methodology-owner",
            approved_at=now,
        ),
        ControlledVersionRow(
            id="catalog-commercial-v1",
            kind="catalog",
            version_label="commercial-1",
            content_hash="d" * 64,
            status="APPROVED",
            payload={},
            approved_by="catalog-owner",
            approved_at=now,
        ),
        ControlledVersionRow(
            id="price-policy-commercial-v1",
            kind="price_policy",
            version_label="commercial-1",
            content_hash="e" * 64,
            status="APPROVED",
            payload={},
            approved_by="methodology-owner",
            approved_at=now,
        ),
        ControlledVersionRow(
            id="contract-rules-commercial-v1",
            kind="contract_risk_rules",
            version_label="commercial-1",
            content_hash="9" * 64,
            status="APPROVED",
            payload={
                "contract": {
                    "required_term_kinds": ["ADVANCE"],
                    "independently_verified_term_kinds": [],
                }
            },
            approved_by="methodology-owner",
            approved_at=now,
        ),
    )

    with factory.begin() as session:
        session.add(
            ProjectRow(
                id="project-commercial",
                organization_id="org-1",
                code="COMMERCIAL-1",
                name="Governed logistics",
                state=ApprovalState.PRICING_IN_PROGRESS.value,
                row_version=1,
                current_document_set_revision_id="document-set-commercial",
                created_at=now,
                updated_at=now,
            )
        )
        session.add_all(
            project_memberships(
                "project-commercial",
                (estimator, reviewer),
                owner_id=estimator.actor_id,
                now=now,
            )
        )
        session.add_all(versions)
        for version, purpose in zip(
            versions,
            (
                "commercial_cost_model",
                "approval_policy",
                "calculation_model",
                "catalog",
                "price_policy",
                "contract_risk_rules",
            ),
            strict=True,
        ):
            session.add(
                ProjectControlledVersionRow(
                    project_id="project-commercial",
                    controlled_version_id=version.id,
                    purpose=purpose,
                    bound_by="methodology-owner",
                    bound_at=now,
                )
            )
        session.add(
            DocumentRow(
                id="document-commercial",
                project_id="project-commercial",
                logical_key="commercial-basis",
                title="Commercial cost basis",
                document_type="COMMERCIAL_BASIS",
                critical=True,
                cancelled=False,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            DocumentRevisionRow(
                id="revision-commercial",
                document_id="document-commercial",
                revision_label="1",
                issue_date=now.date(),
                object_hash="f" * 64,
                object_key="objects/commercial",
                original_filename="commercial.xlsx",
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
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
            DocumentSetRevisionRow(
                id="document-set-commercial",
                project_id="project-commercial",
                manifest_hash="1" * 64,
                revision_ids=["revision-commercial"],
                status="CONFIRMED",
                created_by="document-controller",
                created_at=now,
                confirmed_by="document-controller-reviewer",
                confirmed_at=now,
            )
        )
        session.add(
            DocumentSetRevisionRow(
                id="document-set-commercial-revised",
                project_id="project-commercial",
                manifest_hash="2" * 64,
                revision_ids=["revision-commercial"],
                status="CONFIRMED",
                created_by="document-controller",
                created_at=now,
                confirmed_by="document-controller-reviewer",
                confirmed_at=now,
            )
        )
        session.add(
            BoqLineRow(
                id="line-logistics",
                project_id="project-commercial",
                line_key="project-logistics",
                wbs_node_id="wbs-logistics",
                work_code="PROJECT_LOGISTICS",
                description="Detailed project logistics",
                unit="lot",
                status="VERIFIED",
                supersedes_line_id=None,
                is_current=True,
                payload={
                    "cost_components": [
                        {
                            "semantic_key": "project-logistics",
                            "category": "LOGISTICS",
                            "basis_kind": "DERIVED_MODEL",
                        }
                    ]
                },
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            BoqLineRow(
                id="line-contract-finance",
                project_id="project-commercial",
                line_key="contract-finance",
                wbs_node_id="wbs-contract",
                work_code="CONTRACT_FINANCE",
                description="Dated contract financing cost",
                unit="lot",
                status="VERIFIED",
                supersedes_line_id=None,
                is_current=True,
                payload={
                    "cost_components": [
                        {
                            "semantic_key": "contract-finance",
                            "category": "CONTRACT_FINANCE",
                            "basis_kind": "DERIVED_MODEL",
                        }
                    ]
                },
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            QuantityRow(
                id="quantity-logistics",
                boq_line_id="line-logistics",
                value=Decimal("1"),
                unit="lot",
                status="VERIFIED",
                supersedes_quantity_id=None,
                is_current=True,
                payload={},
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            QuantityRow(
                id="quantity-contract-finance",
                boq_line_id="line-contract-finance",
                value=Decimal("1"),
                unit="lot",
                status="VERIFIED",
                supersedes_quantity_id=None,
                is_current=True,
                payload={},
                created_at=now,
                updated_at=now,
            )
        )
        observation_payloads = {
            "obs-route": {
                "commercial_cost_bases": [
                    {
                        "model_kind": "LOGISTICS",
                        "component_id": "factory-to-site",
                        "values": {
                            "mode": "ROAD",
                            "origin": "Factory",
                            "destination": "Site",
                            "distance_km": "100",
                            "charged_distance_factor": "2",
                        },
                    }
                ]
            },
            "obs-cargo": {
                "commercial_cost_bases": [
                    {
                        "model_kind": "LOGISTICS",
                        "component_id": "factory-to-site",
                        "values": {
                            "cargo_mass_tonnes": "21",
                            "vehicle_mass_capacity_tonnes": "10",
                        },
                    }
                ]
            },
            "obs-rate": {
                "commercial_cost_bases": [
                    {
                        "model_kind": "LOGISTICS",
                        "component_id": "factory-to-site",
                        "values": {
                            "fixed_cost_per_trip": "1000",
                            "rate_per_vehicle_km": "10",
                            "toll_per_trip": "100",
                            "currency": "RUB",
                        },
                    }
                ]
            },
            "obs-direct-cost": {
                "commercial_cost_bases": [
                    {
                        "model_kind": "CONTRACT_FINANCE",
                        "component_id": "direct-cost",
                        "values": {
                            "kind": "DIRECT_COST",
                            "cash_date": "2026-08-01",
                            "amount": "-1000",
                            "currency": "RUB",
                            "contract_term_ids": [],
                        },
                    }
                ]
            },
            "obs-customer-payment": {
                "commercial_cost_bases": [
                    {
                        "model_kind": "CONTRACT_FINANCE",
                        "component_id": "customer-payment",
                        "values": {
                            "kind": "CUSTOMER_PAYMENT",
                            "cash_date": "2026-08-11",
                            "amount": "1000",
                            "currency": "RUB",
                            "contract_term_ids": ["term-retention"],
                        },
                    }
                ]
            },
            "obs-funding-rate": {
                "commercial_cost_bases": [
                    {
                        "model_kind": "CONTRACT_FINANCE",
                        "component_id": "funding-rate",
                        "values": {
                            "starts_on": "2026-08-01",
                            "ends_on": "2026-08-11",
                            "annual_rate": "0.365",
                        },
                    }
                ]
            },
            "obs-term-advance": {"observation": {"value": "Advance is paid on mobilisation"}},
            "obs-term-retention": {
                "observation": {"value": "Retention is released at final acceptance"}
            },
        }
        for observation_id, observation_payload in observation_payloads.items():
            session.add(
                ObservationRow(
                    id=observation_id,
                    project_id="project-commercial",
                    document_revision_id="revision-commercial",
                    field_name=observation_id,
                    method="RULE_ENGINE",
                    method_version="commercial-verification-v1",
                    status="VERIFIED",
                    payload=observation_payload,
                    created_at=now,
                )
            )
        session.add_all(
            (
                ContractTermRow(
                    id="term-advance",
                    project_id="project-commercial",
                    kind=ContractTermKind.ADVANCE.value,
                    verified=True,
                    cost_impact_resolved=False,
                    supersedes_term_id=None,
                    is_current=True,
                    payload={
                        "kind": ContractTermKind.ADVANCE.value,
                        "value": "Advance is paid on mobilisation",
                        "observation_ids": ["obs-term-advance"],
                        "created_by": "technical-commercial",
                        "verified_by": reviewer.actor_id,
                        "rules_version_id": "contract-rules-commercial-v1",
                        "document_set_revision_id": "document-set-commercial",
                    },
                    created_at=now,
                    updated_at=now,
                ),
                ContractTermRow(
                    id="term-retention",
                    project_id="project-commercial",
                    kind=ContractTermKind.RETENTION.value,
                    verified=True,
                    cost_impact_resolved=False,
                    supersedes_term_id=None,
                    is_current=True,
                    payload={
                        "kind": ContractTermKind.RETENTION.value,
                        "value": "Retention is released at final acceptance",
                        "observation_ids": ["obs-term-retention"],
                        "created_by": "technical-commercial",
                        "verified_by": reviewer.actor_id,
                        "rules_version_id": "contract-rules-commercial-v1",
                        "document_set_revision_id": "document-set-commercial",
                    },
                    created_at=now,
                    updated_at=now,
                ),
            )
        )

    model = CommercialCostModelInput(
        model_kind=CommercialCostModelKind.LOGISTICS,
        currency="RUB",
        target_line_id="line-logistics",
        target_semantic_key="project-logistics",
        logistics=LogisticsPlan(
            transport_legs=(
                TransportLeg(
                    component_id="factory-to-site",
                    mode="ROAD",
                    origin="Factory",
                    destination="Site",
                    distance_km=Decimal("100"),
                    charged_distance_factor=Decimal("2"),
                    cargo_mass_tonnes=Decimal("21"),
                    vehicle_mass_capacity_tonnes=Decimal("10"),
                    fixed_cost_per_trip=Decimal("1000"),
                    rate_per_vehicle_km=Decimal("10"),
                    toll_per_trip=Decimal("100"),
                    route_observation_ids=("obs-route",),
                    cargo_observation_ids=("obs-cargo",),
                    rate_observation_ids=("obs-rate",),
                ),
            )
        ),
    )

    with factory.begin() as session:
        service = CommercialCostService(
            session=session,
            settings=settings,
            object_store=store,
        )
        assert model.logistics is not None
        tampered_leg = model.logistics.transport_legs[0].model_copy(
            update={"distance_km": Decimal("101")}
        )
        tampered_model = model.model_copy(
            update={
                "logistics": model.logistics.model_copy(update={"transport_legs": (tampered_leg,)})
            }
        )
        with pytest.raises(ValueError, match="does not reproduce"):
            service.propose(
                actor=estimator,
                project_id="project-commercial",
                model=tampered_model,
                request_id="request-commercial-tamper",
                reason="A mismatched number must not borrow unrelated evidence",
            )
        proposal = service.propose(
            actor=estimator,
            project_id="project-commercial",
            model=model,
            request_id="request-commercial-proposal",
            reason="Price exact logistics plan",
        )
        assert proposal.model.status == "REVIEW_REQUIRED"
        assert proposal.model.total == Decimal("9300.00")
        assert proposal.evaluation.independent.passed
        with pytest.raises(ValueError, match="approvals are incomplete"):
            service.finalize(
                actor=estimator,
                project_id="project-commercial",
                model_id=proposal.model.model_id,
                request_id="request-premature-finalization",
                reason="Must fail before independent approval",
            )
        ApprovalService(
            session=session,
            settings=settings,
            object_store=store,
        ).decide(
            actor=reviewer,
            project_id="project-commercial",
            task_id=proposal.model.approval_task_ids[0],
            command=ApprovalDecisionCommand(
                decision=ApprovalDecision.APPROVED,
                reason="Routes, capacity, trip count, and rates reproduce evidence",
                expected_task_updated_at=approval_task_updated_at(
                    session,
                    proposal.model.approval_task_ids[0],
                ),
                evidence_ids=("obs-route", "obs-cargo", "obs-rate"),
            ),
            request_id="request-commercial-approval",
        )
        finalized = service.finalize(
            actor=estimator,
            project_id="project-commercial",
            model_id=proposal.model.model_id,
            request_id="request-commercial-finalization",
            reason="Use independently approved logistics result",
        )
        assert finalized.status == "VALIDATED"
        assert finalized.is_current
        assert finalized.approval_record_ids

        rejected_revision = service.propose(
            actor=estimator,
            project_id="project-commercial",
            model=model,
            request_id="request-logistics-revision",
            reason="Recheck a revised logistics submission",
        )
        ApprovalService(
            session=session,
            settings=settings,
            object_store=store,
        ).decide(
            actor=reviewer,
            project_id="project-commercial",
            task_id=rejected_revision.model.approval_task_ids[0],
            command=ApprovalDecisionCommand(
                decision=ApprovalDecision.CHANGES_REQUESTED,
                reason="Supplier basis must be re-submitted",
                expected_task_updated_at=approval_task_updated_at(
                    session,
                    rejected_revision.model.approval_task_ids[0],
                ),
            ),
            request_id="request-logistics-changes",
        )
        assert any(
            blocker.endswith("latest-unresolved")
            for blocker in pricing_stage_blockers(session, "project-commercial")
        )
        corrected_revision = service.propose(
            actor=estimator,
            project_id="project-commercial",
            model=model,
            request_id="request-logistics-correction",
            reason="Replace the review-rejected revision without overwriting it",
        )
        assert (
            session.get(CommercialCostModelRow, rejected_revision.model.model_id).status
            == "BLOCKED"
        )
        ApprovalService(
            session=session,
            settings=settings,
            object_store=store,
        ).decide(
            actor=reviewer,
            project_id="project-commercial",
            task_id=corrected_revision.model.approval_task_ids[0],
            command=ApprovalDecisionCommand(
                decision=ApprovalDecision.APPROVED,
                reason="Corrected supplier basis is acceptable",
                expected_task_updated_at=approval_task_updated_at(
                    session,
                    corrected_revision.model.approval_task_ids[0],
                ),
                evidence_ids=("obs-route", "obs-cargo", "obs-rate"),
            ),
            request_id="request-logistics-correction-approval",
        )
        finalized = service.finalize(
            actor=estimator,
            project_id="project-commercial",
            model_id=corrected_revision.model.model_id,
            request_id="request-logistics-correction-finalization",
            reason="Finalize the corrected immutable logistics revision",
        )
        assert finalized.supersedes_model_id == proposal.model.model_id
        reloaded = service.get(
            actor=reviewer,
            project_id="project-commercial",
            model_id=finalized.model_id,
        )
        assert reloaded.model_input == model
        assert reloaded.evaluation.independent.passed

        finance_model = CommercialCostModelInput(
            model_kind=CommercialCostModelKind.CONTRACT_FINANCE,
            currency="RUB",
            target_line_id="line-contract-finance",
            target_semantic_key="contract-finance",
            related_contract_term_ids=("term-advance", "term-retention"),
            contract_finance=ContractFinancePlan(
                valuation_start=date(2026, 8, 1),
                valuation_end=date(2026, 8, 11),
                cash_flows=(
                    ContractCashFlow(
                        cash_flow_id="direct-cost",
                        kind=ContractCashFlowKind.DIRECT_COST,
                        cash_date=date(2026, 8, 1),
                        amount=Decimal("-1000"),
                        observation_ids=("obs-direct-cost",),
                    ),
                    ContractCashFlow(
                        cash_flow_id="customer-payment",
                        kind=ContractCashFlowKind.CUSTOMER_PAYMENT,
                        cash_date=date(2026, 8, 11),
                        amount=Decimal("1000"),
                        observation_ids=("obs-customer-payment",),
                        contract_term_ids=("term-retention",),
                    ),
                ),
                funding_rate_periods=(
                    FundingRatePeriod(
                        rate_period_id="funding-rate",
                        starts_on=date(2026, 8, 1),
                        ends_on=date(2026, 8, 11),
                        annual_rate=Decimal("0.365"),
                        observation_ids=("obs-funding-rate",),
                    ),
                ),
            ),
        )
        finance_proposal = service.propose(
            actor=estimator,
            project_id="project-commercial",
            model=finance_model,
            request_id="request-finance-proposal",
            reason="Calculate financing from the dated contract cash flow",
        )
        assert finance_proposal.model.total == Decimal("10.00")
        ApprovalService(
            session=session,
            settings=settings,
            object_store=store,
        ).decide(
            actor=reviewer,
            project_id="project-commercial",
            task_id=finance_proposal.model.approval_task_ids[0],
            command=ApprovalDecisionCommand(
                decision=ApprovalDecision.APPROVED,
                reason="Cash-flow dates and funding rate reproduce contract evidence",
                expected_task_updated_at=approval_task_updated_at(
                    session,
                    finance_proposal.model.approval_task_ids[0],
                ),
                evidence_ids=(
                    "obs-direct-cost",
                    "obs-customer-payment",
                    "obs-funding-rate",
                    "obs-term-advance",
                    "obs-term-retention",
                ),
            ),
            request_id="request-finance-approval",
        )
        finalized_finance = service.finalize(
            actor=estimator,
            project_id="project-commercial",
            model_id=finance_proposal.model.model_id,
            request_id="request-finance-finalization",
            reason="Use independently approved contract finance result",
        )
        assert finalized_finance.is_current

        contract = ContractService(
            session=session,
            settings=settings,
            object_store=store,
        )
        impact = contract.propose_cost_impact(
            actor=estimator,
            project_id="project-commercial",
            term_id="term-advance",
            command=ContractCostImpactCommand(
                amount=Decimal("10.00"),
                currency="RUB",
                cost_component_line_id="line-contract-finance",
                cost_component_semantic_key="contract-finance",
                derived_cost_model_id=finalized_finance.model_id,
            ),
            request_id="request-contract-impact",
            reason="Link deterministic financing cost to the contract term",
        )
        ApprovalService(
            session=session,
            settings=settings,
            object_store=store,
        ).decide(
            actor=reviewer,
            project_id="project-commercial",
            task_id=impact.approval_task_ids[0],
            command=ApprovalDecisionCommand(
                decision=ApprovalDecision.APPROVED,
                reason="Finance model is the approved treatment of this term",
                expected_task_updated_at=approval_task_updated_at(
                    session,
                    impact.approval_task_ids[0],
                ),
                evidence_ids=("obs-term-advance", "obs-funding-rate"),
            ),
            request_id="request-contract-impact-approval",
        )
        project = session.get(ProjectRow, "project-commercial")
        assert project is not None
        project.current_document_set_revision_id = "document-set-commercial-revised"
        session.flush()
        with pytest.raises(
            ValueError,
            match="current validated finance model",
        ):
            contract.finalize_cost_impact(
                actor=estimator,
                project_id="project-commercial",
                term_id=impact.term_id,
                request_id="request-contract-impact-stale-documents",
                reason="Must reject a finance model from the prior document set",
            )
        assert (
            "commercial-cost-model:line-contract-finance:contract-finance"
            in pricing_stage_blockers(session, "project-commercial")
        )
        project.current_document_set_revision_id = "document-set-commercial"
        session.flush()
        finalized_impact, validation = contract.finalize_cost_impact(
            actor=estimator,
            project_id="project-commercial",
            term_id=impact.term_id,
            request_id="request-contract-impact-finalization",
            reason="Finalize the approved contract financing impact",
        )
        assert finalized_impact.cost_impact_resolved
        assert not validation.findings
        assert not pricing_stage_blockers(session, "project-commercial")

    with factory.begin() as session:
        project = session.get(ProjectRow, "project-commercial")
        assert project is not None
        project.state = ApprovalState.CALCULATION_IN_PROGRESS.value
        calculation = CalculationService(
            session=session,
            settings=settings,
            object_store=store,
        )
        inputs = (
            AtomicCostInput(
                cost_input_id="input-logistics",
                line_id="line-logistics",
                wbs_node_id="wbs-logistics",
                semantic_key="project-logistics",
                category=CostCategory.LOGISTICS,
                quantity=Decimal("1"),
                unit="lot",
                unit_rate=Decimal("9300.00"),
                currency="RUB",
                derived_cost_model_id=session.query(CommercialCostModelRow)
                .filter_by(
                    project_id=project.id,
                    model_kind="LOGISTICS",
                    is_current=True,
                )
                .one()
                .id,
            ),
            AtomicCostInput(
                cost_input_id="input-contract-finance",
                line_id="line-contract-finance",
                wbs_node_id="wbs-contract",
                semantic_key="contract-finance",
                category=CostCategory.CONTRACT_FINANCE,
                quantity=Decimal("1"),
                unit="lot",
                unit_rate=Decimal("10.00"),
                currency="RUB",
                derived_cost_model_id=session.query(CommercialCostModelRow)
                .filter_by(
                    project_id=project.id,
                    model_kind="CONTRACT_FINANCE",
                    is_current=True,
                )
                .one()
                .id,
            ),
        )
        policy = CalculationPolicy(
            policy_version="calculation-model-commercial-v1",
            currency="RUB",
            line_rounding_scale=2,
            total_rounding_scale=2,
            rounding_mode="ROUND_HALF_UP",
            independent_tolerance=Decimal("0.00"),
            expected_semantic_keys=frozenset({"project-logistics", "contract-finance"}),
        )
        project.current_document_set_revision_id = "document-set-commercial-revised"
        session.flush()
        with pytest.raises(ValueError, match="document-aligned"):
            calculation.execute(
                actor=estimator,
                project_id=project.id,
                expected_row_version=project.row_version,
                inputs=inputs,
                policy=policy,
                request_id="request-project-calculation-stale-documents",
                reason="Must reject derived costs from a prior document set",
            )
        project.current_document_set_revision_id = "document-set-commercial"
        session.flush()
        context = calculation.context(
            actor=estimator,
            project_id=project.id,
        )
        assert context.candidate is not None
        assert not context.blockers
        result = calculation.execute_current(
            actor=estimator,
            project_id=project.id,
            expected_row_version=project.row_version,
            candidate_hash=context.candidate.candidate_hash,
            request_id="request-project-calculation",
            reason="Include validated logistics as an atomic project cost",
        )
        assert result.primary.grand_total == Decimal("9310.00")
        assert result.independent.passed
        lineage = LineageService(
            session=session,
            settings=settings,
            object_store=store,
        ).snapshot_lineage(
            actor=estimator,
            project_id=project.id,
            snapshot_id=result.snapshot.snapshot_id,
        )
        derived = {item.semantic_key: item.evidence for item in lineage.cost_inputs}
        assert {item.basis_type for item in derived.values()} == {"DERIVED_COMMERCIAL_COST"}
        assert len(derived["project-logistics"].source_observations) == 3
        assert len(derived["contract-finance"].source_observations) == 3

    engine.dispose()
