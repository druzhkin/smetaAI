from datetime import UTC, datetime
from decimal import Decimal

from tenderguard.application.projects import ProjectService
from tenderguard.domain.enums import ApprovalState, VersionStatus
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
    incomplete = {
        "all_gates_complete": True,
        "gates": {
            "historical_projects": {
                "status": "PASSED",
                "evidence_hash": "a" * 64,
                "owner_id": "owner-1",
                "approved_by": "approver-1",
                "approved_at": NOW.isoformat(),
                "environment": "qualification",
            }
        },
    }
    assert not ProjectService._production_qualification_evidence_complete(incomplete)

    gate = incomplete["gates"]["historical_projects"]
    complete = {
        "all_gates_complete": True,
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
    assert ProjectService._production_qualification_evidence_complete(complete)
