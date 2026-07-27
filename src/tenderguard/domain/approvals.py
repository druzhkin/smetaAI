from __future__ import annotations

from decimal import Decimal

from pydantic import Field

from tenderguard.domain.enums import (
    ActorRole,
    ApprovalReason,
    FindingCode,
    Severity,
)
from tenderguard.domain.models import DomainModel, ValidationFinding

DEDICATED_APPROVAL_TASK_TYPES = frozenset(
    {
        "CONFLICT_RESOLUTION",
        "MANUAL_EVIDENCE_REVIEW",
        "PASSPORT_FACT_REVIEW",
        "CONTRACT_TERM_REVIEW",
        "RISK_ITEM_REVIEW",
    }
)


class ApprovalSubject(DomainModel):
    entity_type: str
    entity_id: str
    reasons: frozenset[ApprovalReason]
    monetary_value: Decimal | None = Field(default=None, ge=0)
    project_total: Decimal | None = Field(default=None, ge=0)
    price_spread: Decimal | None = Field(default=None, ge=0)
    reserve_share: Decimal | None = Field(default=None, ge=0)
    profit_impact_share: Decimal | None = Field(default=None, ge=0)


class ApprovalRule(DomainModel):
    reason: ApprovalReason
    assigned_role: ActorRole
    threshold: Decimal | None = Field(default=None, ge=0)
    threshold_kind: str | None = None
    required: bool = True


class ApprovalPolicyDefinition(DomainModel):
    policy_version: str
    rules: tuple[ApprovalRule, ...]


class ApprovalTaskSpec(DomainModel):
    task_key: str
    entity_type: str
    entity_id: str
    reason: ApprovalReason
    assigned_role: ActorRole
    required: bool


class ApprovalPlan(DomainModel):
    tasks: tuple[ApprovalTaskSpec, ...]
    findings: tuple[ValidationFinding, ...]


_THRESHOLD_VALUE: dict[ApprovalReason, str] = {
    ApprovalReason.HIGH_VALUE: "monetary_value",
    ApprovalReason.HIGH_PRICE_SPREAD: "price_spread",
    ApprovalReason.LARGE_RESERVE: "reserve_share",
    ApprovalReason.MATERIAL_PROFIT_IMPACT: "profit_impact_share",
}


def build_approval_plan(
    subjects: tuple[ApprovalSubject, ...],
    policy: ApprovalPolicyDefinition,
) -> ApprovalPlan:
    rules = {rule.reason: rule for rule in policy.rules}
    tasks: list[ApprovalTaskSpec] = []
    findings: list[ValidationFinding] = []
    for subject in subjects:
        for reason in sorted(subject.reasons, key=lambda item: item.value):
            rule = rules.get(reason)
            if rule is None:
                findings.append(
                    ValidationFinding(
                        code=FindingCode.APPROVAL_THRESHOLD_UNCONFIGURED,
                        severity=Severity.BLOCKER,
                        message=f"Approval rule is not configured: {reason.value}",
                        entity_ids=(subject.entity_id,),
                    )
                )
                continue
            threshold_attribute = _THRESHOLD_VALUE.get(reason)
            if threshold_attribute:
                value = getattr(subject, threshold_attribute)
                if rule.threshold is None or value is None:
                    findings.append(
                        ValidationFinding(
                            code=FindingCode.APPROVAL_THRESHOLD_UNCONFIGURED,
                            severity=Severity.BLOCKER,
                            message=(
                                f"Methodology-owned threshold/value is missing: {reason.value}"
                            ),
                            entity_ids=(subject.entity_id,),
                        )
                    )
                    continue
                if value < rule.threshold:
                    continue
            tasks.append(
                ApprovalTaskSpec(
                    task_key=(
                        f"{policy.policy_version}:{subject.entity_type}:"
                        f"{subject.entity_id}:{reason.value}"
                    ),
                    entity_type=subject.entity_type,
                    entity_id=subject.entity_id,
                    reason=reason,
                    assigned_role=rule.assigned_role,
                    required=rule.required,
                )
            )
    return ApprovalPlan(tasks=tuple(tasks), findings=tuple(findings))
