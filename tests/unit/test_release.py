from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from tenderguard.application.projects import ProjectService
from tenderguard.config import Settings
from tenderguard.domain.access import project_role_mask
from tenderguard.domain.enums import (
    ActorRole,
    ApprovalState,
    ProjectAccessLevel,
    ProjectMembershipStatus,
    VersionStatus,
)
from tenderguard.domain.models import (
    CalculationSnapshot,
    ControlledVersion,
    IndependentValidationResult,
)
from tenderguard.domain.release import (
    FourEyesRecord,
    ReleaseContext,
    evaluate_bid_release,
    evaluate_internal_release,
)
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.database import (
    create_database_engine,
    create_schema_for_tests,
    create_session_factory,
)
from tenderguard.infrastructure.object_store import LocalObjectStore
from tenderguard.infrastructure.orm import (
    ProjectMembershipRow,
    ProjectRow,
    ReleaseDecisionRow,
)

NOW = datetime(2026, 7, 23, tzinfo=UTC)


def version(kind: str) -> ControlledVersion:
    return ControlledVersion(
        kind=kind,
        version_id=f"{kind}-v1",
        content_hash="a" * 64,
        status=VersionStatus.APPROVED,
        approved_by="methodology-owner",
        approved_at=NOW,
    )


def good_context() -> ReleaseContext:
    validation = IndependentValidationResult(
        validator_version="independent-v1",
        passed=True,
        independently_calculated_total=Decimal("100"),
        primary_total=Decimal("100"),
        difference=Decimal("0"),
        tolerance=Decimal("0.01"),
        findings=(),
        validated_at=NOW,
    )
    snapshot = CalculationSnapshot(
        snapshot_id="snapshot-1",
        project_id="project-1",
        document_set_revision_id="docs-v2",
        input_hash="b" * 64,
        output_hash="c" * 64,
        snapshot_hash="d" * 64,
        created_by="estimator-1",
        created_at=NOW,
        fixed=True,
    )
    kinds = (
        "methodology",
        "catalog",
        "calculation_model",
        "reconciliation_rules",
        "scope_rules",
        "quantity_policy",
        "quantity_formula_rules",
        "manual_change_policy",
        "price_policy",
        "approval_policy",
        "document_requirements",
        "risk_model",
        "contract_risk_rules",
        "logistics_model",
        "finance_model",
        "scenario_policy",
        "export_template",
    )
    return ReleaseContext(
        current_document_set_confirmed=True,
        current_document_set_revision_id="docs-v2",
        independent_validation=validation,
        unverified_cost_total=Decimal("0"),
        project_cost_total=Decimal("100"),
        max_unverified_cost_share=Decimal("0"),
        controlled_versions=tuple(version(kind) for kind in kinds),
        snapshot=snapshot,
        snapshot_integrity_valid=True,
        snapshot_controlled_versions_match=True,
        critical_manual_changes=(
            FourEyesRecord(
                change_id="change-1",
                changed_by="estimator-1",
                approved_by="approver-1",
                approval_id="approval-1",
            ),
        ),
        normative_engine_qualified=True,
        normative_calculation_valid=True,
        production_qualification_complete=True,
    )


def test_complete_evidence_can_pass_release_gate() -> None:
    decision = evaluate_bid_release(good_context())
    assert decision.allowed
    assert decision.resulting_state is ApprovalState.APPROVED_FOR_BID
    assert decision.findings == ()


def test_internal_release_is_a_formal_conservative_decision() -> None:
    decision = evaluate_internal_release(good_context())
    assert decision.allowed
    assert decision.requested_state is ApprovalState.APPROVED_FOR_INTERNAL_USE
    assert decision.resulting_state is ApprovalState.APPROVED_FOR_INTERNAL_USE


def test_release_command_rejects_a_stale_complete_gate_evaluation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    actor = Actor("approver-1", "org-1", frozenset({ActorRole.APPROVER}))
    project = ProjectRow(
        id="project-1",
        organization_id="org-1",
        code="REL-1",
        name="Release hash binding",
        state=ApprovalState.EXPERT_REVIEW.value,
        current_document_set_revision_id="docs-v2",
        row_version=1,
        created_at=NOW,
        updated_at=NOW,
    )
    context_holder = {"value": good_context()}

    def current_context(
        _service: ProjectService,
        _project: ProjectRow,
    ) -> ReleaseContext:
        return context_holder["value"]

    monkeypatch.setattr(ProjectService, "_build_release_context", current_context)

    with factory.begin() as session:
        session.add(project)
        session.add(
            ProjectMembershipRow(
                id="membership-release-approver",
                project_id=project.id,
                principal_id=actor.actor_id,
                roles=[ActorRole.APPROVER.value],
                role_mask=project_role_mask((ActorRole.APPROVER,)),
                access_level=ProjectAccessLevel.OWNER.value,
                status=ProjectMembershipStatus.ACTIVE.value,
                version=1,
                supersedes_membership_id=None,
                changed_by="test-fixture",
                reason="Explicit project release authority",
                created_at=NOW,
            )
        )
        session.flush()
        service = ProjectService(
            session=session,
            settings=settings,
            object_store=LocalObjectStore(tmp_path / "objects"),
        )
        (
            project_view,
            decision,
            gate_hash,
            _internal_decision,
            _internal_hash,
        ) = service.evaluate_release_gates(
            actor=actor,
            project_id=project.id,
        )
        assert project_view.row_version == 1
        assert decision.allowed

        context_holder["value"] = context_holder["value"].model_copy(
            update={"production_qualification_complete": False}
        )
        with pytest.raises(
            ValueError,
            match="Release gate changed",
        ):
            service.attempt_bid_release(
                actor=actor,
                project_id=project.id,
                expected_row_version=1,
                expected_gate_hash=gate_hash,
                request_id="request-stale-release",
                reason="Approve only the exact independently reviewed gate result",
            )
        assert session.scalars(select(ReleaseDecisionRow)).all() == []

    blocked = evaluate_internal_release(
        good_context().model_copy(update={"current_document_set_confirmed": False})
    )
    assert not blocked.allowed
    assert blocked.resulting_state is ApprovalState.BLOCKED


