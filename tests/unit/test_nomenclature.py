# ruff: noqa: RUF001

from datetime import UTC, datetime

import pytest

from tenderguard.domain.enums import MatchClass
from tenderguard.domain.nomenclature import (
    approve_analogue,
    assess_exact_match,
    catalog_retrieval_evidence,
)


def test_catalog_retrieval_explains_compound_identifier_terms_without_equivalence() -> None:
    evidence = catalog_retrieval_evidence(
        source_name="Труба ПЭ100 DN 110 SDR 17",
        canonical_item_id="pipe-pe100-dn110",
        attributes={
            "material": "ПЭ100",
            "diameter": "DN100",
            "sdr": "17",
        },
        critical_attributes=("material", "diameter", "sdr"),
    )

    assert evidence.exact_identifier_mentioned is False
    assert {"100", "17", "dn", "пэ"}.issubset(evidence.matched_terms)
    assert evidence.matched_critical_attributes == ("material", "sdr")


def test_catalog_retrieval_does_not_invent_matches_for_unrelated_item() -> None:
    evidence = catalog_retrieval_evidence(
        source_name="Кабель силовой медный 4x16",
        canonical_item_id="pipe-steel-dn100",
        attributes={"material": "steel", "diameter": "DN100"},
        critical_attributes=("material", "diameter"),
    )

    assert evidence.exact_identifier_mentioned is False
    assert evidence.matched_terms == ()
    assert evidence.matched_critical_attributes == ()


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
