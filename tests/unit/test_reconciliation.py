from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from tenderguard.domain.enums import EvidenceMethod
from tenderguard.domain.models import EvidenceLocation, Observation
from tenderguard.domain.reconciliation import reconcile_observations


def observation(identifier: str, value: Any, method: EvidenceMethod) -> Observation:
    return Observation(
        observation_id=identifier,
        field_name="quantity",
        value=value,
        unit="m",
        method=method,
        method_version="1",
        source_priority=10,
        location=EvidenceLocation(
            document_id="doc-1",
            document_revision_id="doc-1-r1",
            original_object_hash="a" * 64,
            locator_kind="cell",
            locator="Sheet1!B2",
            sheet="Sheet1",
            cell_or_range="B2",
        ),
        observed_at=datetime(2026, 7, 23, tzinfo=UTC),
        actor_id="system",
        confidence=Decimal("0.99"),
    )


def test_independent_agreement_returns_value_without_conflict() -> None:
    value, conflict = reconcile_observations(
        conflict_namespace="project-1",
        observations=(
            observation("obs-1", Decimal("120.0"), EvidenceMethod.NATIVE_PARSER),
            observation("obs-2", Decimal("120.00"), EvidenceMethod.VISUAL_MODEL),
        ),
    )
    assert value == Decimal("120.0")
    assert conflict is None


def test_confidence_does_not_auto_merge_disagreement() -> None:
    value, conflict = reconcile_observations(
        conflict_namespace="project-1",
        observations=(
            observation("obs-1", Decimal("120"), EvidenceMethod.NATIVE_PARSER),
            observation("obs-2", Decimal("102"), EvidenceMethod.VISUAL_MODEL),
        ),
    )
    assert value is None
    assert conflict is not None
    assert conflict.observation_ids == ("obs-1", "obs-2")


def test_independent_agreement_supports_structured_values() -> None:
    value, conflict = reconcile_observations(
        conflict_namespace="project-1",
        observations=(
            observation(
                "obs-1",
                {"work_code": "PIPE_INSTALLATION", "unit": "m"},
                EvidenceMethod.NATIVE_PARSER,
            ),
            observation(
                "obs-2",
                {"unit": "m", "work_code": "PIPE_INSTALLATION"},
                EvidenceMethod.VISUAL_MODEL,
            ),
        ),
    )
    assert value == {"work_code": "PIPE_INSTALLATION", "unit": "m"}
    assert conflict is None


def test_observation_rejects_nested_floating_point_values() -> None:
    with pytest.raises(ValueError, match="nesting level"):
        observation(
            "obs-float",
            {"components": [{"quantity": 1.2}]},
            EvidenceMethod.NATIVE_PARSER,
        )
