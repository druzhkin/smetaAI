from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from tenderguard.application.boq import (
    BoqLineDraft,
    BoqService,
    CostComponentDraft,
    QuantityDraft,
    QuantitySubmission,
)
from tenderguard.application.projects import ProjectService
from tenderguard.config import Settings
from tenderguard.domain.enums import (
    ActorRole,
    ApprovalState,
    CostBasisKind,
    CostCategory,
    EvidenceMethod,
    QuantityOperation,
    VerificationStatus,
)
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
    ObservationRow,
    ProjectControlledVersionRow,
    ProjectRow,
    QuantityRow,
)
from tests.integration.support import project_memberships


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
    now = datetime(2026, 7, 23, tzinfo=UTC)

    with factory.begin() as session:
        session.add(
            ProjectRow(
                id="project-boq",
                organization_id="org-1",
                code="BOQ-1",
                name="BoQ workflow",
                state=ApprovalState.BOQ_IN_PROGRESS.value,
                row_version=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.add_all(
            project_memberships(
                "project-boq",
                (estimator, reviewer),
                owner_id=estimator.actor_id,
                now=now,
            )
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
        session.add_all(versions)
        for version, purpose in zip(
            versions,
            ("quantity_policy", "quantity_formula_rules", "scope_rules"),
            strict=True,
        ):
            session.add(
                ProjectControlledVersionRow(
                    project_id="project-boq",
                    controlled_version_id=version.id,
                    purpose=purpose,
                    bound_by="methodology-owner",
                    bound_at=now,
                )
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
                    payload={
                        "observation": {
                            "value": {
                                "work_code": "PIPE_INSTALLATION",
                                "unit": "m",
                            }
                        }
                    },
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
                    payload={"observation": {"value": "100", "unit": "m"}},
                    created_at=now,
                ),
            )
        )

    with factory.begin() as session:
        service = BoqService(session=session, settings=settings, object_store=store)
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

        corrected = service.record_quantity(
            actor=estimator,
            project_id="project-boq",
            line_id=line.line_id,
            submission=QuantitySubmission(
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
            ),
            request_id="request-quantity-corrected",
            reason="Correct after independent formula check",
        )
        assert corrected.validation.passed
        assert corrected.supersedes_quantity_id == invalid.quantity.quantity_id
        current_rows = list(
            session.query(QuantityRow)
            .filter(QuantityRow.boq_line_id == line.line_id)
            .order_by(QuantityRow.created_at)
        )
        assert [row.is_current for row in current_rows] == [False, True]

        project = session.get(ProjectRow, "project-boq")
        assert project is not None
        project.state = ApprovalState.BOQ_REVIEW.value
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
        assert set(release_context.blocking_contour_finding_ids) >= {
            finding.finding_id for finding in scope_result.evaluation.findings
        }

    engine.dispose()
