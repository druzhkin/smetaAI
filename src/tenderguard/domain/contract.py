from __future__ import annotations

from decimal import Decimal

from pydantic import Field

from tenderguard.domain.enums import ContractTermKind, FindingCode, Severity
from tenderguard.domain.models import DomainModel, ValidationFinding


class ContractTerm(DomainModel):
    term_id: str
    kind: ContractTermKind
    value: str
    observation_ids: tuple[str, ...] = Field(min_length=1)
    verified: bool
    cost_impact_resolved: bool
    cost_impact_amount: Decimal | None = None
    cost_impact_currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    cost_input_id: str | None = None
    approved_assumption_id: str | None = None
    derived_cost_model_id: str | None = None


class ContractAssessment(DomainModel):
    assessment_version: str
    terms: tuple[ContractTerm, ...]
    required_term_kinds: frozenset[ContractTermKind]


def validate_contract(assessment: ContractAssessment) -> tuple[ValidationFinding, ...]:
    findings: list[ValidationFinding] = []
    by_kind = {term.kind: term for term in assessment.terms}
    for kind in sorted(assessment.required_term_kinds, key=lambda item: item.value):
        term = by_kind.get(kind)
        if term is None or not term.verified:
            findings.append(
                ValidationFinding(
                    code=FindingCode.CONTRACT_TERM_MISSING,
                    severity=Severity.BLOCKER,
                    message=f"Contract term is missing or unverified: {kind.value}",
                    entity_ids=(kind.value,),
                )
            )
            continue
        if not term.cost_impact_resolved:
            findings.append(
                ValidationFinding(
                    code=FindingCode.CONTRACT_COST_IMPACT_UNRESOLVED,
                    severity=Severity.BLOCKER,
                    message=f"Contract term cost impact is unresolved: {kind.value}",
                    entity_ids=(term.term_id,),
                )
            )
        if term.cost_impact_amount is not None and not (
            term.cost_input_id or term.approved_assumption_id or term.derived_cost_model_id
        ):
            findings.append(
                ValidationFinding(
                    code=FindingCode.COST_WITHOUT_BASIS,
                    severity=Severity.BLOCKER,
                    message=f"Contract cost impact lacks a calculation input: {kind.value}",
                    entity_ids=(term.term_id,),
                )
            )
    return tuple(findings)
