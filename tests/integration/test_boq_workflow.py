from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from tenderguard.application.approvals import (
    ApprovalDecisionCommand,
    ApprovalService,
)
from tenderguard.application.boq import (
    BoqLineDraft,
    BoqService,
    CostComponentDraft,
    QuantityDraft,
    QuantitySubmission,
)
from tenderguard.application.projects import ProjectService
from tenderguard.config import Settings
from tenderguard.domain.common import content_hash
from tenderguard.domain.enums import (
    ActorRole,
    ApprovalDecision,
    ApprovalState,
    CostBasisKind,
    CostCategory,
    EvidenceMethod,
    QuantityOperation,
    VerificationStatus,
)
from tenderguard.domain.models import EvidenceLocation, Observation
from tenderguard.domain.quantities import QuantityFormulaDefinition
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.database import (
    create_database_engine,
    create_schema_for_tests,
    create_session_factory,
)
from tenderguard.infrastructure.object_store import LocalObjectStore
from tenderguard.infrastructure.orm import (
    ControlledVersionRow,
    DocumentSetRevisionRow,
    ObservationRow,
    ProjectRow,
    QuantityManualChangeApplicationRow,
    QuantityRow,
)
from tests.integration.support import (
    add_document_set_confirmation_audit,
    add_governed_controlled_version,
    add_project_controlled_version_binding,
    project_memberships,
)


