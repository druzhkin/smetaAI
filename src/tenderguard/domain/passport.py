from __future__ import annotations

from typing import Any

from pydantic import Field, field_validator, model_validator

from tenderguard.domain.enums import (
    ActorRole,
    FindingCode,
    Severity,
    VerificationStatus,
)
from tenderguard.domain.models import DomainModel, ValidationFinding


class PassportRequirementsPolicy(DomainModel):
    required_fields: frozenset[str] = Field(min_length=1)
    independently_verified_fields: frozenset[str] = frozenset()
    optional_fields: frozenset[str] = frozenset()
    review_role: ActorRole = ActorRole.REVIEWER

    @field_validator(
        "required_fields",
        "independently_verified_fields",
        "optional_fields",
    )
    @classmethod
    def fields_are_normalized(cls, values: frozenset[str]) -> frozenset[str]:
        if any(not value or value != value.strip() or len(value) > 200 for value in values):
            raise ValueError("Passport field names must be normalized and bounded")
        return values

    @model_validator(mode="after")
    def policy_is_closed(self) -> PassportRequirementsPolicy:
        declared = self.required_fields | self.optional_fields
        if not self.independently_verified_fields.issubset(declared):
            raise ValueError(
                "Independently verified passport fields must be declared as required or optional"
            )
        if self.required_fields.intersection(self.optional_fields):
            raise ValueError("Passport required and optional fields must not overlap")
        if self.review_role not in {
            ActorRole.REVIEWER,
            ActorRole.TECHNICAL_EXPERT,
        }:
            raise ValueError("Passport review role must be REVIEWER or TECHNICAL_EXPERT")
        return self

    @property
    def declared_fields(self) -> frozenset[str]:
        return self.required_fields | self.optional_fields


class PassportFact(DomainModel):
    field_name: str
    value: Any
    unit: str | None = None
    observation_ids: tuple[str, ...] = Field(min_length=1)
    independence_source_ids: tuple[str, ...] = ()
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
        independence_sources = fact.independence_source_ids or fact.observation_ids
        if field_name in independently_verified_fields and len(independence_sources) < 2:
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
