from __future__ import annotations

from decimal import Decimal

from pydantic import Field

from tenderguard.domain.enums import ApprovalState, FindingCode, Severity, VersionStatus
from tenderguard.domain.models import (
    CalculationSnapshot,
    ControlledVersion,
    DomainModel,
    GateDecision,
    IndependentValidationResult,
    ValidationFinding,
)


class FourEyesRecord(DomainModel):
    change_id: str
    changed_by: str
    approved_by: str | None
    approval_id: str | None


class ReleaseContext(DomainModel):
    current_document_set_confirmed: bool
    current_document_set_revision_id: str | None = None
    missing_critical_document_ids: tuple[str, ...] = ()
    unverified_key_quantity_ids: tuple[str, ...] = ()
    unresolved_conflict_ids: tuple[str, ...] = ()
    cost_item_ids_without_basis: tuple[str, ...] = ()
    unverified_analogue_ids: tuple[str, ...] = ()
    price_normalization_violation_ids: tuple[str, ...] = ()
    independent_validation: IndependentValidationResult | None = None
    unverified_cost_total: Decimal = Field(ge=0)
    project_cost_total: Decimal = Field(ge=0)
    max_unverified_cost_share: Decimal | None = Field(default=None, ge=0, le=1)
    outstanding_approval_ids: tuple[str, ...] = ()
    controlled_versions: tuple[ControlledVersion, ...] = ()
    invalid_controlled_version_ids: tuple[str, ...] = ()
    required_controlled_version_kinds: frozenset[str] = frozenset(
        {
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
        }
    )
    snapshot: CalculationSnapshot | None = None
    snapshot_integrity_valid: bool = False
    snapshot_controlled_versions_match: bool = False
    critical_manual_changes: tuple[FourEyesRecord, ...] = ()
    unresolved_contract_risk_ids: tuple[str, ...] = ()
    blocking_contour_finding_ids: tuple[str, ...] = ()
    normative_calculation_required: bool = True
    normative_engine_qualified: bool = False
    normative_calculation_valid: bool = False
    production_qualification_complete: bool = False
    operational_integrity_valid: bool = True


def _finding(
    code: FindingCode,
    message: str,
    ids: tuple[str, ...] = (),
    details: dict[str, object] | None = None,
) -> ValidationFinding:
    return ValidationFinding(
        code=code,
        severity=Severity.BLOCKER,
        message=message,
        entity_ids=ids,
        details=details or {},
    )


