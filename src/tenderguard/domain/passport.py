from __future__ import annotations

from typing import Any

from pydantic import Field

from tenderguard.domain.enums import FindingCode, Severity, VerificationStatus
from tenderguard.domain.models import DomainModel, ValidationFinding


class PassportFact(DomainModel):
    field_name: str
    value: Any
    unit: str | None = None
    observation_ids: tuple[str, ...] = Field(min_length=1)
    status: VerificationStatus


class ProjectPassport(DomainModel):
    project_id: str
    facts: tuple[PassportFact, ...]
    passport_version: str


def validate_passport(
    passport: ProjectPassport,
    *,
    required_fields: frozenset[str],
    independently_verified_fields: frozenset[str],
) -> tuple[ValidationFinding, ...]:
    facts = {fact.field_name: fact for fact in passport.facts}
    findings: list[ValidationFinding] = []
    for field_name in sorted(required_fields):
        fact = facts.get(field_name)
        if fact is None or fact.status is not VerificationStatus.VERIFIED:
            findings.append(
                ValidationFinding(
                    code=FindingCode.PROJECT_PASSPORT_INCOMPLETE,
                    severity=Severity.BLOCKER,
                    message=f"Required project passport field is not verified: {field_name}",
                    entity_ids=(field_name,),
                )
            )
            continue
        if field_name in independently_verified_fields and len(fact.observation_ids) < 2:
            findings.append(
                ValidationFinding(
                    code=FindingCode.PROJECT_PASSPORT_INCOMPLETE,
                    severity=Severity.BLOCKER,
                    message=(
                        "Critical project passport field lacks two independent "
                        f"observations: {field_name}"
                    ),
                    entity_ids=(field_name,),
                )
            )
    return tuple(findings)
