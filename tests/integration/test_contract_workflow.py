from datetime import UTC, datetime
from pathlib import Path

from tenderguard.application.approvals import ApprovalDecisionCommand, ApprovalService
from tenderguard.application.contracts import (
    ContractCostImpactCommand,
    ContractService,
    ContractTermDraft,
)
from tenderguard.application.stage_gates import contract_stage_blockers
from tenderguard.config import Settings
from tenderguard.domain.enums import (
    ActorRole,
    ApprovalDecision,
    ApprovalState,
    ContractTermKind,
    EvidenceMethod,
)
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.database import (
    create_database_engine,
    create_schema_for_tests,
    create_session_factory,
)
from tenderguard.infrastructure.object_store import LocalObjectStore
from tenderguard.infrastructure.orm import (
    ContractTermRow,
    ControlledVersionRow,
    ObservationRow,
    ProjectControlledVersionRow,
    ProjectRow,
)
from tests.integration.support import project_memberships


def test_contract_term_cost_impact_requires_four_eyes_approval(
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
    submitter = Actor(
        "technical-1",
        "org-1",
        frozenset({ActorRole.TECHNICAL_EXPERT}),
    )
    reviewer = Actor("reviewer-1", "org-1", frozenset({ActorRole.REVIEWER}))
    estimator = Actor("estimator-1", "org-1", frozenset({ActorRole.ESTIMATOR}))
    now = datetime(2026, 7, 23, tzinfo=UTC)

    with factory.begin() as session:
        rules = ControlledVersionRow(
            id="contract-rules-v1",
            kind="contract_risk_rules",
            version_label="1",
            content_hash="a" * 64,
            status="APPROVED",
            payload={
                "contract": {
                    "required_term_kinds": ["PENALTIES"],
                    "independently_verified_term_kinds": [],
                }
            },
            approved_by="methodology-owner",
            approved_at=now,
        )
        approval_policy = ControlledVersionRow(
            id="approval-policy-v1",
            kind="approval_policy",
            version_label="1",
            content_hash="b" * 64,
            status="APPROVED",
            payload={
                "rules": [
                    {
                        "reason": "CONTRACT_COST_IMPACT",
                        "assigned_role": "REVIEWER",
                        "required": True,
                    }
                ]
            },
            approved_by="methodology-owner",
            approved_at=now,
        )
        session.add_all(
            (
                ProjectRow(
                    id="project-contract",
                    organization_id="org-1",
                    code="CONTRACT-1",
                    name="Contract workflow",
                    state=ApprovalState.PRICING_IN_PROGRESS.value,
                    row_version=1,
                    created_at=now,
                    updated_at=now,
                ),
                *project_memberships(
                    "project-contract",
                    (submitter, reviewer, estimator),
                    owner_id=submitter.actor_id,
                    now=now,
                ),
                rules,
                approval_policy,
                ProjectControlledVersionRow(
                    project_id="project-contract",
                    controlled_version_id=rules.id,
                    purpose="contract_risk_rules",
                    bound_by="methodology-owner",
                    bound_at=now,
                ),
                ProjectControlledVersionRow(
                    project_id="project-contract",
                    controlled_version_id=approval_policy.id,
                    purpose="approval_policy",
                    bound_by="methodology-owner",
                    bound_at=now,
                ),
                ObservationRow(
                    id="observation-penalties",
                    project_id="project-contract",
                    document_revision_id="revision-contract",
                    field_name="contract_penalties",
                    method=EvidenceMethod.MANUAL.value,
                    method_version="human-review-v1",
                    status="UNVERIFIED",
                    payload={"observation": {"value": "0.1% per day", "unit": None}},
                    created_at=now,
                ),
            )
        )

    with factory.begin() as session:
        service = ContractService(session=session, settings=settings, object_store=store)
        term = service.submit_term(
            actor=submitter,
            project_id="project-contract",
            draft=ContractTermDraft(
                kind=ContractTermKind.PENALTIES,
                value="0.1% per day",
                observation_ids=("observation-penalties",),
            ),
            request_id="request-submit-term",
            reason="Extract penalties clause",
        )
        verified, incomplete = service.verify_term(
            actor=reviewer,
            project_id="project-contract",
            term_id=term.term_id,
            request_id="request-verify-term",
            reason="Review contract clause",
        )
        assert verified.verified
        assert incomplete.findings

        proposal = service.propose_cost_impact(
            actor=estimator,
            project_id="project-contract",
            term_id=term.term_id,
            command=ContractCostImpactCommand(
                amount=0,
                no_cost_reason=(
                    "Penalty exposure is represented in the approved risk model, "
                    "not as a deterministic base-cost line"
                ),
            ),
            request_id="request-impact-proposal",
            reason="Document cost treatment",
        )
        assert proposal.approval_task_ids
        approval = ApprovalService(
            session=session,
            settings=settings,
            object_store=store,
        ).decide(
            actor=reviewer,
            project_id="project-contract",
            task_id=proposal.approval_task_ids[0],
            command=ApprovalDecisionCommand(
                decision=ApprovalDecision.APPROVED,
                reason="Cost treatment agrees with the controlled risk methodology",
                evidence_ids=("observation-penalties",),
            ),
            request_id="request-approve-impact",
        )
        assert approval.decision is ApprovalDecision.APPROVED
        finalized, validation = service.finalize_cost_impact(
            actor=estimator,
            project_id="project-contract",
            term_id=proposal.term_id,
            request_id="request-finalize-impact",
            reason="Apply approved contract cost treatment",
        )
        assert finalized.cost_impact_resolved
        assert not validation.findings
        assert not contract_stage_blockers(session, "project-contract")
        revisions = list(
            session.query(ContractTermRow)
            .filter(ContractTermRow.project_id == "project-contract")
            .order_by(ContractTermRow.created_at)
        )
        assert [row.is_current for row in revisions] == [False, True]

    engine.dispose()