def test_missing_threshold_and_four_eyes_violation_force_blocked() -> None:
    context = good_context().model_copy(
        update={
            "max_unverified_cost_share": None,
            "critical_manual_changes": (
                FourEyesRecord(
                    change_id="change-1",
                    changed_by="same-user",
                    approved_by="same-user",
                    approval_id="approval-1",
                ),
            ),
        }
    )
    decision = evaluate_bid_release(context)
    assert not decision.allowed
    assert decision.resulting_state is ApprovalState.BLOCKED
    codes = {finding.code.value for finding in decision.findings}
    assert "UNVERIFIED_COST_THRESHOLD_UNCONFIGURED" in codes
    assert "FOUR_EYES_VIOLATION" in codes


def test_production_qualification_and_normative_engine_fail_closed() -> None:
    decision = evaluate_bid_release(
        good_context().model_copy(
            update={
                "normative_engine_qualified": False,
                "normative_calculation_valid": False,
                "production_qualification_complete": False,
            }
        )
    )
    assert not decision.allowed
    codes = {finding.code.value for finding in decision.findings}
    assert "NORMATIVE_ENGINE_UNAVAILABLE" in codes
    assert "NORMATIVE_CALCULATION_MISSING" in codes
    assert "PRODUCTION_QUALIFICATION_INCOMPLETE" in codes


def test_operational_integrity_failure_blocks_release() -> None:
    decision = evaluate_bid_release(
        good_context().model_copy(update={"operational_integrity_valid": False})
    )

    assert not decision.allowed
    assert {finding.code.value for finding in decision.findings} >= {
        "OPERATIONAL_INTEGRITY_UNAVAILABLE"
    }


def test_stale_or_unverified_snapshot_is_blocked() -> None:
    stale = evaluate_bid_release(
        good_context().model_copy(update={"current_document_set_revision_id": "docs-v3"})
    )
    assert not stale.allowed
    assert {finding.code.value for finding in stale.findings} >= {"CALCULATION_SNAPSHOT_STALE"}

    invalid = evaluate_bid_release(
        good_context().model_copy(update={"snapshot_integrity_valid": False})
    )
    assert not invalid.allowed
    assert {finding.code.value for finding in invalid.findings} >= {
        "CALCULATION_SNAPSHOT_INTEGRITY_FAILED"
    }


def test_production_qualification_requires_evidence_for_every_gate() -> None:
    campaign_id = "business-qualification-campaign-1"
    package_hash = "a" * 64
    incomplete = {
        "all_gates_complete": True,
        "business_qualification": {
            "campaign_id": campaign_id,
            "package_hash": package_hash,
            "approved_by": "business-qualification-approver",
            "approved_at": NOW.isoformat(),
            "environment": "qualification",
        },
        "gates": {
            "historical_projects": {
                "status": "PASSED",
                "evidence_hash": package_hash,
                "owner_id": "owner-1",
                "approved_by": "approver-1",
                "approved_at": NOW.isoformat(),
                "environment": "qualification",
                "source_reference": (f"business_qualification_campaign:{campaign_id}"),
            }
        },
    }
    assert not ProjectService._production_qualification_evidence_complete(incomplete)

    gate = incomplete["gates"]["historical_projects"]
    complete = {
        "all_gates_complete": True,
        "business_qualification": incomplete["business_qualification"],
        "gates": {
            name: dict(gate)
            for name in (
                "historical_projects",
                "blind_estimator_comparison",
                "parallel_operation",
                "variance_resolution",
                "rules_and_catalog_calibration",
                "damaged_conflicting_document_resilience",
                "load_test",
                "security_review",
                "backup_restore",
                "methodology_approval",
            )
        },
    }
    for gate_name in (
        "historical_projects",
        "blind_estimator_comparison",
        "parallel_operation",
        "variance_resolution",
    ):
        complete["gates"][gate_name]["evidence_hash"] = package_hash
        complete["gates"][gate_name]["source_reference"] = (
            f"business_qualification_campaign:{campaign_id}"
        )
    for gate_name in (
        "rules_and_catalog_calibration",
        "damaged_conflicting_document_resilience",
        "load_test",
        "security_review",
        "backup_restore",
        "methodology_approval",
    ):
        package_id = f"qualification-evidence-{gate_name}"
        complete["gates"][gate_name]["evidence_package_id"] = package_id
        complete["gates"][gate_name]["source_reference"] = (
            f"production_gate_evidence_package:{package_id}"
        )
    assert ProjectService._production_qualification_evidence_complete(complete)
