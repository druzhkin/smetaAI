import pytest

from tenderguard.application.passport import (
    PassportFactDraft,
    PassportRequirementsPolicy,
)
from tenderguard.domain.enums import VerificationStatus
from tenderguard.domain.passport import PassportFact, ProjectPassport, validate_passport


def test_critical_passport_facts_require_independent_verified_observations() -> None:
    passport = ProjectPassport(
        project_id="project-1",
        passport_version="passport-v1",
        facts=(
            PassportFact(
                field_name="object_address",
                value="Moscow",
                observation_ids=("tender-terms:page-1",),
                status=VerificationStatus.VERIFIED,
            ),
            PassportFact(
                field_name="project_code",
                value="P-1",
                observation_ids=("tender-terms:page-1", "drawings:title-block"),
                status=VerificationStatus.VERIFIED,
            ),
        ),
    )
    findings = validate_passport(
        passport,
        required_fields=frozenset({"object_address", "project_code", "completion_date"}),
        independently_verified_fields=frozenset({"object_address", "project_code"}),
    )
    messages = {finding.message for finding in findings}
    assert any("lacks two independent observations: object_address" in item for item in messages)
    assert any("not verified: completion_date" in item for item in messages)
    assert not any("project_code" in item for item in messages)


def test_passport_commands_reject_ambiguous_policy_and_duplicate_evidence() -> None:
    with pytest.raises(ValueError, match="must not overlap"):
        PassportRequirementsPolicy(
            required_fields=frozenset({"object_address"}),
            optional_fields=frozenset({"object_address"}),
        )
    with pytest.raises(ValueError, match="must be unique"):
        PassportFactDraft(
            field_name="object_address",
            value="Moscow",
            observation_ids=("observation-1", "observation-1"),
        )