def test_boq_quantity_revision_and_scope_findings_are_operational(tmp_path: Path) -> None:
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
    estimator = Actor("estimator-1", "org-1", frozenset({ActorRole.ESTIMATOR}))
    reviewer = Actor(
        "reviewer-1",
        "org-1",
        frozenset({ActorRole.REVIEWER, ActorRole.TECHNICAL_EXPERT}),
    )
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
        session.add(
            ProjectRow(
                id="project-boq",
                organization_id="org-1",
                code="BOQ-1",
                name="BoQ workflow",
                state=ApprovalState.BOQ_IN_PROGRESS.value,
                current_document_set_revision_id="document-set-boq",
                row_version=1,
                created_at=now,
                updated_at=now,
            )
        )
        document_set = DocumentSetRevisionRow(
            id="document-set-boq",
            project_id="project-boq",
            manifest_hash=content_hash(["document-revision-1"]),
            revision_ids=["document-revision-1"],
            status="CONFIRMED",
            created_by=estimator.actor_id,
            created_at=now,
            confirmed_by=reviewer.actor_id,
            confirmed_at=now,
        )
        session.add(document_set)
        session.add_all(
            project_memberships(
                "project-boq",
                (estimator, reviewer),
                owner_id=estimator.actor_id,
                now=now,
            )
        )
        add_document_set_confirmation_audit(
            session=session,
            settings=settings,
            object_store=store,
            row=document_set,
            actor=reviewer,
        )
        versions = (
            ControlledVersionRow(
                id="quantity-policy-v1",
                kind="quantity_policy",
                version_label="1",
                content_hash="a" * 64,
                status="APPROVED",
                payload={
                    "policy": {
                        "absolute_tolerance": "0.01",
                        "relative_tolerance": "0.001",
                        "allow_zero": False,
                        "allow_negative": False,
                    }
                },
                approved_by="methodology-owner",
                approved_at=now,
            ),
            ControlledVersionRow(
                id="quantity-formulas-v1",
                kind="quantity_formula_rules",
                version_label="1",
                content_hash="b" * 64,
                status="APPROVED",
                payload={"allowed_operations": ["PRODUCT"]},
                approved_by="methodology-owner",
                approved_at=now,
            ),
            ControlledVersionRow(
                id="manual-change-policy-v1",
                kind="manual_change_policy",
                version_label="1",
                content_hash="d" * 64,
                status="APPROVED",
                payload={
                    "policy": {
                        "rules": [
                            {
                                "entity_type": "quantity",
                                "field_name": "record",
                                "critical": True,
                                "assigned_role": "REVIEWER",
                            }
                        ]
                    }
                },
                approved_by="methodology-owner",
                approved_at=now,
            ),
            ControlledVersionRow(
                id="scope-rules-v1",
                kind="scope_rules",
                version_label="1",
                content_hash="c" * 64,
                status="APPROVED",
                payload={
                    "rules": [
                        {
                            "rule_id": "pipe-companions",
                            "trigger_any_work_codes": ["PIPE_INSTALLATION"],
                            "required_work_codes": [
                                "PIPE_INSTALLATION",
                                "TESTING",
                                "AS_BUILT_DOCUMENTATION",
                            ],
                            "rationale": "Pipeline installation needs testing and records",
                        }
                    ]
                },
                approved_by="methodology-owner",
                approved_at=now,
            ),
        )
        for version in versions:
            add_governed_controlled_version(
                session=session,
                settings=settings,
                object_store=store,
                row=version,
                organization_id="org-1",
                creator=methodology_creator,
                approver=methodology_approver,
            )
        for version, purpose in zip(
            versions,
            (
                "quantity_policy",
                "quantity_formula_rules",
                "manual_change_policy",
                "scope_rules",
            ),
            strict=True,
        ):
            add_project_controlled_version_binding(
                session=session,
                settings=settings,
                object_store=store,
                project_id="project-boq",
                version=version,
                purpose=purpose,
                actor=methodology_approver,
            )
        line_observation = Observation(
            observation_id="observation-line",
            field_name="boq_line",
            value={"work_code": "PIPE_INSTALLATION", "unit": "m"},
            unit=None,
            method=EvidenceMethod.RULE_ENGINE,
            method_version="reconciliation-v1",
            source_priority=1,
            location=EvidenceLocation(
                document_id="document-1",
                document_revision_id="document-revision-1",
                original_object_hash="a" * 64,
                locator_kind="table",
                locator="boq[row=1]",
            ),
            observed_at=now,
            actor_id=reviewer.actor_id,
            status=VerificationStatus.VERIFIED,
        )
        length_observation = Observation(
            observation_id="observation-length",
            field_name="length",
            value="100",
            unit="m",
            method=EvidenceMethod.RULE_ENGINE,
            method_version="reconciliation-v1",
            source_priority=1,
            location=EvidenceLocation(
                document_id="document-1",
                document_revision_id="document-revision-1",
                original_object_hash="a" * 64,
                locator_kind="table",
                locator="boq[row=1,column=quantity]",
            ),
            observed_at=now,
            actor_id=reviewer.actor_id,
            status=VerificationStatus.VERIFIED,
        )
        old_line_observation = line_observation.model_copy(
            update={
                "observation_id": "observation-line-old-set",
                "location": line_observation.location.model_copy(
                    update={"document_revision_id": "document-revision-old"}
                ),
            }
        )
        session.add_all(
            (
                ObservationRow(
                    id="observation-line",
                    project_id="project-boq",
                    document_revision_id="document-revision-1",
                    field_name="boq_line",
                    method=EvidenceMethod.RULE_ENGINE.value,
                    method_version="reconciliation-v1",
                    status=VerificationStatus.VERIFIED.value,
                    payload={"observation": line_observation.model_dump(mode="json")},
                    created_at=now,
                ),
                ObservationRow(
                    id="observation-length",
                    project_id="project-boq",
                    document_revision_id="document-revision-1",
                    field_name="length",
                    method=EvidenceMethod.RULE_ENGINE.value,
                    method_version="reconciliation-v1",
                    status=VerificationStatus.VERIFIED.value,
                    payload={"observation": length_observation.model_dump(mode="json")},
                    created_at=now,
                ),
                ObservationRow(
                    id=old_line_observation.observation_id,
                    project_id="project-boq",
                    document_revision_id="document-revision-old",
                    field_name=old_line_observation.field_name,
                    method=old_line_observation.method.value,
                    method_version=old_line_observation.method_version,
                    status=old_line_observation.status.value,
                    payload={"observation": old_line_observation.model_dump(mode="json")},
                    created_at=now + timedelta(seconds=1),
                ),
            )
        )

    with factory.begin() as session:
        service = BoqService(session=session, settings=settings, object_store=store)
        with pytest.raises(ValueError, match="limit must be between 1 and 100"):
            service.authoring_context(
                actor=estimator,
                project_id="project-boq",
                evidence_field_name="boq_line",
                limit=0,
            )
        with pytest.raises(ValueError, match="field name must contain"):
            service.authoring_context(
                actor=estimator,
                project_id="project-boq",
                evidence_field_name="   ",
                limit=100,
            )
        project = session.get(ProjectRow, "project-boq")
        assert project is not None
        project.state = ApprovalState.DRAFT.value
        with pytest.raises(ValueError, match="requires BOQ_IN_PROGRESS"):
            service.authoring_context(
                actor=estimator,
                project_id="project-boq",
                evidence_field_name="boq_line",
                limit=100,
            )
        project.state = ApprovalState.BOQ_IN_PROGRESS.value
        project.current_document_set_revision_id = None
        with pytest.raises(ValueError, match="confirmed current document set"):
            service.authoring_context(
                actor=estimator,
                project_id="project-boq",
                evidence_field_name="boq_line",
                limit=100,
            )
        project.current_document_set_revision_id = "missing-document-set"
        with pytest.raises(ValueError, match="confirmed current document set"):
            service.authoring_context(
                actor=estimator,
                project_id="project-boq",
                evidence_field_name="boq_line",
                limit=100,
            )
        project.current_document_set_revision_id = "document-set-boq"
        authoring = service.authoring_context(
            actor=estimator,
            project_id="project-boq",
            evidence_field_name="boq_line",
            limit=1,
        )
        assert {
            candidate.observation.observation_id for candidate in authoring.evidence_candidates
        } == {"observation-line"}
        assert not authoring.candidates_truncated
        document_set = session.get(DocumentSetRevisionRow, "document-set-boq")
        assert document_set is not None
        document_set.manifest_hash = "0" * 64
        with pytest.raises(ValueError, match="manifest and four-eyes"):
            service.authoring_context(
                actor=estimator,
                project_id="project-boq",
                evidence_field_name="boq_line",
                limit=100,
            )
        document_set.manifest_hash = content_hash(document_set.revision_ids)
        document_set.confirmed_at = now - timedelta(seconds=1)
        with pytest.raises(ValueError, match="manifest and four-eyes"):
            service.authoring_context(
                actor=estimator,
                project_id="project-boq",
                evidence_field_name="boq_line",
                limit=100,
            )
        document_set.confirmed_at = now
        document_set.confirmed_by = "substituted-reviewer"
        with pytest.raises(ValueError, match="audit chain"):
            service.authoring_context(
                actor=estimator,
                project_id="project-boq",
                evidence_field_name="boq_line",
                limit=100,
            )
        document_set.confirmed_by = reviewer.actor_id
        with pytest.raises(ValueError, match="confirmed current document set"):
            service.create_line(
                actor=estimator,
                project_id="project-boq",
                draft=BoqLineDraft(
                    line_key="old-evidence-line",
                    wbs_node_id="wbs-pipeline",
                    work_code="PIPE_INSTALLATION",
                    description="Must not use evidence from an old set",
                    unit="m",
                    evidence_observation_ids=("observation-line-old-set",),
                    cost_components=(
                        CostComponentDraft(
                            semantic_key="old-pipe-material",
                            category=CostCategory.MATERIAL,
                            basis_kind=CostBasisKind.MARKET,
                        ),
                    ),
                ),
                request_id="request-line-old-evidence",
                reason="Prove that old-set evidence is rejected",
            )
        with pytest.raises(ValueError, match="reason must contain"):
            service.create_line(
                actor=estimator,
                project_id="project-boq",
                draft=BoqLineDraft(
                    line_key="blank-reason-line",
                    wbs_node_id="wbs-pipeline",
                    work_code="PIPE_INSTALLATION",
                    description="Must reject a blank authoring reason",
                    unit="m",
                    evidence_observation_ids=("observation-line",),
                    cost_components=(
                        CostComponentDraft(
                            semantic_key="blank-reason-material",
                            category=CostCategory.MATERIAL,
                            basis_kind=CostBasisKind.MARKET,
                        ),
                    ),
                ),
                request_id="request-line-blank-reason",
                reason=" ",
            )
        line = service.create_line(
            actor=estimator,
            project_id="project-boq",
            draft=BoqLineDraft(
                line_key="pipeline-installation-main",
                wbs_node_id="wbs-pipeline",
                work_code="PIPE_INSTALLATION",
                description="Install pipeline",
                unit="m",
                evidence_observation_ids=("observation-line",),
                cost_components=(
                    CostComponentDraft(
                        semantic_key="pipe-material",
                        category=CostCategory.MATERIAL,
                        basis_kind=CostBasisKind.MARKET,
                    ),
                ),
            ),
            request_id="request-line",
            reason="Build BoQ from verified extraction",
        )
        assert line.status is VerificationStatus.IN_REVIEW
        verified = service.verify_line(
            actor=reviewer,
            project_id="project-boq",
            line_id=line.line_id,
            expected_line_updated_at=line.updated_at,
            request_id="request-verify",
            reason="Technical review",
        )
        assert verified.status is VerificationStatus.VERIFIED

        formula = QuantityFormulaDefinition(
            formula_id="pipe-length",
            formula_version="quantity-formulas-v1",
            operation=QuantityOperation.PRODUCT,
            inputs={"length": Decimal("100")},
            output_unit="m",
            display_formula="length",
        )
        invalid = service.record_quantity(
            actor=estimator,
            project_id="project-boq",
            line_id=line.line_id,
            submission=QuantitySubmission(
                draft=QuantityDraft(
                    value=Decimal("99"),
                    unit="m",
                    source_observation_ids=("observation-length",),
                    source_priority=1,
                    rounding_scale=2,
                    waste_factor=Decimal("0"),
                ),
                formula=formula,
                formula_input_observation_ids={"length": "observation-length"},
            ),
            request_id="request-quantity-invalid",
            reason="First extracted quantity",
        )
        assert not invalid.validation.passed

        quantity_change_context = service.quantity_change_context(
            actor=estimator,
            project_id="project-boq",
            line_id=line.line_id,
        )
        assert quantity_change_context.current_quantity_id == invalid.quantity.quantity_id
        assert quantity_change_context.current_quantity_status is VerificationStatus.CONFLICT
        assert quantity_change_context.quantity_formula_rules_version_id == ("quantity-formulas-v1")
        assert quantity_change_context.critical is True
        assert quantity_change_context.approval_role is ActorRole.REVIEWER

        corrected_submission = QuantitySubmission(
            draft=QuantityDraft(
                value=Decimal("100"),
                unit="m",
                source_observation_ids=("observation-length",),
                source_priority=1,
                rounding_scale=2,
                waste_factor=Decimal("0"),
            ),
            formula=formula,
            formula_input_observation_ids={"length": "observation-length"},
        )
        with pytest.raises(ValueError, match="registered manual change"):
            service.record_quantity(
                actor=estimator,
                project_id="project-boq",
                line_id=line.line_id,
                submission=corrected_submission,
                request_id="request-unregistered-correction",
                reason="An unregistered correction must fail closed",
            )
        proposal = service.propose_quantity_manual_change(
            actor=estimator,
            project_id="project-boq",
            line_id=line.line_id,
            submission=corrected_submission,
            request_id="request-propose-quantity-change",
            reason="Correct after independent formula check",
        )
        assert proposal.status == "PENDING_APPROVAL"
        assert proposal.critical is True
        assert proposal.approval_task_id is not None
        assert proposal.approval_task_updated_at is not None
        retried_proposal = service.propose_quantity_manual_change(
            actor=estimator,
            project_id="project-boq",
            line_id=line.line_id,
            submission=corrected_submission,
            request_id="request-retry-quantity-change",
            reason="Correct after independent formula check",
        )
        assert retried_proposal.change_id == proposal.change_id
        pending_submission = QuantitySubmission(
            draft=corrected_submission.draft.model_copy(
                update={"manual_change_id": proposal.change_id}
            ),
            formula=corrected_submission.formula,
            formula_input_observation_ids=(corrected_submission.formula_input_observation_ids),
        )
        with pytest.raises(
            ValueError,
            match="Critical quantity change approval task is incomplete",
        ):
            service.record_quantity(
                actor=estimator,
                project_id="project-boq",
                line_id=line.line_id,
                submission=pending_submission,
                request_id="request-apply-unapproved-change",
                reason="An unapproved critical change must fail closed",
            )
        project = session.get(ProjectRow, "project-boq")
        assert project is not None
        project.current_document_set_revision_id = "superseding-document-set"
        with pytest.raises(
            ValueError,
            match="no longer matches current controlled context",
        ):
            service.record_quantity(
                actor=estimator,
                project_id="project-boq",
                line_id=line.line_id,
                submission=pending_submission,
                request_id="request-apply-stale-change",
                reason="A proposal for a superseded document set must fail closed",
            )
        project.current_document_set_revision_id = "document-set-boq"
        ApprovalService(
            session=session,
            settings=settings,
            object_store=store,
        ).decide(
            actor=reviewer,
            project_id="project-boq",
            task_id=proposal.approval_task_id,
            command=ApprovalDecisionCommand(
                decision=ApprovalDecision.APPROVED,
                reason="Checked the formula and exact source observation",
                expected_task_updated_at=proposal.approval_task_updated_at,
                evidence_ids=("observation-length",),
            ),
            request_id="request-approve-quantity-change",
        )
        approved = service.quantity_manual_change_review(
            actor=reviewer,
            project_id="project-boq",
            change_id=proposal.change_id,
        )
        assert approved.status == "APPROVED"
        corrected = service.apply_quantity_manual_change(
            actor=estimator,
            project_id="project-boq",
            change_id=proposal.change_id,
            request_id="request-quantity-corrected",
            reason="Correct after independent formula check",
        )
        assert corrected.validation.passed
        assert corrected.supersedes_quantity_id == invalid.quantity.quantity_id
        applied = service.quantity_manual_change_review(
            actor=reviewer,
            project_id="project-boq",
            change_id=proposal.change_id,
        )
        assert applied.status == "APPLIED"
        assert applied.applied_quantity_id == corrected.quantity.quantity_id
        assert (
            session.scalar(
                select(QuantityManualChangeApplicationRow).where(
                    QuantityManualChangeApplicationRow.manual_change_id == proposal.change_id
                )
            )
            is not None
        )
        with pytest.raises(ValueError, match="already been applied"):
            service.record_quantity(
                actor=estimator,
                project_id="project-boq",
                line_id=line.line_id,
                submission=pending_submission,
                request_id="request-reuse-applied-change",
                reason="A consumed change cannot be reused",
            )
        current_rows = list(
            session.query(QuantityRow)
            .filter(QuantityRow.boq_line_id == line.line_id)
            .order_by(QuantityRow.created_at)
        )
        assert [row.is_current for row in current_rows] == [False, True]

        project = session.get(ProjectRow, "project-boq")
        assert project is not None
        project.state = ApprovalState.BOQ_REVIEW.value
        with pytest.raises(ValueError, match="WBS node must be normalized"):
            service.run_scope_completeness(
                actor=reviewer,
                project_id="project-boq",
                wbs_node_id=" wbs-pipeline ",
                request_id="request-scope-non-normalized-wbs",
                reason="Reject an ambiguous WBS identity",
            )
        with pytest.raises(ValueError, match="reason must contain"):
            service.run_scope_completeness(
                actor=reviewer,
                project_id="project-boq",
                wbs_node_id="wbs-pipeline",
                request_id="request-scope-blank-reason",
                reason=" ",
            )
        with pytest.raises(ValueError, match="at least one current verified BoQ line"):
            service.run_scope_completeness(
                actor=reviewer,
                project_id="project-boq",
                wbs_node_id="wbs-does-not-exist",
                request_id="request-scope-unknown-wbs",
                reason="Reject an unknown WBS instead of reporting a false pass",
            )
        scope_result = service.run_scope_completeness(
            actor=reviewer,
            project_id="project-boq",
            wbs_node_id="wbs-pipeline",
            request_id="request-scope",
            reason="Run approved scope rule pack",
        )
        assert scope_result.evaluation is not None
        missing = {finding.required_work_code for finding in scope_result.evaluation.findings}
        assert missing == {"TESTING", "AS_BUILT_DOCUMENTATION"}

        release_context = ProjectService(
            session=session,
            settings=settings,
            object_store=store,
        ).evaluate_release(actor=reviewer, project_id="project-boq")
        manual_change_release = next(
            record
            for record in release_context.critical_manual_changes
            if record.change_id == proposal.change_id
        )
        assert manual_change_release.changed_by == estimator.actor_id
        assert manual_change_release.approved_by == reviewer.actor_id
        application = session.scalar(
            select(QuantityManualChangeApplicationRow).where(
                QuantityManualChangeApplicationRow.manual_change_id == proposal.change_id
            )
        )
        assert application is not None
        application.payload = {
            **application.payload,
            "approval_id": "unrelated-approval",
        }
        integrity_view = service.quantity_manual_change_review(
            actor=reviewer,
            project_id="project-boq",
            change_id=proposal.change_id,
        )
        assert integrity_view.status == "BLOCKED_INTEGRITY"
        tampered_release_context = ProjectService(
            session=session,
            settings=settings,
            object_store=store,
        ).evaluate_release(actor=reviewer, project_id="project-boq")
        tampered_manual_change = next(
            record
            for record in tampered_release_context.critical_manual_changes
            if record.change_id == proposal.change_id
        )
        assert tampered_manual_change.approved_by is None
        assert tampered_manual_change.approval_id is None
        assert set(release_context.blocking_contour_finding_ids) >= {
            finding.finding_id for finding in scope_result.evaluation.findings
        }

    engine.dispose()
