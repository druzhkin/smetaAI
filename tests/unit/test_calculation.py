from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tenderguard.domain.calculation import (
    AppliedFactor,
    AtomicCostInput,
    CalculationPolicy,
    calculate_primary,
    create_snapshot,
    validate_independently,
)
from tenderguard.domain.enums import CostCategory, VersionStatus
from tenderguard.domain.models import ControlledVersion

NOW = datetime(2026, 7, 23, tzinfo=UTC)


def input_line(
    identifier: str,
    semantic_key: str,
    *,
    quantity: str = "3",
    rate: str = "10.005",
    basis: bool = True,
) -> AtomicCostInput:
    return AtomicCostInput(
        cost_input_id=identifier,
        line_id=f"line-{identifier}",
        wbs_node_id="wbs-1",
        semantic_key=semantic_key,
        category=CostCategory.MATERIAL,
        quantity=Decimal(quantity),
        unit="m",
        unit_rate=Decimal(rate),
        currency="RUB",
        factors=(
            AppliedFactor(
                factor_id="waste",
                version_id="factor-v1",
                value=Decimal("1.02"),
                evidence_or_rule_id="methodology-v1",
            ),
        ),
        source_observation_id="price-observation-1" if basis else None,
    )


def policy() -> CalculationPolicy:
    return CalculationPolicy(
        policy_version="calculation-policy-v1",
        currency="RUB",
        line_rounding_scale=2,
        total_rounding_scale=2,
        rounding_mode="ROUND_HALF_UP",
        independent_tolerance=Decimal("0.00"),
        expected_semantic_keys=frozenset({"pipe"}),
    )


def test_primary_and_independent_paths_agree() -> None:
    inputs = (input_line("1", "pipe"),)
    primary = calculate_primary(inputs, policy(), engine_version="primary-v1", calculated_at=NOW)
    validation = validate_independently(
        inputs,
        primary,
        policy(),
        validator_version="independent-v1",
        validated_at=NOW,
    )
    assert primary.grand_total == Decimal("30.62")
    assert validation.passed
    assert validation.difference == Decimal("0.00")


def test_tampered_primary_total_is_blocked() -> None:
    inputs = (input_line("1", "pipe"),)
    primary = calculate_primary(inputs, policy(), engine_version="primary-v1", calculated_at=NOW)
    tampered = primary.model_copy(update={"grand_total": Decimal("29.99")})
    validation = validate_independently(
        inputs,
        tampered,
        policy(),
        validator_version="independent-v1",
        validated_at=NOW,
    )
    assert not validation.passed
    assert validation.difference == Decimal("0.63")


def test_double_counting_and_missing_basis_are_blockers() -> None:
    inputs = (
        input_line("1", "pipe", basis=False),
        input_line("2", "pipe"),
    )
    primary = calculate_primary(inputs, policy(), engine_version="primary-v1", calculated_at=NOW)
    validation = validate_independently(
        inputs,
        primary,
        policy(),
        validator_version="independent-v1",
        validated_at=NOW,
    )
    assert not validation.passed
    messages = {finding.message for finding in validation.findings}
    assert any("double counting" in message for message in messages)
    assert any("without source" in message for message in messages)


def test_calculation_rejects_totals_outside_persisted_decimal_range() -> None:
    inputs = (
        input_line(
            "overflow",
            "pipe",
            quantity="100000000000000000000",
            rate="10000000000",
        ),
    )
    with pytest.raises(ValueError, match="supported financial decimal range"):
        calculate_primary(
            inputs,
            policy(),
            engine_version="primary-v1",
            calculated_at=NOW,
        )


def test_snapshot_hash_is_independent_of_input_and_version_order() -> None:
    inputs = (input_line("2", "valve"), input_line("1", "pipe"))
    calculation_policy = policy().model_copy(
        update={"expected_semantic_keys": frozenset({"pipe", "valve"})}
    )
    primary = calculate_primary(
        inputs,
        calculation_policy,
        engine_version="primary-v1",
        calculated_at=NOW,
    )
    validation = validate_independently(
        inputs,
        primary,
        calculation_policy,
        validator_version="independent-v1",
        validated_at=NOW,
    )
    versions = (
        ControlledVersion(
            kind="scope_rules",
            version_id="scope-v1",
            content_hash="b" * 64,
            status=VersionStatus.APPROVED,
            approved_by="owner-2",
            approved_at=NOW,
        ),
        ControlledVersion(
            kind="calculation_model",
            version_id="calculation-policy-v1",
            content_hash="a" * 64,
            status=VersionStatus.APPROVED,
            approved_by="owner-1",
            approved_at=NOW,
        ),
    )
    first = create_snapshot(
        project_id="project-1",
        document_set_revision_id="document-set-1",
        inputs=inputs,
        policy=calculation_policy,
        controlled_versions=versions,
        primary=primary,
        independent=validation,
        created_by="estimator-1",
        created_at=NOW,
    )
    second = create_snapshot(
        project_id="project-1",
        document_set_revision_id="document-set-1",
        inputs=tuple(reversed(inputs)),
        policy=calculation_policy,
        controlled_versions=tuple(reversed(versions)),
        primary=primary,
        independent=validation,
        created_by="estimator-1",
        created_at=NOW,
    )
    assert first.input_hash == second.input_hash
    assert first.snapshot_hash == second.snapshot_hash
