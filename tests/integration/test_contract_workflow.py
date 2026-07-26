from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from tenderguard.application.approvals import ApprovalDecisionCommand, ApprovalService
from tenderguard.application.contracts import (
    ContractCostImpactCommand,
    ContractService,
    ContractTermDecisionCommand,
    ContractTermDraft,
)
from tenderguard.application.stage_gates import contract_stage_blockers
from tenderguard.config import Settings
from tenderguard.domain.common import content_hash, ensure_utc
from tenderguard.domain.enums import (
    ActorRole,
    ApprovalDecision,
    ApprovalState,
    ContractTermKind,
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
    ApprovalTaskRow,
    ContractTermRow,
    ControlledVersionRow,
    DocumentRevisionRow,
    DocumentRow,
    DocumentSetRevisionRow,
    ObservationRow,
    ProjectRow,
)
from tests.integration.support import (
    add_document_set_confirmation_audit,
    add_governed_controlled_version,
    add_project_controlled_version_binding,
    approval_task_updated_at,
    project_memberships,
)


def test_contract_term_and_cost_impact_require_reproducible_four_eyes(
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
        frozenset({ActorRole.TECHNICAL_EXPERT, ActorRole.REVIEWER}),
    )
    reviewer = Actor("reviewer-1", "org-1", frozenset({ActorRole.REVIEWER}))
    estimator = Actor("estimator-1", "org-1", frozenset({ActorRole.ESTIMATOR}))
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

    with factory.begin() as session:
        rules = ControlledVersionRow(
            id="contract-rules-v1",
            kind="contract_risk_rules",
            version_label="1",
            content_hash="",
            status="DRAFT",
            payload={
                "contract": {
                    "required_term_kinds": ["PENALTIES"],
                    "independently_verified_term_kinds": [],
                    "evidence_field_names": {
                        "PENALTIES": "contract_penalties",
                    },
                    "review_role": "REVIEWER",
                }
            },
            approved_by=None,
            approved_at=None,
        )
        approval_policy = ControlledVersionRow(
            id="approval-policy-v1",
            kind="approval_policy",
            version_label="1",
            content_hash="",
            status="DRAFT",
            payload={
                "rules": [
                    {
                        "reason": "CONTRACT_COST_IMPACT",
                        "assigned_role": "REVIEWER",
                        "required": True,
                    }
                ]
            },
            approved_by=None,
            approved_at=None,
        )
        document_set = DocumentSetRevisionRow(
            id="document-set-contract",
            project_id="project-contract",
            manifest_hash=content_hash(["revision-contract"]),
            revision_ids=["revision-contract"],
            status="CONFIRMED",
            created_by=submitter.actor_id,
            created_at=now,
            confirmed_by=reviewer.actor_id,
            confirmed_at=now,
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
                    current_document_set_revision_id=document_set.id,
                    created_at=now,
                    updated_at=now,
                ),
                *project_memberships(
                    "project-contract",
                    (submitter, reviewer, estimator),
                    owner_id=submitter.actor_id,
                    now=now,
                ),
                document_set,
                DocumentRow(
                    id="document-contract",
                    project_id="project-contract",
                    logical_key="contract",
                    title="Tender contract",
                    document_type="CONTRACT",
                    critical=True,
                    cancelled=False,
                    created_at=now,
                    updated_at=now,
                ),
                DocumentRevisionRow(
                    id="revision-contract",
                    document_id="document-contract",
                    revision_label="1",
                    issue_date=date(2026, 7, 23),
                    object_hash="a" * 64,
                    object_key="objects/contract",
                    original_filename="contract.pdf",
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
            )
        )
        session.flush()
        for row, purpose in (
            (rules, "contract_risk_rules"),
            (approval_policy, "approval_policy"),
        ):
            add_governed_controlled_version(
                session=session,
                settings=settings,
                object_store=store,
                row=row,
                organization_id="org-1",
                creator=methodology_creator,
                approver=methodology_approver,
            )
            add_project_controlled_version_binding(
                session=session,
                settings=settings,
                object_store=store,
                project_id="project-contract",
                version=row,
                purpose=purpose,
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
            observation_id="observation-penalties",
            field_name="contract_penalties",
            value="0.1% per day",
            unit=None,
            method=EvidenceMethod.RULE_ENGINE,
            method_version="contract-rules-v1",
            source_priority=1,
            location=EvidenceLocation(
                document_id="document-contract",
                document_revision_id="revision-contract",
                original_object_hash="a" * 64,
                locator_kind="clause",
                locator="clause:12.4",
                page=12,
            ),
            observed_at=now,
            actor_id="contract-extraction-service",
        )
        session.add(
            ObservationRow(
                id=observation.observation_id,
                project_id="project-contract",
                document_revision_id="revision-contract",
                field_name=observation.field_name,
                method=observation.method.value,
                method_version=observation.method_version,
                status=observation.status.value,
                payload={"observation": observation.model_dump(mode="json")},
                created_at=now,
            )
        )
        manual_observation = Observation(
            observation_id="observation-penalties-manual-unreviewed",
            field_name="contract_penalties",
            value="0.1% per day",
            unit=None,
            method=EvidenceMethod.MANUAL,
            method_version="manual-contract-v1",
            source_priority=5,
            location=EvidenceLocation(
                document_id="document-contract",
                document_revision_id="revision-contract",
                original_object_hash="a" * 64,
                locator_kind="clause",
                locator="clause:12.4:manual",
                page=12,
            ),
            observed_at=now,
            actor_id=submitter.actor_id,
        )
        session.add(
            ObservationRow(
                id=manual_observation.observation_id,
                project_id="project-contract",
                document_revision_id="revision-contract",
                field_name=manual_observation.field_name,
                method=manual_observation.method.value,
                method_version=manual_observation.method_version,
                status=manual_observation.status.value,
                payload={"observation": manual_observation.model_dump(mode="json")},
                created_at=now,
            )
        )

    with factory.begin() as session:
        service = ContractService(session=session, settings=settings, object_store=store)
        context = service.context(
            actor=submitter,
            project_id="project-contract",
            selected_kind=ContractTermKind.PENALTIES,
            limit=100,
        )
        assert context.rules_version_id == "contract-rules-v1"
        eligible_candidate = next(
            candidate
            for candidate in context.evidence_candidates
            if candidate.observation.observation_id == "observation-penalties"
        )
        manual_candidate = next(
            candidate
            for candidate in context.evidence_candidates
            if candidate.observation.observation_id == "observation-penalties-manual-unreviewed"
        )
        assert eligible_candidate.eligible
        assert not manual_candidate.eligible
        assert "MANUAL_EVIDENCE_REVIEW_REQUIRED" in manual_candidate.blockers
        assert context.validation.findings

        with pytest.raises(ValueError, match="dedicated review"):
            service.submit_term(
                actor=submitter,
                project_id="project-contract",
                draft=ContractTermDraft(
                    kind=ContractTermKind.PENALTIES,
                    value="0.1% per day",
                    observation_ids=("observation-penalties-manual-unreviewed",),
                ),
                expected_document_set_revision_id=context.document_set_revision_id,
                rules_version_id=context.rules_version_id,
                request_id="request-submit-unreviewed-manual",
                reason="Must reject raw manual evidence",
            )

        term = service.submit_term(
            actor=submitter,
            project_id="project-contract",
            draft=ContractTermDraft(
                kind=ContractTermKind.PENALTIES,
                value="0.1% per day",
                observation_ids=("observation-penalties",),
            ),
            expected_document_set_revision_id=context.document_set_revision_id,
            rules_version_id=context.rules_version_id,
            request_id="request-submit-term",
            reason="Extract penalties clause",
        )
        assert not term.verified
        assert term.approval_task_id

        with pytest.raises(ValueError, match="different actor"):
            service.decide_term(
                actor=submitter,
                project_id="project-contract",
                term_id=term.term_id,
                command=ContractTermDecisionCommand(
                    decision=ApprovalDecision.APPROVED,
                    expected_term_updated_at=term.updated_at,
                    expected_task_updated_at=approval_task_updated_at(
                        session,
                        term.approval_task_id,
                    ),
                ),
                request_id="request-self-review",
                reason="Invalid self review",
            )

        decision = service.decide_term(
            actor=reviewer,
            project_id="project-contract",
            term_id=term.term_id,
            command=ContractTermDecisionCommand(
                decision=ApprovalDecision.APPROVED,
                expected_term_updated_at=term.updated_at,
                expected_task_updated_at=approval_task_updated_at(
                    session,
                    term.approval_task_id,
                ),
            ),
            request_id="request-verify-term",
            reason="Review contract clause against cited source",
        )
        assert decision.term.verified
        assert decision.validation.findings

        proposal = service.propose_cost_impact(
            actor=estimator,
            project_id="project-contract",
            term_id=decision.term.term_id,
            command=ContractCostImpactCommand(
                amount=0,
                no_cost_reason=(
                    "Penalty exposure is represented in the approved risk model, "
                    "not as a deterministic base-cost line"
                ),
            ),
            expected_term_updated_at=decision.term.updated_at,
            request_id="request-impact-proposal",
            reason="Document cost treatment",
        )
        assert proposal.approval_task_ids
        ApprovalService(
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
                expected_task_updated_at=approval_task_updated_at(
                    session,
                    proposal.approval_task_ids[0],
                ),
                evidence_ids=("observation-penalties",),
            ),
            request_id="request-approve-impact",
        )
        finalized, validation = service.finalize_cost_impact(
            actor=estimator,
            project_id="project-contract",
            term_id=proposal.term_id,
            expected_term_updated_at=proposal.updated_at,
            request_id="request-finalize-impact",
            reason="Apply approved contract cost treatment",
        )
        assert finalized.cost_impact_resolved
        assert not validation.findings
        assert not contract_stage_blockers(session, settings, "project-contract")
        revisions = list(
            session.query(ContractTermRow)
            .filter(ContractTermRow.project_id == "project-contract")
            .order_by(ContractTermRow.created_at)
        )
        assert [row.is_current for row in revisions] == [False, True]

        current = revisions[-1]
        current.payload = {**current.payload, "value": "tampered"}
        session.flush()
        assert contract_stage_blockers(session, settings, "project-contract") == (
            "contract-term:PENALTIES:integrity-failed",
        )
        current.payload = {**current.payload, "value": "0.1% per day"}
        session.flush()

        task = session.get(ApprovalTaskRow, term.approval_task_id)
        assert task is not None
        assert ensure_utc(task.updated_at) is not None

    engine.dispose()
