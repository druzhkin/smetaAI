from datetime import UTC, datetime

import pytest

from tenderguard.domain.enums import MatchClass
from tenderguard.domain.nomenclature import approve_analogue, assess_exact_match


def test_critical_attribute_mismatch_is_not_similarity_match() -> None:
    match = assess_exact_match(
        match_id="match-1",
        source_item_id="source-1",
        canonical_item_id="canonical-1",
        required_critical_attributes=frozenset({"diameter", "pressure_class", "material"}),
        source_attributes={
            "diameter": "DN100",
            "pressure_class": "PN16",
            "material": "ductile iron",
        },
        canonical_attributes={
            "diameter": "DN100",
            "pressure_class": "PN10",
            "material": "ductile iron",
        },
    )
    assert match.match_class is MatchClass.TECHNICALLY_UNACCEPTABLE
    assert match.mismatched_attributes == frozenset({"pressure_class"})


def test_analogue_requires_complete_attributes_and_versioned_rule() -> None:
    match = assess_exact_match(
        match_id="match-1",
        source_item_id="source-1",
        canonical_item_id="canonical-1",
        required_critical_attributes=frozenset({"diameter", "pressure_class"}),
        source_attributes={"diameter": "DN100"},
        canonical_attributes={"diameter": "DN100", "pressure_class": "PN16"},
    )
    with pytest.raises(ValueError, match="missing critical"):
        approve_analogue(
            match,
            analogue_class=MatchClass.CONDITIONALLY_ACCEPTABLE_ANALOGUE,
            equivalence_rule_version_id="equivalence-v1",
            verified_by="expert-1",
            verified_at=datetime(2026, 7, 23, tzinfo=UTC),
        )
