from decimal import Decimal

from tenderguard.domain.approvals import (
    ApprovalPolicyDefinition,
    ApprovalRule,
    ApprovalSubject,
    build_approval_plan,
)
from tenderguard.domain.enums import ActorRole, ApprovalReason


def test_financial_approval_threshold_is_not_invented() -> None:
    plan = build_approval_plan(
        (
            ApprovalSubject(
                entity_type="boq_line",
                entity_id="line-1",
                reasons=frozenset({ApprovalReason.HIGH_VALUE}),
                monetary_value=Decimal("1000000"),
                project_total=Decimal("5000000"),
            ),
        ),
        ApprovalPolicyDefinition(
            policy_version="approval-policy-draft",
            rules=(
                ApprovalRule(
                    reason=ApprovalReason.HIGH_VALUE,
                    assigned_role=ActorRole.APPROVER,
                    threshold=None,
                    threshold_kind="absolute_value",
                ),
            ),
        ),
    )
    assert not plan.tasks
    assert plan.findings[0].code.value == "APPROVAL_THRESHOLD_UNCONFIGURED"


def test_nonfinancial_trigger_creates_required_expert_task() -> None:
    plan = build_approval_plan(
        (
            ApprovalSubject(
                entity_type="nomenclature_match",
                entity_id="match-1",
                reasons=frozenset({ApprovalReason.TECHNICAL_ANALOGUE}),
            ),
        ),
        ApprovalPolicyDefinition(
            policy_version="approval-policy-v1",
            rules=(
                ApprovalRule(
                    reason=ApprovalReason.TECHNICAL_ANALOGUE,
                    assigned_role=ActorRole.TECHNICAL_EXPERT,
                ),
            ),
        ),
    )
    assert not plan.findings
    assert plan.tasks[0].assigned_role is ActorRole.TECHNICAL_EXPERT
