from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from tenderguard.domain.calculation import (
    AtomicCostInput,
    CalculationPolicy,
)
from tenderguard.domain.enums import CostCategory
from tenderguard.domain.normative import validate_normative_result
from tenderguard.domain.scenarios import (
    ScenarioDefinition,
    ScenarioOverride,
    calculate_scenario,
)
from tenderguard.integrations.contracts import (
    NormativeCalculationRequest,
    NormativeCalculationResult,
    NormativeEngineUnavailable,
    NormativeResourceComponent,
    UnavailableNormativeEstimatingEngine,
)


def test_normative_calculation_has_no_fake_fallback() -> None:
    request = NormativeCalculationRequest(
        request_id="normative-request-1",
        project_id="project-1",
        work_or_resource_code="GOVERNED-CODE",
        description="Pipe installation",
        quantity=Decimal("100"),
        unit="m",
        region="77",
        price_period=date(2026, 7, 1),
        normative_basis_version="approved-basis-v1",
        calculation_method="RESOURCE_INDEX",
        technology_attributes={"diameter": "DN100"},
        requested_coefficients={},
    )
    with pytest.raises(NormativeEngineUnavailable):
        UnavailableNormativeEstimatingEngine().calculate(request)


def test_each_scenario_is_independently_recalculated() -> None:
    base = (
        AtomicCostInput(
            cost_input_id="material-1",
            line_id="line-1",
            wbs_node_id="wbs-1",
            semantic_key="pipe",
            category=CostCategory.MATERIAL,
            quantity=Decimal("10"),
            unit="m",
            unit_rate=Decimal("100"),
            currency="RUB",
            source_observation_id="quote-1",
        ),
    )
    scenario = ScenarioDefinition(
        scenario_id="stress",
        scenario_version="1",
        name="Supplier repricing",
        overrides=(
            ScenarioOverride(
                cost_input_id="material-1",
                unit_rate=Decimal("120"),
                evidence_or_assumption_id="approved-assumption-1",
                reason="Stress case",
            ),
        ),
    )
    policy = CalculationPolicy(
        policy_version="calc-v1",
        currency="RUB",
        line_rounding_scale=2,
        total_rounding_scale=2,
        rounding_mode="ROUND_HALF_UP",
        independent_tolerance=Decimal("0"),
        expected_semantic_keys=frozenset({"pipe"}),
    )
    result = calculate_scenario(
        base,
        scenario,
        policy,
        calculated_at=datetime(2026, 7, 23, tzinfo=UTC),
    )
    assert result.primary.grand_total == Decimal("1200.00")
    assert result.independent.passed


def test_normative_result_must_reproduce_resources_and_request_basis() -> None:
    request = NormativeCalculationRequest(
        request_id="normative-request-2",
        project_id="project-1",
        work_or_resource_code="WORK-1",
        description="Controlled work",
        quantity=Decimal("2"),
        unit="m",
        region="77",
        price_period=date(2026, 7, 1),
        normative_basis_version="basis-v1",
        calculation_method="RESOURCE",
        technology_attributes={},
        requested_coefficients={"winter": Decimal("1.1")},
    )
    resource = NormativeResourceComponent(
        resource_code="LABOUR-1",
        category="LABOUR",
        quantity=Decimal("2"),
        unit="h",
        rate=Decimal("100"),
        currency="RUB",
        source_reference="basis-v1:table-1",
    )
    result = NormativeCalculationResult(
        adapter_qualification_id="qualification-1",
        engine_name="licensed-engine",
        engine_version="1.0",
        normative_basis_version="basis-v1",
        calculation_method="RESOURCE",
        region="77",
        price_period=date(2026, 7, 1),
        work_or_resource_code="WORK-1",
        unit="m",
        applied_coefficients={"winter": Decimal("1.1")},
        resource_components=(resource,),
        total=Decimal("200"),
        currency="RUB",
        calculation_artifact_hash="b" * 64,
        calculated_at=datetime(2026, 7, 23, tzinfo=UTC),
    )
    assert (
        validate_normative_result(
            request,
            result,
            rounding_tolerance=Decimal("0.01"),
        )
        == ()
    )

    invalid = result.model_copy(
        update={
            "region": "78",
            "resource_components": (resource, resource),
            "total": Decimal("999"),
        }
    )
    findings = validate_normative_result(
        request,
        invalid,
        rounding_tolerance=Decimal("0.01"),
    )
    messages = {finding.message for finding in findings}
    assert "Normative result mismatch: region" in messages
    assert "Normative resource components do not reproduce the engine total" in messages
    assert "Normative result contains duplicate resource components" in messages