def evaluate_bid_release(context: ReleaseContext) -> GateDecision:
    findings: list[ValidationFinding] = []

    if not context.current_document_set_confirmed:
        findings.append(
            _finding(
                FindingCode.CURRENT_DOCUMENT_SET_NOT_CONFIRMED,
                "Current tender-document set and revision are not confirmed",
            )
        )
    checks: tuple[tuple[tuple[str, ...], FindingCode, str], ...] = (
        (
            context.missing_critical_document_ids,
            FindingCode.CRITICAL_DOCUMENT_MISSING,
            "Critical documents are missing",
        ),
        (
            context.unverified_key_quantity_ids,
            FindingCode.KEY_QUANTITY_UNVERIFIED,
            "Key quantities are not verified",
        ),
        (
            context.unresolved_conflict_ids,
            FindingCode.UNRESOLVED_CONFLICT,
            "Unresolved evidence or documentation conflicts exist",
        ),
        (
            context.cost_item_ids_without_basis,
            FindingCode.COST_WITHOUT_BASIS,
            "Cost items lack a source or explicit approved assumption",
        ),
        (
            context.unverified_analogue_ids,
            FindingCode.TECHNICAL_ANALOGUE_UNVERIFIED,
            "Technical analogues have not been verified",
        ),
        (
            context.price_normalization_violation_ids,
            FindingCode.PRICE_NORMALIZATION_FAILED,
            "Price normalization rules are violated",
        ),
        (
            context.outstanding_approval_ids,
            FindingCode.REQUIRED_APPROVAL_MISSING,
            "Mandatory expert approvals are incomplete",
        ),
        (
            context.unresolved_contract_risk_ids,
            FindingCode.CONTRACT_RISK_UNRESOLVED,
            "Contract risks that affect execution cost remain unresolved",
        ),
        (
            context.blocking_contour_finding_ids,
            FindingCode.BLOCKING_CONTOUR_FINDING,
            "One or more verification contours have blocking findings",
        ),
    )
    for identifiers, code, message in checks:
        if identifiers:
            findings.append(_finding(code, message, identifiers))

    if context.independent_validation is None:
        findings.append(
            _finding(
                FindingCode.INDEPENDENT_VALIDATION_MISSING,
                "Independent recalculation has not been completed",
            )
        )
    elif not context.independent_validation.passed:
        findings.append(
            _finding(
                FindingCode.INDEPENDENT_RECALCULATION_MISMATCH,
                "Independent recalculation did not pass",
            )
        )

    if context.max_unverified_cost_share is None:
        findings.append(
            _finding(
                FindingCode.UNVERIFIED_COST_THRESHOLD_UNCONFIGURED,
                "Methodology owner has not configured maximum unverified cost share",
            )
        )
    elif context.project_cost_total == 0:
        if context.unverified_cost_total > 0:
            findings.append(
                _finding(
                    FindingCode.UNVERIFIED_COST_SHARE_EXCEEDED,
                    "Unverified cost exists while project cost total is zero",
                )
            )
    else:
        share = context.unverified_cost_total / context.project_cost_total
        if share > context.max_unverified_cost_share:
            findings.append(
                _finding(
                    FindingCode.UNVERIFIED_COST_SHARE_EXCEEDED,
                    "Unverified cost share exceeds the methodology-owned threshold",
                    details={
                        "actual_share": share,
                        "allowed_share": context.max_unverified_cost_share,
                    },
                )
            )

    versions_by_kind = {version.kind: version for version in context.controlled_versions}
    missing_versions = tuple(
        sorted(context.required_controlled_version_kinds - versions_by_kind.keys())
    )
    if missing_versions:
        findings.append(
            _finding(
                FindingCode.CONTROLLED_VERSION_MISSING,
                "Required controlled versions are missing",
                missing_versions,
            )
        )
    unapproved_versions = tuple(
        sorted(
            version.version_id
            for version in context.controlled_versions
            if version.kind in context.required_controlled_version_kinds
            and version.status is not VersionStatus.APPROVED
        )
    )
    if unapproved_versions:
        findings.append(
            _finding(
                FindingCode.CONTROLLED_VERSION_NOT_APPROVED,
                "Required models, catalogs, rules, or methodology are not approved",
                unapproved_versions,
            )
        )
    if context.invalid_controlled_version_ids:
        findings.append(
            _finding(
                FindingCode.CONTROLLED_VERSION_INTEGRITY_FAILED,
                "One or more bound governed versions fail content, lifecycle, "
                "four-eyes, or signed audit verification",
                context.invalid_controlled_version_ids,
            )
        )

    if context.snapshot is None or not context.snapshot.fixed:
        findings.append(
            _finding(
                FindingCode.CALCULATION_SNAPSHOT_MISSING,
                "A fixed calculation snapshot is required",
            )
        )
    elif (
        context.snapshot.document_set_revision_id != context.current_document_set_revision_id
        or not context.snapshot_controlled_versions_match
    ):
        findings.append(
            _finding(
                FindingCode.CALCULATION_SNAPSHOT_STALE,
                "The fixed calculation snapshot does not match the current document set "
                "or controlled versions",
                (context.snapshot.snapshot_id,),
            )
        )
    elif not context.snapshot_integrity_valid:
        findings.append(
            _finding(
                FindingCode.CALCULATION_SNAPSHOT_INTEGRITY_FAILED,
                "The fixed calculation snapshot or its content-addressed object "
                "failed verification",
                (context.snapshot.snapshot_id,),
            )
        )

    incomplete_changes = tuple(
        record.change_id
        for record in context.critical_manual_changes
        if not (record.approved_by and record.approval_id)
    )
    if incomplete_changes:
        findings.append(
            _finding(
                FindingCode.CRITICAL_MANUAL_CHANGE_NOT_APPROVED,
                "Critical manual changes lack approval",
                incomplete_changes,
            )
        )
    same_person_changes = tuple(
        record.change_id
        for record in context.critical_manual_changes
        if record.approved_by == record.changed_by
    )
    if same_person_changes:
        findings.append(
            _finding(
                FindingCode.FOUR_EYES_VIOLATION,
                "The same actor changed and approved a critical value",
                same_person_changes,
            )
        )

    if context.normative_calculation_required and not context.normative_engine_qualified:
        findings.append(
            _finding(
                FindingCode.NORMATIVE_ENGINE_UNAVAILABLE,
                "No qualified normative estimating engine or complete approved basis is available",
            )
        )
    if context.normative_calculation_required and not context.normative_calculation_valid:
        findings.append(
            _finding(
                FindingCode.NORMATIVE_CALCULATION_MISSING,
                "No validated normative calculation artifact is bound to this project",
            )
        )
    if not context.production_qualification_complete:
        findings.append(
            _finding(
                FindingCode.PRODUCTION_QUALIFICATION_INCOMPLETE,
                "Production quality gates and formal methodology approval are incomplete",
            )
        )
    if not context.operational_integrity_valid:
        findings.append(
            _finding(
                FindingCode.OPERATIONAL_INTEGRITY_UNAVAILABLE,
                "WORM evidence storage or external audit anchoring is not valid",
            )
        )

    allowed = not findings
    return GateDecision(
        requested_state=ApprovalState.APPROVED_FOR_BID,
        allowed=allowed,
        resulting_state=ApprovalState.APPROVED_FOR_BID if allowed else ApprovalState.BLOCKED,
        findings=tuple(findings),
    )


def evaluate_internal_release(context: ReleaseContext) -> GateDecision:
    bid_decision = evaluate_bid_release(context)
    return bid_decision.model_copy(
        update={
            "requested_state": ApprovalState.APPROVED_FOR_INTERNAL_USE,
            "resulting_state": (
                ApprovalState.APPROVED_FOR_INTERNAL_USE
                if bid_decision.allowed
                else ApprovalState.BLOCKED
            ),
        }
    )
